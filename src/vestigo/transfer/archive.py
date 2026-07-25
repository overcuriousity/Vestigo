"""The .vestigo case archive format (zip container).

Layout: manifest.json + postgres/*.ndjson + events/<source_id>.arrow +
optional blobs/<sha256>. The manifest carries a SHA-256 and a byte size per
member; readers verify every member before any data is written. Format
versioning: readers reject anything newer than FORMAT_VERSION.

Sizes are load-bearing, not informational: an import is an untrusted upload
from any authenticated user, so every read is bounded by the member's declared
size, the declared size is cross-checked against the zip's own directory entry,
and the total is capped by VESTIGO_TRANSFER_MAX_EXPANDED_BYTES. Without that a
deflate bomb inside a small upload could exhaust memory or disk.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import time
import zipfile
from pathlib import Path
from typing import Any

from vestigo.core.config import get_settings

FORMAT_VERSION = 1
_CHUNK = 1 << 20

# How long an export archive survives in the temp root before an opportunistic
# sweep removes it. Sizing detail, not an operator tunable: the download link
# is only useful while the analyst who triggered the export is still around.
_ARCHIVE_TTL_SECONDS = 24 * 3600


class ArchiveFormatError(Exception):
    """Raised when an archive is malformed, unsupported, or tampered with."""


def _check_member_name(name: str) -> None:
    if name.startswith("/") or ".." in name.split("/"):
        raise ArchiveFormatError(f"unsafe member name: {name}")


def temp_root() -> Path:
    """Directory holding in-flight export archives (swept at app startup).

    Archives hold complete case data, so the directory must be ours alone.
    A world-writable parent (the old default was the system temp dir) lets
    another local user pre-create the path and read every export, so an
    unexpected owner or mode is a hard failure rather than a warning.
    """
    root = Path(get_settings().transfer_temp_path)
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "posix":
        # No POSIX ownership/mode semantics to assert.
        with contextlib.suppress(OSError):
            root.chmod(0o700)
        return root
    root.chmod(0o700)
    st = root.lstat()
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"transfer temp path is not a real directory: {root}")
    if st.st_uid != os.getuid():
        raise RuntimeError(f"transfer temp path is not owned by this process: {root}")
    if st.st_mode & 0o077:
        raise RuntimeError(f"transfer temp path is group/world accessible: {root}")
    return root


def new_archive_path(job_id: str) -> Path:
    return temp_root() / f"{job_id}.vestigo"


def sweep_stale(max_age_seconds: int = _ARCHIVE_TTL_SECONDS) -> None:
    """Delete export archives and job working dirs older than the TTL.

    Called opportunistically when an export starts rather than from a timer:
    this deployment has no scheduler (see ``core/jobs.py``), and an export is
    the only thing that creates these files. Without it, a completed export
    that is never downloaded would sit on disk until the process restarts.
    """
    cutoff = time.time() - max_age_seconds
    root = temp_root()
    for entry in root.iterdir():
        with contextlib.suppress(OSError):
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)


class ArchiveWriter:
    """Streaming zip writer that hashes every member as it is written."""

    def __init__(self, path: Path) -> None:
        self._zip = zipfile.ZipFile(path, "w", allowZip64=True)
        self._members: list[dict[str, Any]] = []

    def add_bytes(self, arcname: str, data: bytes, *, compress: bool = True) -> None:
        _check_member_name(arcname)
        info = zipfile.ZipInfo(arcname)
        info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
        self._zip.writestr(info, data)
        self._record(arcname, hashlib.sha256(data).hexdigest(), len(data))

    def add_file(self, arcname: str, src: Path, *, compress: bool = False) -> None:
        _check_member_name(arcname)
        sha = hashlib.sha256()
        total = 0
        info = zipfile.ZipInfo(arcname)
        info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
        with src.open("rb") as fsrc, self._zip.open(info, mode="w") as fdst:
            while chunk := fsrc.read(_CHUNK):
                sha.update(chunk)
                total += len(chunk)
                fdst.write(chunk)
        self._record(arcname, sha.hexdigest(), total)

    def _record(self, arcname: str, sha256: str, size: int) -> None:
        self._members.append({"path": arcname, "sha256": sha256, "bytes": size})

    def finish(self, manifest_core: dict[str, Any]) -> None:
        """Write manifest.json (members appended) and close the archive."""
        manifest = {**manifest_core, "members": self._members}
        info = zipfile.ZipInfo("manifest.json")
        info.compress_type = zipfile.ZIP_DEFLATED
        self._zip.writestr(info, json.dumps(manifest, indent=2).encode())
        self._zip.close()


class ArchiveReader:
    """Reads and verifies an archive. Construction validates the manifest."""

    def __init__(self, path: Path) -> None:
        try:
            self._zip = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise ArchiveFormatError(f"not a zip archive: {exc}") from exc
        try:
            raw = self._zip.read("manifest.json")
        except KeyError as exc:
            self._zip.close()
            raise ArchiveFormatError("manifest.json missing") from exc
        try:
            self.manifest: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._zip.close()
            raise ArchiveFormatError(f"manifest is not valid JSON: {exc}") from exc
        if not isinstance(self.manifest, dict):
            self._zip.close()
            raise ArchiveFormatError("manifest is not a JSON object")
        version = self.manifest.get("format_version")
        # type() is int, not isinstance: JSON true is a bool, and bool is an
        # int subclass — it must not satisfy the version check.
        if type(version) is not int or version < 1:
            self._zip.close()
            raise ArchiveFormatError("manifest missing integer format_version")
        if version > FORMAT_VERSION:
            self._zip.close()
            raise ArchiveFormatError(
                f"archive format_version {version} newer than supported {FORMAT_VERSION}"
            )
        members = self.manifest.get("members")
        if not isinstance(members, list) or any(
            not isinstance(m, dict)
            or not isinstance(m.get("path"), str)
            or not isinstance(m.get("sha256"), str)
            or type(m.get("bytes")) is not int
            or m["bytes"] < 0
            for m in members
        ):
            self._zip.close()
            raise ArchiveFormatError(
                "manifest members must be a list of objects with str path, str sha256, "
                "and int bytes"
            )
        try:
            self._validate_sizes(members)
        except ArchiveFormatError:
            self._zip.close()
            raise
        # Only manifest-listed members are hash-verified and size-bounded;
        # anything else in the zip is untrusted and is never read.
        self.verified_names: set[str] = {m["path"] for m in members}
        self._sizes: dict[str, int] = {m["path"]: m["bytes"] for m in members}

    def _validate_sizes(self, members: list[dict[str, Any]]) -> None:
        """Cross-check declared sizes against the zip directory and the cap."""
        total = 0
        for member in members:
            name = member["path"]
            _check_member_name(name)
            try:
                info = self._zip.getinfo(name)
            except KeyError as exc:
                raise ArchiveFormatError(f"member missing: {name}") from exc
            if info.file_size != member["bytes"]:
                raise ArchiveFormatError(
                    f"member size {info.file_size} does not match the manifest's "
                    f"{member['bytes']}: {name}"
                )
            total += info.file_size
        limit = get_settings().transfer_max_expanded_bytes
        if limit and total > limit:
            raise ArchiveFormatError(
                f"archive expands to {total} bytes, over the {limit}-byte limit "
                "(raise VESTIGO_TRANSFER_MAX_EXPANDED_BYTES, 0 disables)"
            )

    def _declared_size(self, arcname: str) -> int:
        """Declared size of a manifest-listed member; raise if unlisted."""
        _check_member_name(arcname)
        if arcname not in self._sizes:
            raise ArchiveFormatError(f"member not listed in the manifest: {arcname}")
        return self._sizes[arcname]

    def verify_members(self) -> None:
        """SHA-256 every manifest member; raise on missing/mismatched/unsafe."""
        for member in self.manifest.get("members", []):
            name = member["path"]
            limit = self._declared_size(name)
            read = 0
            try:
                with self._zip.open(name) as f:
                    sha = hashlib.sha256()
                    while chunk := f.read(_CHUNK):
                        read += len(chunk)
                        if read > limit:
                            # The zip directory said one size, the stream is
                            # longer — a lying local header, i.e. a bomb.
                            raise ArchiveFormatError(f"member exceeds its declared size: {name}")
                        sha.update(chunk)
            except KeyError as exc:
                raise ArchiveFormatError(f"member missing: {name}") from exc
            if sha.hexdigest() != member["sha256"]:
                raise ArchiveFormatError(f"hash mismatch: {name}")

    def _read_bounded(self, arcname: str) -> bytes:
        """Read a manifest-listed member, refusing to exceed its declared size."""
        limit = self._declared_size(arcname)
        try:
            with self._zip.open(arcname) as f:
                data = f.read(limit + 1)
        except KeyError as exc:
            raise ArchiveFormatError(f"member missing: {arcname}") from exc
        if len(data) > limit:
            raise ArchiveFormatError(f"member exceeds its declared size: {arcname}")
        return data

    def read_json(self, arcname: str) -> Any:
        return json.loads(self._read_bounded(arcname))

    def read_ndjson(self, arcname: str) -> list[dict[str, Any]]:
        raw = self._read_bounded(arcname)
        return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]

    def open_member(self, arcname: str):
        """Raw stream for a listed member. Only safe after ``verify_members``,
        which is what bounds how much a consumer can read from it."""
        self._declared_size(arcname)
        return self._zip.open(arcname)

    def extract_to(self, arcname: str, dest: Path) -> str:
        """Extract one member to dest; returns the SHA-256 of the extracted
        bytes (single pass, so callers can verify content-addressed names
        without re-reading the file)."""
        limit = self._declared_size(arcname)
        sha = hashlib.sha256()
        written = 0
        with self._zip.open(arcname) as src, dest.open("wb") as dst:
            while chunk := src.read(_CHUNK):
                written += len(chunk)
                if written > limit:
                    raise ArchiveFormatError(f"member exceeds its declared size: {arcname}")
                sha.update(chunk)
                dst.write(chunk)
        return sha.hexdigest()

    def member_names(self) -> list[str]:
        return self._zip.namelist()

    def close(self) -> None:
        self._zip.close()
