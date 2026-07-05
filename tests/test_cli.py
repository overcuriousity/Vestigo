"""Tests for the `tsig` CLI: case listing, case/user validation on ingest,
and the ScalarForensic-style progress widget's math.

Test bodies are sync (not `async def`): the CLI commands themselves call
`asyncio.run()` internally, which cannot be nested inside a running event
loop — so these tests build the SQLite store and seed fixtures via a small
`run_async()` helper rather than via an async test function.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracesignal.cli import main as cli_main
from tracesignal.cli.progress import BytesProgressPrinter
from tracesignal.core.eta import ETATracker
from tracesignal.db.postgres import PostgresStore, generate_id
from tracesignal.ingestion.pipeline import IngestionResult

runner = CliRunner()


def run_async(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """In-memory SQLite store wired into the CLI's _get_store()."""
    db_path = tmp_path / "test_cli.db"
    s = PostgresStore(url=f"sqlite+aiosqlite:///{db_path}")
    run_async(s.init_schema())
    monkeypatch.setattr(cli_main, "_get_store", lambda: s)
    yield s
    run_async(s.engine.dispose())


class FakeIngestionPipeline:
    """Counts non-empty lines; mirrors tests/test_uploads.py's fake."""

    def __init__(
        self,
        case_id,
        source_id,
        clickhouse=None,
        batch_size=None,
        file_hash=None,
        source_name=None,
        progress_callback=None,
    ) -> None:
        self.case_id = case_id
        self.source_id = source_id
        self.progress_callback = progress_callback

    def run(self, path: Path, format_name: str | None = None) -> IngestionResult:
        data = path.read_bytes()
        if self.progress_callback is not None:
            self.progress_callback(total=len(data), processed=0)
            self.progress_callback(total=len(data), processed=len(data))
        lines = [line for line in data.split(b"\n") if line.strip()]
        return IngestionResult(
            case_id=self.case_id,
            source_id=self.source_id,
            files=[path],
            events_parsed=len(lines),
            events_inserted=len(lines),
        )


@pytest.fixture(autouse=True)
def _patch_pipeline(monkeypatch):
    monkeypatch.setattr(cli_main, "IngestionPipeline", FakeIngestionPipeline)


def _make_admin(store: PostgresStore, username: str = "admin"):
    return run_async(
        store.create_user(
            user_id=generate_id(f"user-{username}"),
            username=username,
            password_hash="x",
            is_admin=True,
        )
    )


def _make_user(store: PostgresStore, username: str):
    return run_async(
        store.create_user(
            user_id=generate_id(f"user-{username}"), username=username, password_hash="x"
        )
    )


def _make_case(store: PostgresStore, name: str = "Case One", owner_id=None, team_id=None):
    case_id = generate_id(name)
    return run_async(
        store.create_case(case_id=case_id, name=name, owner_id=owner_id, team_id=team_id)
    )


def _make_team(store: PostgresStore, name: str = "Blue Team"):
    return run_async(store.create_team(generate_id(name), name=name))


def _list_sources(store: PostgresStore, case_id: str):
    return run_async(store.list_sources(case_id))


# --------------------------------------------------------------------------
# tsig cases list
# --------------------------------------------------------------------------


def test_cases_list_empty(store):
    result = runner.invoke(cli_main.app, ["cases", "list"])
    assert result.exit_code == 0
    assert "No cases found" in result.stdout


def test_cases_list_shows_owner_and_team(store):
    admin = _make_admin(store)
    team = _make_team(store)
    case = _make_case(store, name="Personal Case", owner_id=admin.id)
    team_case = _make_case(store, name="Team Case", team_id=team.id)

    result = runner.invoke(cli_main.app, ["cases", "list"])
    assert result.exit_code == 0
    assert case.id in result.stdout
    assert "admin" in result.stdout
    assert "— (personal)" in result.stdout
    assert team_case.id in result.stdout
    assert "Blue Team" in result.stdout


# --------------------------------------------------------------------------
# tsig ingest — case validation
# --------------------------------------------------------------------------


