"""Format-level tests for the .vestigo archive (no stores involved)."""

from __future__ import annotations

import json
import os
import time
import uuid
import zipfile
from pathlib import Path

import pytest

from vestigo.core.config import get_settings
from vestigo.transfer.archive import (
    FORMAT_VERSION,
    MAX_WARNINGS,
    ArchiveFormatError,
    ArchiveReader,
    ArchiveWriter,
    cap_warnings,
    new_archive_path,
    sweep_stale,
    temp_root,
)


def _write_sample(path, manifest_core=None):
    writer = ArchiveWriter(path)
    writer.add_bytes("postgres/case.json", json.dumps({"name": "Demo"}).encode())
    writer.add_bytes("postgres/sources.ndjson", b'{"id": "s1"}\n{"id": "s2"}\n')
    writer.finish(manifest_core or {"format_version": FORMAT_VERSION, "case": {"name": "Demo"}})


class TestRoundTrip:
    def test_write_read_verify(self, tmp_path):
        path = tmp_path / "demo.vestigo"
        _write_sample(path)
        reader = ArchiveReader(path)
        assert reader.manifest["format_version"] == FORMAT_VERSION
        assert {m["path"] for m in reader.manifest["members"]} == {
            "postgres/case.json",
            "postgres/sources.ndjson",
        }
        reader.verify_members()  # must not raise
        assert reader.read_json("postgres/case.json") == {"name": "Demo"}
        assert reader.read_ndjson("postgres/sources.ndjson") == [{"id": "s1"}, {"id": "s2"}]
        reader.close()

    def test_unlisted_member_cannot_be_read(self, tmp_path):
        # A member that is not in the manifest is neither hash-verified nor
        # size-bounded, so reading it at all would defeat both guarantees.
        path = tmp_path / "demo.vestigo"
        _write_sample(path)
        reader = ArchiveReader(path)
        with pytest.raises(ArchiveFormatError, match="not listed in the manifest"):
            reader.read_ndjson("postgres/absent.ndjson")
        reader.close()

    def test_member_hashes_recorded(self, tmp_path):
        path = tmp_path / "demo.vestigo"
        _write_sample(path)
        reader = ArchiveReader(path)
        for member in reader.manifest["members"]:
            assert len(member["sha256"]) == 64
            assert member["bytes"] > 0
        reader.close()


class TestRejection:
    def test_not_a_zip(self, tmp_path):
        path = tmp_path / "junk.vestigo"
        path.write_bytes(b"this is not a zip")
        with pytest.raises(ArchiveFormatError, match="not a zip"):
            ArchiveReader(path)

    def test_missing_manifest(self, tmp_path):
        path = tmp_path / "nomanifest.vestigo"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("postgres/case.json", "{}")
        with pytest.raises(ArchiveFormatError, match="manifest"):
            ArchiveReader(path)

    def test_newer_format_version_rejected(self, tmp_path):
        path = tmp_path / "v2.vestigo"
        _write_sample(path, {"format_version": FORMAT_VERSION + 1})
        with pytest.raises(ArchiveFormatError, match="format_version"):
            ArchiveReader(path)

    def test_non_dict_manifest_rejected(self, tmp_path):
        path = tmp_path / "listmanifest.vestigo"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("manifest.json", "[1, 2, 3]")
        with pytest.raises(ArchiveFormatError, match="manifest"):
            ArchiveReader(path)

    def test_tampered_member_detected(self, tmp_path):
        path = tmp_path / "demo.vestigo"
        _write_sample(path)
        # Rewrite the zip, corrupting one member but keeping the manifest.
        with zipfile.ZipFile(path) as zin:
            items = {i.filename: zin.read(i.filename) for i in zin.infolist()}
        # Same length as the original: a size change is caught earlier, by the
        # manifest cross-check, so this exercises the hash specifically.
        items["postgres/sources.ndjson"] = b'{"id": "s1"}\n{"id": "XX"}\n'
        with zipfile.ZipFile(path, "w") as zout:
            for name, data in items.items():
                zout.writestr(name, data)
        reader = ArchiveReader(path)
        with pytest.raises(ArchiveFormatError, match="hash mismatch"):
            reader.verify_members()
        reader.close()

    def test_unsafe_member_name_rejected(self, tmp_path):
        path = tmp_path / "demo.vestigo"
        _write_sample(path)
        reader = ArchiveReader(path)
        with pytest.raises(ArchiveFormatError, match="unsafe"):
            reader.extract_to("../escape", tmp_path / "out")
        reader.close()

    def test_bool_format_version_rejected(self, tmp_path):
        path = tmp_path / "boolversion.vestigo"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("postgres/case.json", "{}")
            # JSON true must not satisfy the integer version check.
            z.writestr("manifest.json", json.dumps({"format_version": True, "members": []}))
        with pytest.raises(ArchiveFormatError, match="format_version"):
            ArchiveReader(path)

    def test_members_must_be_list_of_objects(self, tmp_path):
        path = tmp_path / "badmembers.vestigo"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("postgres/case.json", "{}")
            z.writestr(
                "manifest.json",
                json.dumps({"format_version": FORMAT_VERSION, "members": {}}),
            )
        with pytest.raises(ArchiveFormatError, match="members"):
            ArchiveReader(path)

    def test_member_entries_need_str_path_and_sha256(self, tmp_path):
        path = tmp_path / "badmemberentries.vestigo"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("postgres/case.json", "{}")
            z.writestr(
                "manifest.json",
                json.dumps(
                    {"format_version": FORMAT_VERSION, "members": [{"path": "x", "sha256": 7}]}
                ),
            )
        with pytest.raises(ArchiveFormatError, match="members"):
            ArchiveReader(path)


