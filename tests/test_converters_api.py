"""Tests for the vendored converter download endpoints."""

from __future__ import annotations

import hashlib
import json
import py_compile

import pytest

from tests.conftest import as_admin
from tracesignal.api.routers.converters import ASSETS_DIR


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
    assert "nginx2timesketch" in names

    resp = client.get("/api/converters/nginx2timesketch")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/x-python")
    # Download must be byte-identical to the committed asset.
    assert resp.content == (ASSETS_DIR / "nginx2timesketch.py").read_bytes()


def test_download_unknown_converter_404(client, admin_bootstrap) -> None:
    as_admin(client, admin_bootstrap)
    assert client.get("/api/converters/evil-name").status_code == 404
