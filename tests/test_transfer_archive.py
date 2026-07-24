"""Format-level tests for the .vestigo archive (no stores involved)."""

from __future__ import annotations

import json
import zipfile

import pytest

from vestigo.transfer.archive import (
    FORMAT_VERSION,
    ArchiveFormatError,
    ArchiveReader,
    ArchiveWriter,
    new_archive_path,
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
        assert reader.read_ndjson("postgres/absent.ndjson") == []
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
        items["postgres/sources.ndjson"] = b'{"id": "EVIL"}\n'
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


def test_temp_root_and_archive_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)  # force re-evaluation of TMPDIR
    root = temp_root()
    assert root.is_dir()
    p = new_archive_path("job123")
    assert p.parent == root and p.name == "job123.vestigo"