class TestSizeBounds:
    """Sizes are load-bearing: an import is an untrusted upload."""

    def _repack(self, path, mutate):
        with zipfile.ZipFile(path) as zin:
            items = {i.filename: zin.read(i.filename) for i in zin.infolist()}
        manifest = json.loads(items["manifest.json"])
        mutate(manifest, items)
        items["manifest.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(path, "w") as zout:
            for name, data in items.items():
                zout.writestr(name, data)

    def test_declared_size_mismatch_rejected(self, tmp_path):
        path = tmp_path / "demo.vestigo"
        _write_sample(path)

        def _shrink(manifest, _items):
            manifest["members"][0]["bytes"] = 1

        self._repack(path, _shrink)
        with pytest.raises(ArchiveFormatError, match="does not match the manifest"):
            ArchiveReader(path)

    def test_missing_bytes_rejected(self, tmp_path):
        path = tmp_path / "demo.vestigo"
        _write_sample(path)

        def _drop(manifest, _items):
            del manifest["members"][0]["bytes"]

        self._repack(path, _drop)
        with pytest.raises(ArchiveFormatError, match="members"):
            ArchiveReader(path)

    def test_total_over_ceiling_rejected(self, tmp_path, monkeypatch):
        path = tmp_path / "demo.vestigo"
        _write_sample(path)
        monkeypatch.setenv("VESTIGO_TRANSFER_MAX_EXPANDED_BYTES", "10")
        get_settings.cache_clear()
        with pytest.raises(ArchiveFormatError, match="over the 10-byte limit"):
            ArchiveReader(path)

    def test_zero_ceiling_disables_the_check(self, tmp_path, monkeypatch):
        path = tmp_path / "demo.vestigo"
        _write_sample(path)
        monkeypatch.setenv("VESTIGO_TRANSFER_MAX_EXPANDED_BYTES", "0")
        get_settings.cache_clear()
        reader = ArchiveReader(path)  # must not raise
        reader.close()

    def test_manifest_member_absent_from_zip_rejected(self, tmp_path):
        path = tmp_path / "demo.vestigo"
        _write_sample(path)

        def _drop_member(_manifest, items):
            del items["postgres/sources.ndjson"]

        self._repack(path, _drop_member)
        with pytest.raises(ArchiveFormatError, match="member missing"):
            ArchiveReader(path)


class TestMetadataCeiling:
    """The total cap says nothing about one member: a lone huge NDJSON fits
    under it and would still be an out-of-memory kill for any authenticated
    user who uploads one."""

    def _archive(self, path, metadata_bytes, events_bytes=0):
        writer = ArchiveWriter(path)
        writer.add_bytes("postgres/case.json", json.dumps({"name": "Demo"}).encode())
        writer.add_bytes("postgres/annotations.ndjson", b"x" * metadata_bytes)
        if events_bytes:
            writer.add_bytes("events/s1.arrow", b"x" * events_bytes, compress=False)
        writer.finish({"format_version": FORMAT_VERSION, "case": {"name": "Demo"}})

    def test_oversized_metadata_member_rejected(self, tmp_path, monkeypatch):
        path = tmp_path / "demo.vestigo"
        self._archive(path, metadata_bytes=5000)
        monkeypatch.setenv("VESTIGO_TRANSFER_MAX_METADATA_BYTES", "1000")
        get_settings.cache_clear()
        with pytest.raises(ArchiveFormatError, match="per-member limit"):
            ArchiveReader(path)

    def test_events_and_blobs_are_exempt(self, tmp_path, monkeypatch):
        """They stream, and they are the legitimately large part of an archive."""
        path = tmp_path / "demo.vestigo"
        self._archive(path, metadata_bytes=100, events_bytes=5000)
        monkeypatch.setenv("VESTIGO_TRANSFER_MAX_METADATA_BYTES", "1000")
        get_settings.cache_clear()
        ArchiveReader(path).close()  # must not raise

    def test_zero_disables_the_check(self, tmp_path, monkeypatch):
        path = tmp_path / "demo.vestigo"
        self._archive(path, metadata_bytes=5000)
        monkeypatch.setenv("VESTIGO_TRANSFER_MAX_METADATA_BYTES", "0")
        get_settings.cache_clear()
        ArchiveReader(path).close()  # must not raise

    def test_the_check_precedes_any_read(self, tmp_path, monkeypatch):
        """It runs in the constructor, so the oversized member is never opened
        — the point is to reject it without ever holding its bytes."""
        path = tmp_path / "demo.vestigo"
        self._archive(path, metadata_bytes=5000)
        monkeypatch.setenv("VESTIGO_TRANSFER_MAX_METADATA_BYTES", "1000")
        get_settings.cache_clear()
        real_open = zipfile.ZipFile.open

        def _guard(self, name, *args, **kwargs):
            filename = name.filename if hasattr(name, "filename") else name
            if filename == "postgres/annotations.ndjson":
                pytest.fail("the oversized member was opened")
            return real_open(self, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "open", _guard)
        with pytest.raises(ArchiveFormatError, match="per-member limit"):
            ArchiveReader(path)


class TestStreamingNdjson:
    def test_iter_matches_read(self, tmp_path):
        path = tmp_path / "demo.vestigo"
        _write_sample(path)
        reader = ArchiveReader(path)
        try:
            rows = list(reader.iter_ndjson("postgres/sources.ndjson"))
            assert rows == [{"id": "s1"}, {"id": "s2"}]
            assert reader.read_ndjson("postgres/sources.ndjson") == rows
        finally:
            reader.close()

    def test_blank_lines_skipped(self, tmp_path):
        path = tmp_path / "demo.vestigo"
        writer = ArchiveWriter(path)
        writer.add_bytes("postgres/sources.ndjson", b'{"id": "s1"}\n\n\n{"id": "s2"}\n')
        writer.finish({"format_version": FORMAT_VERSION})
        reader = ArchiveReader(path)
        try:
            assert list(reader.iter_ndjson("postgres/sources.ndjson")) == [
                {"id": "s1"},
                {"id": "s2"},
            ]
        finally:
            reader.close()

    def test_stream_longer_than_declared_is_rejected(self, tmp_path):
        """A local header that lies about its size, i.e. a bomb — iter_ndjson
        stops rather than yielding past the manifest's declared bound."""
        path = tmp_path / "demo.vestigo"
        _write_sample(path)
        reader = ArchiveReader(path)
        try:
            reader._sizes["postgres/sources.ndjson"] = 5  # shorter than the member
            with pytest.raises(ArchiveFormatError, match="exceeds its declared size"):
                list(reader.iter_ndjson("postgres/sources.ndjson"))
        finally:
            reader.close()

    def test_unlisted_member_cannot_be_streamed(self, tmp_path):
        path = tmp_path / "demo.vestigo"
        _write_sample(path)
        reader = ArchiveReader(path)
        try:
            with pytest.raises(ArchiveFormatError, match="not listed in the manifest"):
                list(reader.iter_ndjson("postgres/nope.ndjson"))
        finally:
            reader.close()


def test_temp_root_and_archive_path(tmp_path):
    # The conftest autouse fixture already points transfer_temp_path at tmp_path.
    root = temp_root()
    assert root.is_dir()
    assert root.stat().st_mode & 0o077 == 0
    # A real JobStore id: uuid4().hex[:16], which new_archive_path now requires.
    p = new_archive_path("0123456789abcdef")
    assert p.parent == root and p.name == "0123456789abcdef.vestigo"


def test_temp_root_repairs_a_loose_mode(tmp_path, monkeypatch):
    """A group/world-readable directory we own is fixed, not rejected."""
    root = tmp_path / "loose"
    root.mkdir(mode=0o755)
    monkeypatch.setenv("VESTIGO_TRANSFER_TEMP_PATH", str(root))
    get_settings.cache_clear()
    assert temp_root().stat().st_mode & 0o077 == 0


def test_temp_root_rejects_a_world_readable_dir_it_cannot_repair(tmp_path, monkeypatch):
    """The mode check is the backstop for filesystems where chmod no-ops."""
    root = tmp_path / "loose"
    root.mkdir(mode=0o755)
    monkeypatch.setenv("VESTIGO_TRANSFER_TEMP_PATH", str(root))
    get_settings.cache_clear()
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
    with pytest.raises(RuntimeError, match="group/world accessible"):
        temp_root()


def test_temp_root_rejects_a_symlinked_path(tmp_path, monkeypatch):
    """A symlink is a path someone else may control — never adopt it."""
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("VESTIGO_TRANSFER_TEMP_PATH", str(link))
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="not a real directory"):
        temp_root()


