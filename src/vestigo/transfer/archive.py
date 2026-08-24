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

The total cap alone is not enough, because it says nothing about any *single*
member: a lone 100 GiB NDJSON member sits well under a 200 GiB total, and
reading it whole would exhaust memory long before the disk. So the metadata
members (``postgres/*``) carry their own much smaller ceiling
(VESTIGO_TRANSFER_MAX_METADATA_BYTES) and are additionally read as streams
rather than materialized. Events and blobs are exempt from that ceiling —
they are the legitimately large part of an archive and are only ever streamed.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from vestigo.core.config import get_settings

FORMAT_VERSION = 1
_CHUNK = 1 << 20

# Members under this prefix are archive *metadata* (the Postgres snapshot):
# small relative to the events they describe, and the only ones a reader ever
# parses rather than streams. They carry the per-member size ceiling.
METADATA_PREFIX = "postgres/"

# JobStore ids: uuid4().hex[:16]. Export working directories are named after
# one, which is how a sweep tells its own scratch from an operator's files.
_JOB_ID_RE = re.compile(r"[0-9a-f]{16}")

logger = logging.getLogger(__name__)

# How long an export archive survives in the temp root before an opportunistic
# sweep removes it. Sizing detail, not an operator tunable: the download link
# is only useful while the analyst who triggered the export is still around.
_ARCHIVE_TTL_SECONDS = 24 * 3600


# Export and import warnings ride into the job result and from there into the
# audit_log detail JSON column, so the list has to stay bounded — a single
# skipped source can otherwise produce one warning per affected row.
MAX_WARNINGS = 50


class ArchiveFormatError(Exception):
    """Raised when an archive is malformed, unsupported, or tampered with."""


def cap_warnings(warnings: list[str]) -> list[str]:
    """Truncate a warning list to ``MAX_WARNINGS`` plus a summary line."""
    if len(warnings) <= MAX_WARNINGS:
        return warnings
    return [*warnings[:MAX_WARNINGS], f"…and {len(warnings) - MAX_WARNINGS} more"]


def _check_member_name(name: str) -> None:
    if name.startswith("/") or ".." in name.split("/"):
        raise ArchiveFormatError(f"unsafe member name: {name}")


def temp_root() -> Path:
    """Directory holding in-flight export archives (swept at app startup).

    Archives hold complete case data, so the directory must be ours alone.
    A world-writable parent (the old default was the system temp dir) lets
    another local user pre-create the path and read every export.

    The directory is created 0700, and that mode is *forced* on an existing
    one — a loose mode is repaired rather than rejected, because repairing it
    is both safe and what an operator wants. The hard failures are the two
    conditions we cannot fix from here: a path that is not a real directory
    (a symlink someone else controls) and one owned by another user.
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
        # Normally unreachable — the chmod above just cleared these bits. It
        # stays as the backstop for filesystems where chmod silently no-ops
        # (some network mounts), which is the one case we must not run in.
        raise RuntimeError(f"transfer temp path is group/world accessible: {root}")
    return root


def is_job_id(value: str) -> bool:
    """Whether a string is shaped like a JobStore id (``uuid4().hex[:16]``)."""
    return bool(_JOB_ID_RE.fullmatch(value))


def new_archive_path(job_id: str) -> Path:
    """Path of the archive for ``job_id``, which must be a real job id.

    The id reaches here straight from a URL path segment, so the shape check is
    the barrier that keeps a request from naming a file outside the temp root
    (``../../etc/passwd``) — the job-store lookup callers do first happens to
    reject the same thing, but nothing about a dict lookup makes that a
    guarantee anyone can see from here.
    """
    if not is_job_id(job_id):
        raise ValueError(f"not a job id: {job_id!r}")
    return temp_root() / f"{job_id}.vestigo"


def is_transfer_artifact(entry: Path) -> bool:
    """Whether a temp-root entry is something a transfer job created.

    The only two shapes a transfer ever writes here: ``<job_id>.vestigo`` for a
    finished archive, and a ``<job_id>/`` working directory. Nothing else in
    the directory is ours, and a sweep must not touch it — ``transfer_temp_path``
    is operator-configurable, and pointing it at a populated directory must
    cost that directory nothing.
    """
    if entry.is_dir():
        return bool(_JOB_ID_RE.fullmatch(entry.name))
    return entry.suffix == ".vestigo"


def sweep_stale(max_age_seconds: float | None = _ARCHIVE_TTL_SECONDS) -> None:
    """Delete export archives and job working dirs older than ``max_age_seconds``.

    ``None`` sweeps them all regardless of age — what startup wants, where every
    leftover is orphaned by definition (the job store is in-memory).

    Called opportunistically when an export starts rather than from a timer:
    this deployment has no scheduler (see ``core/jobs.py``), and an export is
    the only thing that creates these files. Without it, a completed export
    that is never downloaded would sit on disk until the process restarts.
    """
    cutoff = None if max_age_seconds is None else time.time() - max_age_seconds
    root = temp_root()
    foreign = 0
    for entry in root.iterdir():
        if not is_transfer_artifact(entry):
            foreign += 1
            continue
        with contextlib.suppress(OSError):
            if cutoff is not None and entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    if foreign:
        # Once per sweep, not once per entry: a misconfigured transfer_temp_path
        # pointing at a shared directory would otherwise flood the log.
        logger.warning(
            "%s entr(ies) under the transfer temp path %s were not written by a "
            "transfer job and were left alone — is VESTIGO_TRANSFER_TEMP_PATH "
            "pointing at a shared directory?",
            foreign,
            root,
        )


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
        with src.open("rb") as fsrc, self._zip.open(info, mode="w", force_zip64=True) as fdst:
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
        """Cross-check declared sizes against the zip directory and the caps."""
        settings = get_settings()
        meta_limit = settings.transfer_max_metadata_bytes
        total = 0
        seen: set[str] = set()
        for member in members:
            name = member["path"]
            _check_member_name(name)
            if name in seen:
                # Duplicates would be counted twice against the expansion cap
                # but deduped everywhere else — reject rather than pick one.
                raise ArchiveFormatError(f"member listed twice in the manifest: {name}")
            seen.add(name)
            try:
                info = self._zip.getinfo(name)
            except KeyError as exc:
                raise ArchiveFormatError(f"member missing: {name}") from exc
            if info.file_size != member["bytes"]:
                raise ArchiveFormatError(
                    f"member size {info.file_size} does not match the manifest's "
                    f"{member['bytes']}: {name}"
                )
            if meta_limit and name.startswith(METADATA_PREFIX) and info.file_size > meta_limit:
                # The total cap says nothing about one member: a single huge
                # NDJSON fits comfortably under it and would still be too big
                # to hold a row of at a time, let alone whole.
                raise ArchiveFormatError(
                    f"metadata member is {info.file_size} bytes, over the "
                    f"{meta_limit}-byte per-member limit: {name} "
                    "(raise VESTIGO_TRANSFER_MAX_METADATA_BYTES, 0 disables)"
                )
            total += info.file_size
        limit = settings.transfer_max_expanded_bytes
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

    def iter_ndjson(self, arcname: str) -> Iterator[dict[str, Any]]:
        """Stream a listed member one row at a time, bounded by its declared size.

        The bounded alternative to ``read_ndjson``: peak memory is one row
        rather than the whole member, so a caller's footprint no longer scales
        with how big the archive's largest entity happens to be. Every import
        path that walks a stem uses this; ``read_ndjson`` remains for the two
        members that are genuinely small and read as a whole.
        """
        limit = self._declared_size(arcname)
        read = 0
        with self._zip.open(arcname) as f:
            for raw in io.TextIOWrapper(f, encoding="utf-8"):
                read += len(raw.encode())
                if read > limit:
                    # A local header that lies about its size — i.e. a bomb.
                    # Same check verify_members makes, repeated because a
                    # caller may stream a member without re-verifying it.
                    raise ArchiveFormatError(f"member exceeds its declared size: {arcname}")
                if raw.strip():
                    yield json.loads(raw)

    def read_ndjson(self, arcname: str) -> list[dict[str, Any]]:
        return list(self.iter_ndjson(arcname))

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
