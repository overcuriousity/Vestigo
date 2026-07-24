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
