"""Endpoint tests for case export/import (SQLite store, no ClickHouse needed
for empty cases — the ClickHouse factory is lazy)."""

from __future__ import annotations

import json
import time
import zipfile
from io import BytesIO

from tests.conftest import as_admin, login


def _create_case(client, name="API Case") -> str:
    resp = client.post("/api/cases/", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["case"]["id"]


def _register_user(client, username: str, password: str) -> None:
    resp = client.post(
        "/api/admin/users",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text


def _job_terminal(client, job_id: str) -> dict:
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not reach a terminal state")


class TestExportEndpoint:
    def test_anonymous_401(self, client):
        resp = client.post("/api/cases/whatever/export")
        assert resp.status_code == 401

    def test_non_member_403(self, client, admin_bootstrap):
        as_admin(client, admin_bootstrap)
        case_id = _create_case(client)
        _register_user(client, "mallory", "mallory-pass-123")
        login(client, "mallory", "mallory-pass-123")
        resp = client.post(f"/api/cases/{case_id}/export")
        assert resp.status_code == 403

    def test_export_download_and_audit(self, client, admin_bootstrap):
        as_admin(client, admin_bootstrap)
        case_id = _create_case(client)
        resp = client.post(f"/api/cases/{case_id}/export?include_blobs=false")
        assert resp.status_code == 202, resp.text
        job = _job_terminal(client, resp.json()["job_id"])
        assert job["status"] == "completed", job.get("error")

        dl = client.get(f"/api/cases/{case_id}/export/{job['id']}/download")
        assert dl.status_code == 200
        with zipfile.ZipFile(BytesIO(dl.content)) as z:
            manifest = json.loads(z.read("manifest.json"))
        assert manifest["case"]["id"] == case_id
        assert manifest["format_version"] == 1

        audit = client.get(f"/api/admin/audit?case_id={case_id}&action=case.export")
        assert audit.status_code == 200
        entries = audit.json()["audit"]
        assert any(e["action"] == "case.export" for e in entries)

        # Download deletes the server-side temp archive.
        dl2 = client.get(f"/api/cases/{case_id}/export/{job['id']}/download")
        assert dl2.status_code == 404


class TestImportEndpoint:
    def _archive_bytes(self) -> bytes:
        """Minimal valid archive: manifest + case.json only."""
        import hashlib

        from vestigo.transfer.archive import FORMAT_VERSION

        buf = BytesIO()
        case_json = json.dumps({"id": "old-case", "name": "Imported", "description": None}).encode()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("postgres/case.json", case_json)
            z.writestr(
                "postgres/user_refs.json",
                json.dumps({"users": {}, "team": None}).encode(),
            )
            manifest = {
                "format_version": FORMAT_VERSION,
                "vestigo_version": "test",
                "exported_at": "2026-07-24T00:00:00+00:00",
                "exported_by": "alice",
                "case": {"id": "old-case", "name": "Imported"},
                "include_blobs": False,
                "counts": {},
                "members": [
                    {
                        "path": "postgres/case.json",
                        "sha256": hashlib.sha256(case_json).hexdigest(),
                        "bytes": len(case_json),
                    }
                ],
            }
            z.writestr("manifest.json", json.dumps(manifest).encode())
        return buf.getvalue()

    def test_anonymous_401(self, client):
        resp = client.post("/api/cases/import")
        assert resp.status_code == 401

    def test_import_creates_importer_owned_case(self, client, admin_bootstrap):
        as_admin(client, admin_bootstrap)
        _register_user(client, "bob", "bob-pass-12345")
        me = login(client, "bob", "bob-pass-12345")
        resp = client.post(
            "/api/cases/import",
            files={"file": ("backup.vestigo", self._archive_bytes(), "application/zip")},
        )
        assert resp.status_code == 202, resp.text
        job = _job_terminal(client, resp.json()["job_id"])
        assert job["status"] == "completed", job.get("error")
        new_case_id = job["result"]["case_id"]

        case = client.get(f"/api/cases/{new_case_id}")
        assert case.status_code == 200
        body = case.json()["case"]
        assert body["name"] == "Imported"
        assert body["owner_id"] == me["user"]["id"]
        assert body["team_id"] is None

        # /api/admin/* is admin-gated at the router level; bob can't read it.
        login(client, "admin", "rotated-pass-456")
        audit = client.get("/api/admin/audit?action=case.import")
        assert audit.status_code == 200
        entries = audit.json()["audit"]
        assert any(e["action"] == "case.import" for e in entries)

    def test_garbage_upload_fails_job_not_server(self, client, admin_bootstrap):
        as_admin(client, admin_bootstrap)
        resp = client.post(
            "/api/cases/import",
            files={"file": ("junk.vestigo", b"not a zip at all", "application/zip")},
        )
        assert resp.status_code == 202, resp.text
        job = _job_terminal(client, resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "not a zip" in job["error"]
