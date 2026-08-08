"""Unified finding-disposition taxonomy: API CRUD, scope invariants, RBAC,
audit trail, and the reproducibility hash (see routers/dispositions.py and
db/postgres.py::FindingDisposition)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from tests.conftest import as_admin
from vestigo.db.postgres import FindingDisposition, dispositions_hash


def _setup_case(client) -> tuple[str, str]:
    case = client.post("/api/cases/", json={"name": "dispo-case"}).json()["case"]
    timelines = client.get(f"/api/cases/{case['id']}/timelines").json()["timelines"]
    return case["id"], timelines[0]["id"]


def _base(case_id: str, tl_id: str) -> str:
    return f"/api/cases/{case_id}/timelines/{tl_id}/dispositions"


def test_disposition_crud_and_idempotent_create(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    body = {
        "kind": "normal",
        "detector": "value_novelty",
        "field": "attr:user",
        "value": "svc_backup",
        "note": "known service account",
    }
    row = client.post(base, json=body).json()["disposition"]
    assert row["kind"] == "normal"
    assert row["timeline_id"] == tl_id

    # Identical declaration is idempotent — same row, no duplicate.
    again = client.post(base, json=body).json()["disposition"]
    assert again["id"] == row["id"]
    assert len(client.get(base).json()["dispositions"]) == 1

    resp = client.delete(f"{base}/{row['id']}")
    assert resp.status_code == 200
    assert client.get(base).json()["dispositions"] == []
    assert client.delete(f"{base}/{row['id']}").status_code == 404


def test_disposition_scope_invariants(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    # No scope at all.
    assert client.post(base, json={"kind": "normal"}).status_code == 422
    # Half a value scope.
    assert client.post(base, json={"kind": "normal", "field": "attr:x"}).status_code == 422
    # Half an event scope.
    assert client.post(base, json={"kind": "dismissed", "event_id": "e1"}).status_code == 422
    # Both scopes at once.
    assert (
        client.post(
            base,
            json={
                "kind": "normal",
                "field": "attr:x",
                "value": "v",
                "source_id": "s1",
                "event_id": "e1",
            },
        ).status_code
        == 422
    )
    # confirmed requires event scope…
    assert (
        client.post(
            base,
            json={"kind": "confirmed", "detector": "charset", "field": "attr:x", "value": "v"},
        ).status_code
        == 422
    )
    # …and a concrete detector.
    assert (
        client.post(
            base, json={"kind": "confirmed", "source_id": "s1", "event_id": "e1"}
        ).status_code
        == 422
    )
    # Unknown kind rejected by the model.
    assert (
        client.post(base, json={"kind": "meh", "field": "attr:x", "value": "v"}).status_code == 422
    )


def test_routine_disposition_invariants_and_create(client, admin_bootstrap, store, monkeypatch):
    """routine requires value scope, detector=sequence_motif and details.values;
    a valid create returns the row plus a materialization job id."""
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    # Event scope rejected.
    assert (
        client.post(
            base,
            json={
                "kind": "routine",
                "detector": "sequence_motif",
                "source_id": "s1",
                "event_id": "e1",
            },
        ).status_code
        == 422
    )
    # Wrong detector rejected.
    assert (
        client.post(
            base,
            json={
                "kind": "routine",
                "detector": "value_novelty",
                "field": "artifact",
                "value": "a → b",
                "details": {"values": ["a", "b"]},
            },
        ).status_code
        == 422
    )
    # Missing details.values rejected.
    assert (
        client.post(
            base,
            json={
                "kind": "routine",
                "detector": "sequence_motif",
                "field": "artifact",
                "value": "a → b",
            },
        ).status_code
        == 422
    )
    # Non-string / empty elements in details.values rejected.
    for bad_values in (["a", 2], ["a", ""], ["a", None]):
        assert (
            client.post(
                base,
                json={
                    "kind": "routine",
                    "detector": "sequence_motif",
                    "field": "artifact",
                    "value": "a → b",
                    "details": {"values": bad_values},
                },
            ).status_code
            == 422
        )
    # Present-but-malformed mining scope rejected — a bad snapshot must never
    # silently degrade to an unscoped (wider) collapse.
    for bad_scope in ({"scope_start": "not-a-date"}, {"scope_end": 12345}):
        assert (
            client.post(
                base,
                json={
                    "kind": "routine",
                    "detector": "sequence_motif",
                    "field": "artifact",
                    "value": "a → b",
                    "details": {"values": ["a", "b"], **bad_scope},
                },
            ).status_code
            == 422
        )

    # value disagreeing with details.values rejected — the displayed motif
    # and the materialized occurrences must describe the same pattern.
    assert (
        client.post(
            base,
            json={
                "kind": "routine",
                "detector": "sequence_motif",
                "field": "artifact",
                "value": "a → b",
                "details": {"values": ["a", "c"]},
            },
        ).status_code
        == 422
    )

    # Valid create: row persisted, background materialization scheduled.
    from vestigo.api.routers import dispositions as dispo_module

    materialize_calls: list[tuple] = []
    monkeypatch.setattr(
        dispo_module,
        "_run_motif_materialization_job",
        lambda *args: materialize_calls.append(args),
    )
    resp = client.post(
        base,
        json={
            "kind": "routine",
            "detector": "sequence_motif",
            "field": "artifact",
            "value": "a → b → c",
            "details": {"values": ["a", "b", "c"], "n": 3, "support": 47},
        },
    ).json()
    row = resp["disposition"]
    assert row["kind"] == "routine"
    assert row["detector"] == "sequence_motif"
    assert resp["materialization_job_id"]
    assert len(materialize_calls) == 1
    # Job args: (job_id, case_id, source_ids, series_field, values,
    # disposition_id, start, end, ...). Unscoped mining → unscoped collapse.
    args = materialize_calls[0]
    assert args[1] == case_id
    assert args[3] == "artifact"
    assert args[4] == ["a", "b", "c"]
    assert args[5] == row["id"]
    assert args[6] is None and args[7] is None

    # A time-scoped mined motif hands its frame to the materialization job,
    # so the collapse covers exactly what the analyst saw mined.
    client.post(
        base,
        json={
            "kind": "routine",
            "detector": "sequence_motif",
            "field": "artifact",
            "value": "x → y",
            "details": {
                "values": ["x", "y"],
                "scope_start": "2026-07-01T00:00:00+00:00",
                "scope_end": "2026-07-02T00:00:00+00:00",
            },
        },
    )
    assert len(materialize_calls) == 2
    args = materialize_calls[1]
    assert args[6] == datetime.fromisoformat("2026-07-01T00:00:00+00:00")
    assert args[7] == datetime.fromisoformat("2026-07-02T00:00:00+00:00")

    # Listable by kind.
    routine = client.get(base, params={"kind": "routine"}).json()["dispositions"]
    assert {d["id"] for d in routine} == {row["id"], materialize_calls[1][5]}


def test_log_template_routine_disposition_invariants_and_create(
    client, admin_bootstrap, store, monkeypatch
):
    """log_template routine (W6): requires field='template_id', a decimal
    value, and details.template/template_version — and, unlike
    sequence_motif, schedules NO materialization job (membership is a plain
    column predicate, no aux table)."""
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    # Wrong field rejected.
    assert (
        client.post(
            base,
            json={
                "kind": "routine",
                "detector": "log_template",
                "field": "artifact",
                "value": "12345",
                "details": {"template": "Allow TCP <IP>", "template_version": 1},
            },
        ).status_code
        == 422
    )
    # Non-decimal value rejected.
    assert (
        client.post(
            base,
            json={
                "kind": "routine",
                "detector": "log_template",
                "field": "template_id",
                "value": "not-a-number",
                "details": {"template": "Allow TCP <IP>", "template_version": 1},
            },
        ).status_code
        == 422
    )
    # Missing details.template rejected.
    assert (
        client.post(
            base,
            json={
                "kind": "routine",
                "detector": "log_template",
                "field": "template_id",
                "value": "12345",
                "details": {"template_version": 1},
            },
        ).status_code
        == 422
    )
    # Wrong template_version rejected.
    assert (
        client.post(
            base,
            json={
                "kind": "routine",
                "detector": "log_template",
                "field": "template_id",
                "value": "12345",
                "details": {"template": "Allow TCP <IP>", "template_version": 2},
            },
        ).status_code
        == 422
    )
    # A template mined over a non-message field is rejected: the collapse
    # predicate only knows the materialized message hash, so such a row would
    # collapse events it does not describe.
    assert (
        client.post(
            base,
            json={
                "kind": "routine",
                "detector": "log_template",
                "field": "template_id",
                "value": "12345",
                "details": {
                    "template": "<TS> <IP> GET /x",
                    "template_version": 1,
                    "field": "attr:raw_line",
                },
            },
        ).status_code
        == 422
    )
    # Event scope rejected — log_template is value-scoped only.
    assert (
        client.post(
            base,
            json={
                "kind": "routine",
                "detector": "log_template",
                "source_id": "s1",
                "event_id": "e1",
            },
        ).status_code
        == 422
    )

    from vestigo.api.routers import dispositions as dispo_module

    materialize_calls: list[tuple] = []
    monkeypatch.setattr(
        dispo_module,
        "_run_motif_materialization_job",
        lambda *args: materialize_calls.append(args),
    )
    resp = client.post(
        base,
        json={
            "kind": "routine",
            "detector": "log_template",
            "field": "template_id",
            "value": "987654321",
            "details": {
                "template": "Allow TCP <IP>:<NUM> -> <IP>:<NUM>",
                "template_version": 1,
                "field": "message",
                "example": "Allow TCP 10.0.0.5:4433 -> 10.0.0.9:443",
                "count_at_mute": 3,
            },
        },
    ).json()
    row = resp["disposition"]
    assert row["kind"] == "routine"
    assert row["detector"] == "log_template"
    assert row["value"] == "987654321"
    # No occurrence-materialization job — membership is a column predicate.
    assert "materialization_job_id" not in resp
    assert materialize_calls == []


def test_dispositions_hash_ignores_routine():
    """routine is presentation-only — never enters the reproducibility hash."""
    value_normal = FindingDisposition(
        id="d1", case_id="c", kind="normal", detector="charset", field="f", value="v"
    )
    routine = FindingDisposition(
        id="d2",
        case_id="c",
        kind="routine",
        detector="sequence_motif",
        field="artifact",
        value="a → b → c",
    )
    assert dispositions_hash([value_normal, routine]) == dispositions_hash([value_normal])


def test_event_scoped_rows_have_no_timeline_and_list_by_source(client, admin_bootstrap, store):
    """Event-scoped rows carry timeline_id=NULL; the list endpoint surfaces
    them for a timeline via its sources."""
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    row = client.post(
        base,
        json={
            "kind": "dismissed",
            "detector": "timestamp_order",
            "source_id": "s-none",
            "event_id": "e1",
        },
    ).json()["disposition"]
    assert row["timeline_id"] is None
    # Not listed: source "s-none" isn't attached to the timeline.
    assert client.get(base).json()["dispositions"] == []


def test_list_filters_by_kind_and_detector(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    client.post(base, json={"kind": "normal", "detector": "charset", "field": "f", "value": "v1"})
    client.post(
        base, json={"kind": "dismissed", "detector": "entropy", "field": "f", "value": "v2"}
    )
    client.post(base, json={"kind": "normal", "detector": "*", "field": "f", "value": "v3"})

    assert len(client.get(base).json()["dispositions"]) == 3
    assert len(client.get(base, params={"kind": "normal"}).json()["dispositions"]) == 2
    # detector filter matches concrete + wildcard rows.
    charset = client.get(base, params={"detector": "charset"}).json()["dispositions"]
    assert {d["value"] for d in charset} == {"v1", "v3"}
    assert client.get(base, params={"kind": "nope"}).status_code == 422


def test_disposition_mutations_are_audited(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    row = client.post(
        base, json={"kind": "normal", "detector": "charset", "field": "f", "value": "v"}
    ).json()["disposition"]
    bulk = client.post(
        f"{base}/bulk",
        json={
            "items": [
                {"kind": "dismissed", "detector": "entropy", "field": "f", "value": "v2"},
                {"kind": "dismissed", "detector": "entropy", "field": "f", "value": "v3"},
            ]
        },
    ).json()["dispositions"]
    assert len(bulk) == 2
    client.delete(f"{base}/{row['id']}")

    audit = client.get("/api/admin/audit", params={"action": "disposition.create"}).json()
    assert any(r["target_id"] == row["id"] for r in audit["audit"])
    audit = client.get("/api/admin/audit", params={"action": "disposition.bulk_create"}).json()
    assert any(r["detail"]["count"] == 2 for r in audit["audit"])
    audit = client.get("/api/admin/audit", params={"action": "disposition.delete"}).json()
    assert any(r["target_id"] == row["id"] for r in audit["audit"])


def test_bulk_validates_everything_before_writing(client, admin_bootstrap, store):
    """A bulk request with one invalid item writes nothing."""
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    resp = client.post(
        f"{base}/bulk",
        json={
            "items": [
                {"kind": "normal", "detector": "charset", "field": "f", "value": "ok"},
                {"kind": "normal", "field": "half-scope-only"},
            ]
        },
    )
    assert resp.status_code == 422
    assert client.get(base).json()["dispositions"] == []


@pytest.mark.asyncio
async def test_update_disposition_details_merges_without_clobbering(store):
    await store.init_schema()
    row = await store.create_disposition(
        case_id="c1",
        kind="routine",
        detector="sequence_motif",
        timeline_id="t1",
        field="artifact",
        value="a → b",
        details={"values": ["a", "b"], "scope_start": "2026-07-01T00:00:00+00:00"},
    )
    ok = await store.update_disposition_details(
        "c1", row.id, {"materialization": {"status": "completed", "rows_written": 4}}
    )
    assert ok is True
    rows = await store.list_dispositions("c1", timeline_id="t1")
    details = rows[0].details
    # Patch merged; pre-existing keys (the collapse contract) untouched.
    assert details["materialization"] == {"status": "completed", "rows_written": 4}
    assert details["values"] == ["a", "b"]
    assert details["scope_start"] == "2026-07-01T00:00:00+00:00"
    # Unknown row → False, no error.
    assert await store.update_disposition_details("c1", "nope", {"x": 1}) is False


def test_materialization_outcome_persisted_on_disposition(
    client, admin_bootstrap, store, monkeypatch
):
    """The background job writes its outcome (incl. cap warnings / failure)
    into details.materialization — durable, unlike the in-memory job result."""
    from vestigo.api.routers import dispositions as dispo_module
    from vestigo.core.jobs import get_job_store
    from vestigo.db.postgres import PostgresStore

    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    # The job helper opens a fresh store (thread/loop isolation); point it at
    # the test database instead of the configured Postgres.
    # `str(URL)` masks the password as `***` — fine against SQLite, which has
    # none, and an authentication failure against the real PostgreSQL. The
    # helper swallows its own exceptions, so that failure would surface only as
    # a missing key three lines below.
    test_url = store.engine.url.render_as_string(hide_password=False)
    monkeypatch.setattr(
        "vestigo.db.postgres.PostgresStore",
        lambda url=None: PostgresStore(url=test_url),
    )

    class _FakeSvc:
        def __init__(self, result=None, error=None):
            self.result, self.error = result, error

        def resolve_motif_occurrences(self, **kwargs):
            if self.error:
                raise RuntimeError(self.error)
            return self.result

    row = client.post(
        base,
        json={
            "kind": "routine",
            "detector": "sequence_motif",
            "field": "artifact",
            "value": "a → b",
            "details": {"values": ["a", "b"]},
        },
    ).json()["disposition"]

    # Completed with a cap warning.
    monkeypatch.setattr(
        "vestigo.api.routers.events._get_stat_anomaly_service",
        lambda: _FakeSvc(result=(4, ["partial collapse"])),
    )
    job = get_job_store().create(kind="motif_materialize", case_id=case_id)
    dispo_module._run_motif_materialization_job(
        job.id,
        case_id,
        ["s1"],
        "artifact",
        ["a", "b"],
        row["id"],
        None,
        None,
        None,
        None,
        get_job_store(),
    )
    stored = client.get(base, params={"kind": "routine"}).json()["dispositions"][0]
    mat = stored["details"]["materialization"]
    assert mat == {"status": "completed", "rows_written": 4, "warnings": ["partial collapse"]}

    # Failure path overwrites with status=failed.
    monkeypatch.setattr(
        "vestigo.api.routers.events._get_stat_anomaly_service",
        lambda: _FakeSvc(error="clickhouse down"),
    )
    job2 = get_job_store().create(kind="motif_materialize", case_id=case_id)
    dispo_module._run_motif_materialization_job(
        job2.id,
        case_id,
        ["s1"],
        "artifact",
        ["a", "b"],
        row["id"],
        None,
        None,
        None,
        None,
        get_job_store(),
    )
    stored = client.get(base, params={"kind": "routine"}).json()["dispositions"][0]
    assert stored["details"]["materialization"] == {"status": "failed", "error": "clickhouse down"}
    assert stored["details"]["values"] == ["a", "b"]


async def _backdate(store, disposition_id: str, day: str) -> None:
    """Set a disposition's created_at to noon UTC of *day* (API-created rows
    all land on "today"; the stats tests need rows spread across days)."""
    created = datetime.fromisoformat(f"{day}T12:00:00").replace(tzinfo=UTC)
    async with store.session_factory() as session:
        await session.execute(
            update(FindingDisposition)
            .where(FindingDisposition.id == disposition_id)
            .values(created_at=created)
        )
        await session.commit()


async def test_disposition_stats_counts_by_day_and_kind(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    rows = [
        ("normal", "v1", "2026-07-01"),
        ("dismissed", "v2", "2026-07-01"),
        ("dismissed", "v3", "2026-07-03"),
    ]
    for kind, value, day in rows:
        row = client.post(
            base, json={"kind": kind, "detector": "charset", "field": "f", "value": value}
        ).json()["disposition"]
        await _backdate(store, row["id"], day)
    # One row keeps its real (today's) timestamp.
    client.post(base, json={"kind": "normal", "detector": "*", "field": "f", "value": "v4"})

    stats = client.get(f"{base}/stats").json()
    days = stats["days"]
    assert [d["date"] for d in days[:2]] == ["2026-07-01", "2026-07-03"]
    assert len(days) == 3

    assert days[0]["normal"] == 1 and days[0]["dismissed"] == 1 and days[0]["total"] == 2
    assert days[1]["dismissed"] == 1 and days[1]["total"] == 1
    assert days[1]["cumulative"] == {
        "normal": 1,
        "dismissed": 2,
        "confirmed": 0,
        "routine": 0,
        "total": 3,
    }
    assert stats["totals"] == {
        "normal": 2,
        "dismissed": 2,
        "confirmed": 0,
        "routine": 0,
        "total": 4,
    }
    assert days[-1]["cumulative"] == stats["totals"]


def test_disposition_stats_scopes_to_timeline_and_404s(client, admin_bootstrap, store):
    """Event-scoped rows on sources outside the timeline are excluded."""
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    client.post(
        base,
        json={
            "kind": "dismissed",
            "detector": "timestamp_order",
            "source_id": "s-none",
            "event_id": "e1",
        },
    )
    client.post(base, json={"kind": "normal", "detector": "charset", "field": "f", "value": "v"})

    stats = client.get(f"{base}/stats").json()
    assert stats["totals"]["total"] == 1
    assert stats["totals"]["normal"] == 1

    resp = client.get(f"/api/cases/{case_id}/timelines/nope/dispositions/stats")
    assert resp.status_code == 404


def test_dispositions_hash_covers_normal_only_and_event_scope():
    """dismissed/confirmed never change the hash; event-scoped normals do —
    the reproducibility gap the old allowlist_hash left open."""
    value_normal = FindingDisposition(kind="normal", detector="charset", field="f", value="v")
    event_normal = FindingDisposition(kind="normal", detector="*", source_id="s1", event_id="e1")
    dismissed = FindingDisposition(kind="dismissed", detector="charset", field="f", value="x")
    confirmed = FindingDisposition(
        kind="confirmed", detector="charset", source_id="s1", event_id="e2"
    )

    base = dispositions_hash([value_normal])
    assert len(base) == 64
    # Deterministic and order-independent.
    assert dispositions_hash([event_normal, value_normal]) == dispositions_hash(
        [value_normal, event_normal]
    )
    # Presentation-only / escalation rows don't affect it.
    assert dispositions_hash([value_normal, dismissed, confirmed]) == base
    # An event-scoped normal DOES affect it.
    assert dispositions_hash([value_normal, event_normal]) != base


@pytest.mark.asyncio
async def test_store_create_disposition_idempotent(store):
    await store.init_schema()
    a = await store.create_disposition(
        case_id="c1", kind="normal", detector="charset", timeline_id="t1", field="f", value="v"
    )
    b = await store.create_disposition(
        case_id="c1", kind="normal", detector="charset", timeline_id="t1", field="f", value="v"
    )
    assert a.id == b.id
    # Same key with a different kind is a distinct verdict, not a duplicate.
    c = await store.create_disposition(
        case_id="c1", kind="dismissed", detector="charset", timeline_id="t1", field="f", value="v"
    )
    assert c.id != a.id
