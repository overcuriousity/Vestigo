"""The .vestigo case archive format (zip container).

Layout: manifest.json + postgres/*.ndjson + events/<source_id>.arrow +
optional blobs/<sha256>. The manifest carries a SHA-256 per member; readers
verify every member before any data is written. Format versioning: readers
reject anything newer than FORMAT_VERSION.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
_CHUNK = 1 << 20


class ArchiveFormatError(Exception):
    """Raised when an archive is malformed, unsupported, or tampered with."""


def _check_member_name(name: str) -> None:
    if name.startswith("/") or ".." in name.split("/"):
        raise ArchiveFormatError(f"unsafe member name: {name}")


def temp_root() -> Path:
    """Directory holding in-flight export archives (swept at app startup)."""
    root = Path(tempfile.gettempdir()) / "vestigo-transfer"
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_archive_path(job_id: str) -> Path:
    return temp_root() / f"{job_id}.vestigo"


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
        version = self.manifest.get("format_version")
        if not isinstance(version, int) or version < 1:
            self._zip.close()
            raise ArchiveFormatError("manifest missing integer format_version")
        if version > FORMAT_VERSION:
            self._zip.close()
            raise ArchiveFormatError(
                f"archive format_version {version} newer than supported {FORMAT_VERSION}"
            )

    def verify_members(self) -> None:
        """SHA-256 every manifest member; raise on missing/mismatched/unsafe."""
        for member in self.manifest.get("members", []):
            name = member["path"]
            _check_member_name(name)
            try:
                with self._zip.open(name) as f:
                    sha = hashlib.sha256()
                    while chunk := f.read(_CHUNK):
                        sha.update(chunk)
            except KeyError as exc:
                raise ArchiveFormatError(f"member missing: {name}") from exc
            if sha.hexdigest() != member["sha256"]:
                raise ArchiveFormatError(f"hash mismatch: {name}")

    def read_json(self, arcname: str) -> Any:
        _check_member_name(arcname)
        return json.loads(self._zip.read(arcname))

    def read_ndjson(self, arcname: str) -> list[dict[str, Any]]:
        _check_member_name(arcname)
        try:
            raw = self._zip.read(arcname)
        except KeyError:
            return []
        return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]

    def open_member(self, arcname: str):
        _check_member_name(arcname)
        return self._zip.open(arcname)

    def extract_to(self, arcname: str, dest: Path) -> None:
        _check_member_name(arcname)
        with self._zip.open(arcname) as src, dest.open("wb") as dst:
            shutil.copyfileobj(src, dst, _CHUNK)

    def member_names(self) -> list[str]:
        return self._zip.namelist()

    def close(self) -> None:
        self._zip.close()
