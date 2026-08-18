"""End to end on real Postgres+ClickHouse with a fake model.

generate → sample run → validate → (repair) → full run → ingest, plus reuse
and regeneration. The model is a stub returning the fixture converter; the
subprocess, the validator, the Parquet registration and the ClickHouse ingest
are all real.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import pytest_asyncio

from tests.conftest import _fake_user
from vestigo.api import deps
from vestigo.converters import generator as G
from vestigo.converters import job as J
from vestigo.converters.job import ConvertJobInputs, run_convert_ingest_job
from vestigo.core.config import get_settings
from vestigo.core.jobs import JobStore
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.postgres import PostgresStore
from vestigo.ingestion.files import hash_file

FIX = Path("tests/fixtures/converters")
GOOD = (FIX / "syslog_fixture_converter.py").read_text()


@pytest_asyncio.fixture()
async def store(pg_database: str, monkeypatch) -> PostgresStore:
    s = PostgresStore(url=pg_database)
    monkeypatch.setattr(deps, "_store", s)
    monkeypatch.setenv("VESTIGO_CONVERTER_GENERATION_ENABLED", "1")
    get_settings.cache_clear()
    yield s
    get_settings.cache_clear()


@pytest.fixture()
def clickhouse():
    ch = ClickHouseStore()
    ch.init_schema()
    return ch


def _fake_generator(scripts: list[str], calls: list):
    """Return successive scripts per call; version placeholder taken from the task text."""

    async def gen(system, task, *, timeout_s=180.0):
        calls.append(task)
        version = re.search(r'= "(\d+)\.0\.0"', task).group(1)
        script = scripts[min(len(calls) - 1, len(scripts) - 1)]
        return G.GeneratedScript(
            name="syslog2vestigo",
            artifact="syslog",
            script=script.replace("__CONVERTER_VERSION__", f"{version}.0.0"),
            model="test-model",
            provider_endpoint="http://x/v1",
            prompt_hash="ph",
        )

    return gen


def _inputs(case_id: str, tmp_path: Path, name: str = "auth.log") -> ConvertJobInputs:
    raw = tmp_path / name
    shutil.copy(FIX / "sample.syslog", raw)
    return ConvertJobInputs(
        case_id=case_id,
        user=_fake_user(),
        raw_tmp_path=raw,
        raw_hash=hash_file(raw),
        raw_size=raw.stat().st_size,
        filename=name,
    )


async def _run(store, case_id, inputs):
    jobs = JobStore()
    job = jobs.create(kind="convert_ingest", case_id=case_id)
    await run_convert_ingest_job(job.id, inputs, job_store=jobs)
    return jobs.get(job.id)


@pytest.mark.asyncio
async def test_happy_path_ingests_and_keeps_script(store, clickhouse, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(J, "generate_script", _fake_generator([GOOD], calls))
    case = await store.create_case("c", "d")
    job = await _run(store, case.id, _inputs(case.id, tmp_path))
    assert job.status == "completed", job.error
    assert len(calls) == 1
    sid = job.result["source_id"]
    src = await store.get_source(case.id, sid)
    assert src.status == "ready" and src.parser == "syslog2vestigo@1.0.0"
    assert src.event_count == 12
    script = await store.get_converter_script(case.id, job.result["converter_script_id"])
    assert script.status == "working" and script.version == 1
    assert src.converter_script_id == script.id
    assert [a["phase"] for a in script.attempts] == ["sample", "full"]
    assert all(a["validation"]["ok"] for a in script.attempts)
    assert "Accepted publickey" in script.sample_excerpt
    assert clickhouse.count_events(case.id, source_id=sid) == 12
    clickhouse.delete_source_events(case.id, sid)


@pytest.mark.asyncio
async def test_repair_round_after_bad_footer(store, clickhouse, tmp_path, monkeypatch):
    bad = GOOD.replace('"vestigo.format_version": "1",', "")
    calls: list[str] = []
    monkeypatch.setattr(J, "generate_script", _fake_generator([bad, GOOD], calls))
    case = await store.create_case("c", "d")
    job = await _run(store, case.id, _inputs(case.id, tmp_path))
    assert job.status == "completed", job.error
    assert len(calls) == 2 and "PREVIOUS SCRIPT" in calls[1] and "format_version" in calls[1]
    script = await store.get_converter_script(case.id, job.result["converter_script_id"])
    assert [a["n"] for a in script.attempts][:2] == [1, 2]
    assert script.attempts[0]["validation"]["ok"] is False
    clickhouse.delete_source_events(case.id, job.result["source_id"])


@pytest.mark.asyncio
async def test_exhausted_attempts_fail_and_keep_draft(store, tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGO_CONVERTER_MAX_ATTEMPTS", "2")
    get_settings.cache_clear()
    calls: list[str] = []
    monkeypatch.setattr(J, "generate_script", _fake_generator(["import sys\nsys.exit(1)\n"], calls))
    case = await store.create_case("c", "d")
    job = await _run(store, case.id, _inputs(case.id, tmp_path))
    assert job.status == "failed" and len(calls) == 2
    script = await store.get_converter_script(case.id, job.progress["converter_script_id"])
    assert script.status == "failed" and script.source_code.startswith("import sys")
    assert await store.list_sources(case.id) == []


@pytest.mark.asyncio
async def test_denied_import_costs_an_attempt(store, clickhouse, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        J, "generate_script", _fake_generator(["import socket\n" + GOOD, GOOD], calls)
    )
    case = await store.create_case("c", "d")
    job = await _run(store, case.id, _inputs(case.id, tmp_path))
    assert job.status == "completed" and len(calls) == 2
    assert "not allowed" in calls[1]
    clickhouse.delete_source_events(case.id, job.result["source_id"])


@pytest.mark.asyncio
async def test_reuse_skips_the_model(store, clickhouse, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(J, "generate_script", _fake_generator([GOOD], calls))
    case = await store.create_case("c", "d")
    j1 = await _run(store, case.id, _inputs(case.id, tmp_path))
    assert j1.status == "completed", j1.error
    sid = j1.result["converter_script_id"]

    async def never(*a, **k):
        raise AssertionError("model must not be called on reuse")

    monkeypatch.setattr(J, "generate_script", never)
    raw2 = tmp_path / "auth2.log"
    raw2.write_text((FIX / "sample.syslog").read_text().replace("web01", "web02"))
    inp = ConvertJobInputs(
        case_id=case.id,
        user=_fake_user(),
        raw_tmp_path=raw2,
        raw_hash=hash_file(raw2),
        raw_size=raw2.stat().st_size,
        filename="auth2.log",
        reuse_script_id=sid,
    )
    j2 = await _run(store, case.id, inp)
    assert j2.status == "completed", j2.error
    assert j2.result["converter_script_id"] == sid
    assert (await store.count_sources_by_converter(case.id))[sid] == 2
    # The raw input's hash is on the source, so a repeat of this exact
    # (script, raw file) pair is refusable before any work.
    src2 = await store.get_source(case.id, j2.result["source_id"])
    assert src2.converter_input_hash == inp.raw_hash
    assert (await store.get_source_by_converter_input(case.id, sid, inp.raw_hash)).id == src2.id
    for j in (j1, j2):
        clickhouse.delete_source_events(case.id, j.result["source_id"])


@pytest.mark.asyncio
async def test_regenerate_bumps_version_and_enforces_footer(
    store, clickhouse, tmp_path, monkeypatch
):
    calls: list[str] = []
    monkeypatch.setattr(J, "generate_script", _fake_generator([GOOD], calls))
    case = await store.create_case("c", "d")
    j1 = await _run(store, case.id, _inputs(case.id, tmp_path))
    assert j1.status == "completed", j1.error
    parent = j1.result["converter_script_id"]
    inp = _inputs(case.id, tmp_path)
    inp.parent_id = parent
    inp.name_hint = "syslog2vestigo"
    inp.hint = "same format"
    j2 = await _run(store, case.id, inp)
    assert j2.status == "completed", j2.error
    s2 = await store.get_converter_script(case.id, j2.result["converter_script_id"])
    assert s2.version == 2 and s2.parent_id == parent and s2.hint == "same format"
    assert len(calls) == 2 and '"2.0.0"' in calls[1]
    for j in (j1, j2):
        clickhouse.delete_source_events(case.id, j.result["source_id"])


@pytest.mark.asyncio
async def test_gz_upload_sees_the_same_name_in_both_phases(
    store, clickhouse, tmp_path, monkeypatch
):
    import gzip

    calls: list[str] = []
    monkeypatch.setattr(J, "generate_script", _fake_generator([GOOD], calls))
    case = await store.create_case("c", "d")
    raw = tmp_path / "auth.log.gz"
    raw.write_bytes(gzip.compress((FIX / "sample.syslog").read_bytes()))
    inp = ConvertJobInputs(
        case_id=case.id,
        user=_fake_user(),
        raw_tmp_path=raw,
        raw_hash=hash_file(raw),
        raw_size=raw.stat().st_size,
        filename="auth.log.gz",
    )
    job = await _run(store, case.id, inp)
    # The fixture handles .gz by suffix; one draft passes both the sample and
    # the full run only if both phases stage the file as auth.log.gz.
    assert job.status == "completed", job.error
    assert len(calls) == 1
    sid = job.result["source_id"]
    rows, _ = clickhouse.list_events(case.id, sid, limit=5)
    # source_file names the evidence file, not the retention hash.
    assert rows and all(r["source_file"] == "auth.log.gz" for r in rows)
    clickhouse.delete_source_events(case.id, sid)


@pytest.mark.asyncio
async def test_footer_name_must_match_the_row(store, clickhouse, tmp_path, monkeypatch):
    renamed = GOOD.replace('CONVERTER_NAME = "syslog2vestigo"', 'CONVERTER_NAME = "other2vestigo"')
    calls: list[str] = []
    monkeypatch.setattr(J, "generate_script", _fake_generator([renamed, GOOD], calls))
    case = await store.create_case("c", "d")
    job = await _run(store, case.id, _inputs(case.id, tmp_path))
    assert job.status == "completed", job.error
    assert len(calls) == 2 and "converter_name" in calls[1]
    src = await store.get_source(case.id, job.result["source_id"])
    assert src.parser == "syslog2vestigo@1.0.0"
    clickhouse.delete_source_events(case.id, job.result["source_id"])


@pytest.mark.asyncio
async def test_fresh_generation_of_an_existing_name_redrafts_with_the_real_version(
    store, clickhouse, tmp_path, monkeypatch
):
    calls: list[str] = []
    monkeypatch.setattr(J, "generate_script", _fake_generator([GOOD], calls))
    case = await store.create_case("c", "d")
    j1 = await _run(store, case.id, _inputs(case.id, tmp_path))
    assert j1.status == "completed", j1.error
    raw2 = tmp_path / "auth2.log"
    raw2.write_text((FIX / "sample.syslog").read_text().replace("web01", "web02"))
    inp = ConvertJobInputs(
        case_id=case.id,
        user=_fake_user(),
        raw_tmp_path=raw2,
        raw_hash=hash_file(raw2),
        raw_size=raw2.stat().st_size,
        filename="auth2.log",
    )
    j2 = await _run(store, case.id, inp)
    assert j2.status == "completed", j2.error
    # First draft declared 1.0.0 (name unknown), the redraft was told v2 by name.
    assert len(calls) == 3 and '"2.0.0"' in calls[2] and "syslog2vestigo" in calls[2]
    s2 = await store.get_converter_script(case.id, j2.result["converter_script_id"])
    assert s2.version == 2 and s2.status == "working"
    # The discarded draft is on the record and did not cost an attempt.
    assert [a["phase"] for a in s2.attempts] == ["generate", "sample", "full"]
    assert "regenerating" in s2.attempts[0]["error"]
    for j in (j1, j2):
        clickhouse.delete_source_events(case.id, j.result["source_id"])


@pytest.mark.asyncio
async def test_ingest_failure_is_on_the_converter_trail(store, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(J, "generate_script", _fake_generator([GOOD], calls))
    from vestigo.ingestion import pipeline as P

    def boom(self, *a, **k):
        raise RuntimeError("clickhouse is down")

    monkeypatch.setattr(P.IngestionPipeline, "run", boom)
    case = await store.create_case("c", "d")
    job = await _run(store, case.id, _inputs(case.id, tmp_path))
    assert job.status == "failed" and "clickhouse is down" in (job.error or "")
    script = await store.get_converter_script(case.id, job.progress["converter_script_id"])
    # The converter itself passed; the trail says its product never landed.
    assert script.status == "working"
    assert [a["phase"] for a in script.attempts] == ["sample", "full", "ingest"]
    assert "clickhouse is down" in script.attempts[-1]["error"]
    assert await store.list_sources(case.id) == []


def test_traversal_filename_is_reduced_to_a_basename(tmp_path):
    inp = ConvertJobInputs(
        case_id="c",
        user=_fake_user(),
        raw_tmp_path=tmp_path / "x",
        raw_hash="0" * 64,
        raw_size=1,
        filename="../../../../home/app/.bashrc",
    )
    assert inp.filename == ".bashrc"


@pytest.mark.asyncio
async def test_model_going_away_mid_loop_marks_the_row_failed(store, tmp_path, monkeypatch):
    # Attempt 1 creates the row and fails its sample run; attempt 2 finds the
    # endpoint gone. GenerationUnavailable is re-raised past the exhaustion
    # branch — the row must still not stay 'generating' forever.
    calls: list[str] = []
    first = _fake_generator(["import sys\nsys.exit(1)\n"], calls)

    async def gen(system, task, *, timeout_s=180.0):
        if calls:
            raise G.GenerationUnavailable("endpoint down")
        return await first(system, task, timeout_s=timeout_s)

    monkeypatch.setattr(J, "generate_script", gen)
    case = await store.create_case("c", "d")
    job = await _run(store, case.id, _inputs(case.id, tmp_path))
    assert job.status == "failed" and "endpoint down" in (job.error or "")
    script = await store.get_converter_script(case.id, job.progress["converter_script_id"])
    assert script.status == "failed"