def test_ingest_unknown_case_rejected(store, tmp_path):
    _make_admin(store)
    source_file = tmp_path / "events.jsonl"
    source_file.write_text('{"a": 1}\n')

    result = runner.invoke(
        cli_main.app,
        ["ingest", str(source_file), "--case", "does-not-exist", "--source", "s1"],
    )
    assert result.exit_code != 0
    output = result.stdout + (result.stderr or "")
    assert "No case with id" in output


def test_ingest_success_sets_created_by_and_audit(store, tmp_path):
    admin = _make_admin(store)
    case = _make_case(store, owner_id=admin.id)
    source_file = tmp_path / "events.jsonl"
    source_file.write_text('{"a": 1}\n{"a": 2}\n')

    result = runner.invoke(
        cli_main.app,
        ["ingest", str(source_file), "--case", case.id, "--source", "s1"],
    )
    assert result.exit_code == 0, result.stdout

    sources = _list_sources(store, case.id)
    assert len(sources) == 1
    assert sources[0].created_by == admin.id
    assert sources[0].event_count == 2


# --------------------------------------------------------------------------
# tsig ingest — user attribution
# --------------------------------------------------------------------------


def test_ingest_requires_user_when_zero_admins(store, tmp_path):
    case = _make_case(store)
    source_file = tmp_path / "events.jsonl"
    source_file.write_text('{"a": 1}\n')

    result = runner.invoke(
        cli_main.app,
        ["ingest", str(source_file), "--case", case.id, "--source", "s1"],
    )
    assert result.exit_code != 0
    output = result.stdout + (result.stderr or "")
    assert "--user required" in output


def test_ingest_requires_user_when_multiple_admins(store, tmp_path):
    _make_admin(store, "admin1")
    _make_admin(store, "admin2")
    case = _make_case(store)
    source_file = tmp_path / "events.jsonl"
    source_file.write_text('{"a": 1}\n')

    result = runner.invoke(
        cli_main.app,
        ["ingest", str(source_file), "--case", case.id, "--source", "s1"],
    )
    assert result.exit_code != 0
    output = result.stdout + (result.stderr or "")
    assert "--user required" in output


def test_ingest_explicit_user_used(store, tmp_path):
    _make_admin(store, "admin1")
    _make_admin(store, "admin2")
    analyst = _make_user(store, "analyst")
    case = _make_case(store)
    source_file = tmp_path / "events.jsonl"
    source_file.write_text('{"a": 1}\n')

    result = runner.invoke(
        cli_main.app,
        ["ingest", str(source_file), "--case", case.id, "--source", "s1", "--user", "analyst"],
    )
    assert result.exit_code == 0, result.stdout
    sources = _list_sources(store, case.id)
    assert sources[0].created_by == analyst.id


def test_ingest_unknown_user_rejected(store, tmp_path):
    _make_admin(store)
    case = _make_case(store)
    source_file = tmp_path / "events.jsonl"
    source_file.write_text('{"a": 1}\n')

    result = runner.invoke(
        cli_main.app,
        ["ingest", str(source_file), "--case", case.id, "--source", "s1", "--user", "ghost"],
    )
    assert result.exit_code != 0
    output = result.stdout + (result.stderr or "")
    assert "No active user" in output


# --------------------------------------------------------------------------
# Progress widget
# --------------------------------------------------------------------------


def test_eta_tracker_steady_state_gain_approaches_half():
    tracker = ETATracker()
    for _ in range(200):
        tracker.update(1000, 1.0)
    assert tracker.kalman_gain == pytest.approx(0.5, abs=0.05)


def test_eta_tracker_eta_none_before_two_updates():
    tracker = ETATracker()
    assert tracker.eta(1000) is None
    tracker.update(100, 1.0)
    assert tracker.eta(1000) is None
    tracker.update(100, 1.0)
    assert tracker.eta(1000) is not None


def test_bytes_progress_printer_advances_without_crashing():
    printer = BytesProgressPrinter()
    total = 10_000_000
    for processed in range(0, total + 1, 1_000_000):
        printer.on_progress(total=total, processed=processed)
    # No exception, and the meter picked up a rate estimate.
    assert printer._latest is not None
    assert printer._latest.rate_bps is not None
