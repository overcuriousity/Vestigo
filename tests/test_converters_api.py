"""Tests for the vendored converter download endpoints."""

from __future__ import annotations

import hashlib
import json
import py_compile

import pytest

from tests.conftest import as_admin
from vestigo.api.routers.converters import ASSETS_DIR


def test_assets_directory_matches_manifest() -> None:
    manifest = json.loads((ASSETS_DIR / "manifest.json").read_text(encoding="utf-8"))
    listed = {c["filename"] for c in manifest["converters"]}
    on_disk = {p.name for p in ASSETS_DIR.glob("*.py")}
    assert listed == on_disk
    assert manifest["upstream"] == "https://github.com/overcuriousity/2timesketch"
    assert manifest["commit"]


def test_manifest_hashes_match_committed_assets() -> None:
    manifest = json.loads((ASSETS_DIR / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["converters"]:
        data = (ASSETS_DIR / entry["filename"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], entry["name"]
        assert len(data) == entry["size_bytes"], entry["name"]


@pytest.mark.parametrize("path", sorted(ASSETS_DIR.glob("*.py")), ids=lambda p: p.name)
def test_vendored_script_compiles(path, tmp_path) -> None:
    py_compile.compile(str(path), cfile=str(tmp_path / "out.pyc"), doraise=True)


def test_list_converters_requires_auth(client) -> None:
    assert client.get("/api/converters").status_code == 401


def test_list_and_download(client, admin_bootstrap) -> None:
    as_admin(client, admin_bootstrap)

    resp = client.get("/api/converters")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = [c["name"] for c in body["converters"]]
    assert "browser2timesketch" in names
    # cloudtrail/filterlog/nginx/pcap/suricata are served both ways: the
    # vendored stdlib-only script stays available as a minimal-dependency
    # alternative alongside the native pyarrow-based Parquet converter.
    for stem in ("cloudtrail", "evtx", "filterlog", "nginx", "pcap", "suricata"):
        assert f"{stem}2timesketch" in names
        assert f"{stem}2vestigo" in names
    # timesketch2parquet is a generic converter with no vendored counterpart
    # to pair with (it's new, not a port of an existing *2timesketch script).
    assert "timesketch2parquet" in names

    resp = client.get("/api/converters/nginx2vestigo")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    # Download must be byte-identical to the committed asset.
    assert resp.content == (ASSETS_DIR / "nginx2vestigo.py").read_bytes()


def test_native_converter_entries_flagged() -> None:
    manifest = json.loads((ASSETS_DIR / "manifest.json").read_text(encoding="utf-8"))
    by_name = {c["name"]: c for c in manifest["converters"]}
    for name in (
        "nginx2vestigo",
        "cloudtrail2vestigo",
        "evtx2vestigo",
        "filterlog2vestigo",
        "pcap2vestigo",
        "suricata2vestigo",
        "timesketch2parquet",
    ):
        assert by_name[name]["native"] is True
        assert "pyarrow" in by_name[name]["requires"]
    # evtx2vestigo is the only converter needing a second runtime dependency.
    assert by_name["evtx2vestigo"]["requires"] == ["pyarrow", "evtx"]
    # Vendored entries carry no native flag.
    assert "native" not in by_name["browser2timesketch"]
    assert "native" not in by_name["cloudtrail2timesketch"]
    assert "native" not in by_name["filterlog2timesketch"]
    assert "native" not in by_name["nginx2timesketch"]
    assert "native" not in by_name["pcap2timesketch"]
    assert "native" not in by_name["suricata2timesketch"]


def test_download_unknown_converter_404(client, admin_bootstrap) -> None:
    as_admin(client, admin_bootstrap)
    assert client.get("/api/converters/evil-name").status_code == 404