def _job_id() -> str:
    """A JobStore-shaped id — what an export working directory is named."""
    return uuid.uuid4().hex[:16]


def test_sweep_stale_removes_expired_entries(tmp_path):
    root = temp_root()
    fresh = root / "fresh.vestigo"
    stale = root / "stale.vestigo"
    stale_dir = root / _job_id()
    fresh.write_bytes(b"x")
    stale.write_bytes(b"x")
    stale_dir.mkdir()
    (stale_dir / "scratch").write_bytes(b"x")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    os.utime(stale_dir, (old, old))

    sweep_stale(max_age_seconds=60)

    assert fresh.exists()
    assert not stale.exists()
    assert not stale_dir.exists()


def test_sweep_stale_without_a_ttl_removes_everything_of_ours(tmp_path):
    """What startup does: after a restart every leftover is orphaned."""
    root = temp_root()
    fresh = root / "fresh.vestigo"
    fresh_dir = root / _job_id()
    fresh.write_bytes(b"x")
    fresh_dir.mkdir()

    sweep_stale(max_age_seconds=None)

    assert not fresh.exists()
    assert not fresh_dir.exists()


def test_sweep_stale_leaves_foreign_entries_alone(tmp_path):
    """transfer_temp_path is operator-configurable — pointing it at a populated
    directory must not cost that directory anything."""
    root = temp_root()
    bystander_file = root / "important.db"
    bystander_dir = root / "someone-elses-data"
    bystander_file.write_bytes(b"x")
    bystander_dir.mkdir()
    (bystander_dir / "payload").write_bytes(b"x")
    old = time.time() - 3600
    os.utime(bystander_file, (old, old))
    os.utime(bystander_dir, (old, old))

    sweep_stale(max_age_seconds=None)

    assert bystander_file.exists()
    assert (bystander_dir / "payload").exists()
    assert root.exists()

    def test_duplicate_member_names_rejected(self, tmp_path):
        """A duplicate would be summed twice against the expansion cap but
        deduped everywhere else — reject rather than pick one."""
        path = tmp_path / "demo.vestigo"
        _write_sample(path)

        def _duplicate(manifest, _items):
            manifest["members"].append(dict(manifest["members"][0]))

        self._repack(path, _duplicate)
        with pytest.raises(ArchiveFormatError, match="listed twice"):
            ArchiveReader(path)


class TestWarningBounds:
    def test_short_lists_pass_through(self):
        assert cap_warnings(["a", "b"]) == ["a", "b"]

    def test_long_lists_truncate_with_a_summary(self):
        capped = cap_warnings([f"w{i}" for i in range(MAX_WARNINGS + 5)])
        assert len(capped) == MAX_WARNINGS + 1
        assert capped[-1] == "…and 5 more"
