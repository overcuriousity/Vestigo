# Case Export/Import (X1, `.vestigo` archive) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Full-fidelity export of a case into a single versioned `.vestigo` zip archive (Postgres state + ClickHouse events + optional source blobs) and restore as a new case on the same or a different instance — plus the bundled ClickHouse pytest-marker residue.

**Architecture:** New `src/vestigo/transfer/` package (`archive.py` = pure format code, `exporter.py`, `importer.py`) driven by two audited, RBAC-gated API endpoints that run work in JobStore background jobs. Events move as Arrow IPC via the existing `iter_source_events` / `insert_events_arrow` primitives; Postgres entities move as NDJSON produced by generic SQLAlchemy column serialization (no new store methods). Import always creates a new importer-owned case with full ID remapping and preserved event IDs.

**Spec:** `docs/superpowers/specs/2026-07-24-case-export-import-design.md` (committed). Two deliberate implementation-level deviations, to be synced into the spec in Task 9: (a) the Postgres snapshot uses direct ORM selects in `exporter.py` — no new `PostgresStore` methods; (b) the case-name suffix clause is dropped (case names are not unique-constrained; the imported case keeps the archived name verbatim).

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0 async (SQLite in tests, Postgres in prod), pyarrow, stdlib `zipfile`/`hashlib`/`json`; React + TanStack Query + zustand frontend.

## Global Constraints

- **No new runtime dependencies.** stdlib `zipfile`, `hashlib`, `json`, `tempfile`, `shutil`, `re` + already-present `pyarrow`, `sqlalchemy`, `fastapi`.
- Archive format per spec §2: `format_version: 1`, `manifest.json` with per-member SHA-256; NDJSON deflate-compressed, `.arrow`/`blobs/` members stored uncompressed.
- **Secrets never exported:** no `users`/`sessions`/`agent_tokens` rows, no `enricher_global_configs`, no password or token material anywhere in the archive.
- Export gate: `require_case_manage`; import gate: any authenticated user (becomes owner). Audit actions exactly `case.export` / `case.import` via `store.record_audit`.
- Restore is always as-new-case (no merge). Event IDs preserved verbatim. Imported case: `owner_id` = importer, `team_id` = None, timeline embedding columns reset to NULL.
- pytest `asyncio_mode = "auto"` — async test functions need no decorator. Reuse `store` / `client` / `login` / `as_admin` fixtures and helpers from `tests/conftest.py`.
- Test runner: `uv run pytest` (fallback: `.venv/bin/pytest`). Frontend: `npm run typecheck`, `npm run lint`, `npm run build`, `npm test` inside `frontend/`.
- Every test file that needs a live ClickHouse carries `pytestmark = pytest.mark.clickhouse` (registered in Task 1).
- Commits after every task; do not amend earlier tasks' commits.

## File Structure

| File | Responsibility |
|---|---|
| `src/vestigo/transfer/__init__.py` | package marker (docstring only) |
| `src/vestigo/transfer/archive.py` | `.vestigo` zip format: manifest, writer, reader, member hashing/verification, temp-root helpers |
| `src/vestigo/core/retention.py` | content-addressed source-blob path/retain helpers (moved out of `api/routers/cases.py`) |
| `src/vestigo/transfer/exporter.py` | Postgres snapshot (ORM selects) + event streaming + blob collection → archive |
| `src/vestigo/transfer/importer.py` | verification, ID remap, ordered ORM inserts, Arrow column rewrite, blob placement, stats recompute, failure cleanup |
| `src/vestigo/api/routers/transfer.py` | export/import endpoints, download endpoint, job runners |
| `src/vestigo/api/main.py` | register router; sweep stale transfer temp archives at startup |
| `src/vestigo/api/routers/cases.py` | import retention helpers from their new home (no behavior change) |
| `pyproject.toml` | register `clickhouse` pytest marker |
| `tests/test_transfer_archive.py` | format round-trip, tamper, version-gate tests |
| `tests/test_transfer_export.py` | snapshot completeness + archive assembly tests (fake ClickHouse) |
| `tests/test_transfer_import.py` | remap integrity, secrets scan, tamper abort, failure-cleanup tests (fake ClickHouse) |
| `tests/test_transfer_api.py` | endpoint RBAC/audit/job/download tests |
| `tests/test_transfer_roundtrip_clickhouse.py` | live-ClickHouse export→import event equality (marked) |
| `frontend/src/api/transfer.ts` | typed API helpers |
| `frontend/src/components/cases/ExportCaseDialog.tsx` | export button + include_blobs choice + job poll + download |
| `frontend/src/components/cases/ImportCaseDialog.tsx` | archive upload + job poll + navigate to new case |
| `frontend/src/components/cases/CaseCard.tsx` / `CaseList.tsx` | wire the two dialogs in |

---

### Task 1: Register and apply the `clickhouse` pytest marker

**Files:**
- Modify: `pyproject.toml` (`[tool.pytest.ini_options]`)
- Modify: `tests/test_arrow_insert_clickhouse.py`, `tests/test_clickhouse_store.py`, `tests/test_compare_baseline_cache_clickhouse.py`, `tests/test_field_mappings_clickhouse.py`, `tests/test_novelty_batched_clickhouse.py`, `tests/test_pagination_completeness_clickhouse.py`, `tests/test_search_blob_clickhouse.py`, `tests/test_template_clickhouse.py`, `tests/test_time_fields_clickhouse.py`, `tests/test_viz_stats_clickhouse.py`, `tests/test_viz_timeseries_fused_clickhouse.py`, `tests/test_field_stats.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the `clickhouse` marker used by Task 7's round-trip test; `-m clickhouse` / `-m "not clickhouse"` selection.

- [ ] **Step 1: Register the marker**

In `pyproject.toml`, change:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "--cov=vestigo --cov-report=term-missing"
```

to:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "--cov=vestigo --cov-report=term-missing"
markers = [
    "clickhouse: requires a reachable ClickHouse (dev compose stack); skipped otherwise",
]
```

- [ ] **Step 2: Apply `pytestmark` to all twelve files**

In each listed file, add immediately after the last import line:

```python
pytestmark = pytest.mark.clickhouse
```

(All twelve already `import pytest`. `tests/test_field_stats.py` belongs in this set: it seeds fixture events into a live ClickHouse in its `ch_store` module fixture.)

- [ ] **Step 3: Verify selection works**

Run: `uv run pytest --collect-only -q -m clickhouse 2>&1 | grep -c "::"`
Expected: a number equal to the total test count of the twelve files (only those files collected).

Run: `uv run pytest --collect-only -q -m "not clickhouse" 2>&1 | grep "clickhouse" | wc -l`
Expected: `0` (no `test_*_clickhouse` items collected).

- [ ] **Step 4: Run the suite without ClickHouse to confirm nothing broke**

Run: `uv run pytest -m "not clickhouse" -q`
Expected: PASS (the twelve files skip inside their fixtures exactly as before; marker changes collection only).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/test_arrow_insert_clickhouse.py tests/test_clickhouse_store.py tests/test_compare_baseline_cache_clickhouse.py tests/test_field_mappings_clickhouse.py tests/test_novelty_batched_clickhouse.py tests/test_pagination_completeness_clickhouse.py tests/test_search_blob_clickhouse.py tests/test_template_clickhouse.py tests/test_time_fields_clickhouse.py tests/test_viz_stats_clickhouse.py tests/test_viz_timeseries_fused_clickhouse.py tests/test_field_stats.py
git commit -m "test: register clickhouse marker and apply to all CH-dependent test files"
```

---

### Task 2: `transfer/archive.py` — the `.vestigo` format module

**Files:**
- Create: `src/vestigo/transfer/__init__.py`
- Create: `src/vestigo/transfer/archive.py`
- Test: `tests/test_transfer_archive.py`

**Interfaces:**
- Consumes: nothing from later tasks.
- Produces (used by Tasks 3–6): `FORMAT_VERSION: int`, `ArchiveFormatError`, `ArchiveWriter(path)`, `.add_bytes(arcname, data: bytes, *, compress=True)`, `.add_file(arcname, src: Path, *, compress=False)`, `.finish(manifest_core: dict)`, `ArchiveReader(path)`, `.manifest: dict`, `.verify_members()`, `.read_json(arcname)`, `.read_ndjson(arcname) -> list[dict]`, `.extract_to(arcname, dest: Path)`, `.member_names() -> list[str]`, `.close()`, `temp_root() -> Path`, `new_archive_path(job_id: str) -> Path`.

- [ ] **Step 1: Write the failing tests**

`tests/test_transfer_archive.py`:

```python
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

    tempfile.tempdir = None  # force re-evaluation of TMPDIR
    root = temp_root()
    assert root.is_dir()
    p = new_archive_path("job123")
    assert p.parent == root and p.name == "job123.vestigo"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transfer_archive.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vestigo.transfer'`

- [ ] **Step 3: Implement the module**

`src/vestigo/transfer/__init__.py`:

```python
"""Case portability (X1): .vestigo archive export/import."""
```

`src/vestigo/transfer/archive.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transfer_archive.py -q`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/vestigo/transfer/__init__.py src/vestigo/transfer/archive.py tests/test_transfer_archive.py
git commit -m "feat(transfer): .vestigo archive format reader/writer with member verification"
```

---

### Task 3: `transfer/exporter.py` — snapshot, events, blobs, archive assembly

**Files:**
- Create: `src/vestigo/core/retention.py`
- Modify: `src/vestigo/api/routers/cases.py` (import move only — delete the three helper defs at lines ~137–167, import them instead)
- Create: `tests/transfer_fakes.py` (shared test doubles: `_fill`, `_add`, `FakeClickHouse`, `_event_rows` — used by both transfer test files)
- Create: `src/vestigo/transfer/exporter.py`
- Test: `tests/test_transfer_export.py`

**Interfaces:**
- Consumes: Task 2's `ArchiveWriter`, `FORMAT_VERSION`, `new_archive_path`.
- Produces (used by Tasks 4–7): `ExportResult` dataclass (`path: Path`, `bytes: int`, `counts: dict[str, int]`, `warnings: list[str]`); `async def export_case(store, clickhouse_factory, case_id, *, include_blobs: bool, exported_by: str, dest_dir: Path, progress=None) -> ExportResult`; `retention_dir()`, `retention_path(file_hash)`, `retain_file(tmp, dest)` in `vestigo.core.retention`.

- [ ] **Step 1: Move the retention helpers (no behavior change)**

Create `src/vestigo/core/retention.py` with exactly the three helpers currently at `src/vestigo/api/routers/cases.py:137-167`, renamed to public names:

```python
"""Content-addressed retention of original source files (instance-global)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from vestigo.core.config import get_settings


def retention_dir() -> Path:
    """Return the directory used for content-addressed source file retention."""
    path = Path(get_settings().source_retention_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def retention_path(file_hash: str) -> Path:
    """Return the content-addressed path for a retained source file."""
    return retention_dir() / file_hash[:2] / file_hash


def retain_file(tmp_path: Path, retention_path_: Path) -> None:
    if retention_path_.exists():
        return
    retention_path_.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(tmp_path, retention_path_)
    except OSError:
        shutil.copy2(tmp_path, retention_path_)
```

In `src/vestigo/api/routers/cases.py`: delete the old `_retention_dir` / `_retention_path` / `_retain_file` defs, add `from vestigo.core.retention import retain_file as _retain_file, retention_path as _retention_path` to its imports, keep every call site unchanged (they call `_retention_path(...)` / `_retain_file(...)`).

Run: `uv run pytest tests/test_admin_api.py tests/test_agent_api.py -q` plus `uv run pytest -q -k source_upload`
Expected: PASS (import move breaks nothing).

- [ ] **Step 2: Write the shared test doubles + the failing exporter tests**

`tests/transfer_fakes.py` (imported by both transfer test files — single home for the fakes):

```python
"""Shared test doubles for the transfer (X1) test suite.

`_add` fills unknown non-nullable columns so tests survive model churn;
`FakeClickHouse` stands in for ClickHouseStore keyed by (case_id, source_id).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def _fill(col):
    """Value for a non-nullable column without a default, by Python type."""
    t = col.type.python_type
    if t is str:
        return "x"
    if t is int:
        return 0
    if t is bool:
        return False
    if t is dict:
        return {}
    if t is list:
        return []
    if t is datetime:
        return datetime.now(UTC)
    raise AssertionError(f"unhandled column type {col}")


async def _add(store, model, **overrides):
    values = {}
    for col in model.__table__.columns:
        if col.name in overrides:
            values[col.name] = overrides.pop(col.name)
        elif col.primary_key:
            values[col.name] = f"{model.__tablename__}-{uuid.uuid4().hex[:8]}"
        elif col.nullable or col.default is not None or col.server_default is not None:
            continue
        else:
            values[col.name] = _fill(col)
    assert not overrides, f"unknown columns for {model.__tablename__}: {overrides}"
    async with store.session_factory() as session:
        obj = model(**values)
        session.add(obj)
        await session.commit()
        await session.refresh(obj)
        return obj


class FakeClickHouse:
    """Stands in for ClickHouseStore; keyed by (case_id, source_id)."""

    def __init__(self, rows=None):
        self.rows = rows or {}
        self.inserted = {}
        self.deleted = []

    def iter_source_events(self, case_id, source_id, batch_size):
        rows = self.rows.get((case_id, source_id), [])
        for i in range(0, len(rows), batch_size):
            yield rows[i : i + batch_size]

    def insert_events_arrow(self, batch):
        for r in batch.to_pylist():
            self.inserted.setdefault((r["case_id"], r["source_id"]), []).append(r)
        return batch.num_rows

    def delete_source_events(self, case_id, source_id):
        self.deleted.append((case_id, source_id))


def _event_rows(case_id, source_id, n=2):
    return [
        {
            "event_id": uuid.uuid4(),
            "case_id": case_id,
            "source_id": source_id,
            "source_file": "demo.log",
            "byte_offset": i * 100,
            "line_number": i,
            "content_hash": (f"{i:064d}").encode(),
            "file_hash": b"ab" * 32,
            "parser_name": "demo",
            "parser_version": "1",
            "ingest_time": "2026-07-24T10:00:00+00:00",
            "message": f"line {i}",
            "timestamp": f"2026-07-24T09:00:0{i}+00:00",
            "timestamp_desc": "parsed",
            "artifact": "x",
            "artifact_long": "y",
            "display_name": "d",
            "tags": ["t1"],
            "attributes": {"k": "v"},
            "embedding_model": "",
            "embedding_config_hash": "",
        }
        for i in range(n)
    ]
```

`tests/test_transfer_export.py`:

```python
"""Exporter tests: Postgres snapshot completeness + archive assembly.

Uses the SQLite `store` fixture from conftest and the shared FakeClickHouse
from tests/transfer_fakes.py — no live services.
"""

from __future__ import annotations

import pytest

from vestigo.core.retention import retention_path
from vestigo.db import postgres as pg
from vestigo.transfer.archive import FORMAT_VERSION, ArchiveReader
from vestigo.transfer.exporter import export_case
from tests.transfer_fakes import FakeClickHouse, _add, _event_rows


async def _rich_case(store, owner_id):
    """A case with one of every exported entity."""
    case = await _add(store, pg.Case, name="Export Me", owner_id=owner_id)
    src = await _add(store, pg.Source, case_id=case.id, name="src", file_hash="ab" * 32)
    tl = await _add(store, pg.Timeline, case_id=case.id, name="tl", is_default=True)
    await _add(store, pg.TimelineSource, timeline_id=tl.id, source_id=src.id)
    await _add(store, pg.TimelineEnricher, timeline_id=tl.id)
    await _add(store, pg.View, case_id=case.id, name="v", query="", view_filter={})
    await _add(store, pg.SavedChart, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.BaselineDefinition, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.DetectorRun, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.FindingDisposition, case_id=case.id, timeline_id=tl.id)
    await _add(
        store, pg.Annotation, case_id=case.id, source_id=src.id,
        event_id="evt-1", annotation_type="comment", content="note",
    )
    await _add(store, pg.SigmaRule, case_id=case.id)
    await _add(store, pg.SigmaRun, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.SourceEnrichment, case_id=case.id, source_id=src.id)
    conv = await _add(store, pg.AgentConversation, case_id=case.id, timeline_id=tl.id, user_id=owner_id)
    await _add(store, pg.AgentMessage, conversation_id=conv.id)
    await _add(store, pg.AgentProposal, case_id=case.id, conversation_id=conv.id)
    await _add(store, pg.AuditLog, case_id=case.id)
    return case, src, tl


async def test_export_roundtrip_all_entities(store, tmp_path, monkeypatch):
    owner = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
    case, src, tl = await _rich_case(store, owner.id)
    fake_ch = FakeClickHouse({(case.id, src.id): _event_rows(case.id, src.id)})

    result = await export_case(
        store, lambda: fake_ch, case.id,
        include_blobs=False, exported_by="alice", dest_dir=tmp_path,
    )

    assert result.path.exists() and result.bytes > 0
    reader = ArchiveReader(result.path)
    m = reader.manifest
    assert m["format_version"] == FORMAT_VERSION
    assert m["case"] == {"id": case.id, "name": "Export Me"}
    assert m["exported_by"] == "alice"
    assert m["include_blobs"] is False
    reader.verify_members()
    for stem in (
        "sources", "timelines", "timeline_sources", "timeline_enrichers", "views",
        "saved_charts", "baseline_definitions", "detector_runs", "finding_dispositions",
        "annotations", "sigma_rules", "sigma_runs", "source_enrichments",
        "agent_conversations", "agent_messages", "agent_proposals", "audit_log",
    ):
        rows = reader.read_ndjson(f"postgres/{stem}.ndjson")
        assert len(rows) == 1, f"{stem}: expected 1 row, got {len(rows)}"
    assert reader.read_json("postgres/case.json")["name"] == "Export Me"
    assert reader.read_json("postgres/user_refs.json")["users"] == {owner.id: "alice"}
    # Events: one Arrow member with both rows, hashes as plain strings.
    names = reader.member_names()
    assert f"events/{src.id}.arrow" in names
    assert result.counts["events"] == 2
    reader.close()


async def test_export_without_sources_never_touches_clickhouse(store, tmp_path):
    owner = await _add(store, pg.User, username="bob", is_admin=False, is_active=True)
    case = await _add(store, pg.Case, name="Empty", owner_id=owner.id)

    def _forbidden():
        raise AssertionError("ClickHouse factory must not be called")

    result = await export_case(
        store, _forbidden, case.id,
        include_blobs=True, exported_by="bob", dest_dir=tmp_path,
    )
    assert result.counts["sources"] == 0
    assert result.counts["events"] == 0


async def test_export_blobs(store, tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGO_SOURCE_RETENTION_PATH", str(tmp_path / "retained"))
    from vestigo.core.config import get_settings

    get_settings.cache_clear()
    try:
        owner = await _add(store, pg.User, username="carol", is_admin=False, is_active=True)
        file_hash = "cd" * 32
        case = await _add(store, pg.Case, name="Blobs", owner_id=owner.id)
        src = await _add(store, pg.Source, case_id=case.id, name="s", file_hash=file_hash)
        blob = retention_path(file_hash)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"original file bytes")
        fake_ch = FakeClickHouse({(case.id, src.id): _event_rows(case.id, src.id, n=1)})

        result = await export_case(
            store, lambda: fake_ch, case.id,
            include_blobs=True, exported_by="carol", dest_dir=tmp_path / "out",
        )
        reader = ArchiveReader(result.path)
        assert f"blobs/{file_hash}" in reader.member_names()
        reader.verify_members()
        reader.close()
        assert result.counts["blobs"] == 1
    finally:
        get_settings.cache_clear()


async def test_export_missing_blob_warns_not_fails(store, tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGO_SOURCE_RETENTION_PATH", str(tmp_path / "retained"))
    from vestigo.core.config import get_settings

    get_settings.cache_clear()
    try:
        owner = await _add(store, pg.User, username="dave", is_admin=False, is_active=True)
        case = await _add(store, pg.Case, name="NoBlob", owner_id=owner.id)
        await _add(store, pg.Source, case_id=case.id, name="s", file_hash="ef" * 32)
        result = await export_case(
            store, lambda: FakeClickHouse(), case.id,
            include_blobs=True, exported_by="dave", dest_dir=tmp_path / "out",
        )
        assert len(result.warnings) == 1
        assert result.counts["blobs"] == 0
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_transfer_export.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vestigo.transfer.exporter'`

- [ ] **Step 4: Implement the exporter**

`src/vestigo/transfer/exporter.py`:

```python
"""Case export: snapshot Postgres state + ClickHouse events into a .vestigo archive.

The Postgres snapshot is generic: every exported model is read with a plain
ORM select and serialized by column introspection, so new columns ride along
without exporter changes. Events stream per source through the existing
iter_source_events primitive into an Arrow IPC member.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
from sqlalchemy import select

from vestigo import __version__
from vestigo.core.retention import retention_path
from vestigo.db._arrow_schema import EVENT_ARROW_SCHEMA
from vestigo.db._dt import NULL_TS_SENTINEL
from vestigo.db.postgres import (
    AgentConversation,
    AgentMessage,
    AgentProposal,
    Annotation,
    AuditLog,
    BaselineDefinition,
    Case,
    DetectorRun,
    FindingDisposition,
    PostgresStore,
    SavedChart,
    SigmaRule,
    SigmaRun,
    Source,
    SourceEnrichment,
    Team,
    Timeline,
    TimelineEnricher,
    TimelineSource,
    User,
    View,
)
from vestigo.transfer.archive import FORMAT_VERSION, ArchiveWriter

# (file stem, model, scope): "case" = WHERE case_id == ..., "timeline" =
# WHERE timeline_id IN (case's timelines), "conversation" = WHERE
# conversation_id IN (case's conversations). Insertion order = export order.
_EXPORT_ENTITIES: list[tuple[str, type, str]] = [
    ("sources", Source, "case"),
    ("timelines", Timeline, "case"),
    ("timeline_sources", TimelineSource, "timeline"),
    ("timeline_enrichers", TimelineEnricher, "timeline"),
    ("views", View, "case"),
    ("saved_charts", SavedChart, "case"),
    ("baseline_definitions", BaselineDefinition, "case"),
    ("detector_runs", DetectorRun, "case"),
    ("finding_dispositions", FindingDisposition, "case"),
    ("annotations", Annotation, "case"),
    ("sigma_rules", SigmaRule, "case"),
    ("sigma_runs", SigmaRun, "case"),
    ("source_enrichments", SourceEnrichment, "case"),
    ("agent_conversations", AgentConversation, "case"),
    ("agent_messages", AgentMessage, "conversation"),
    ("agent_proposals", AgentProposal, "case"),
    ("audit_log", AuditLog, "case"),
]


@dataclass
class ExportResult:
    path: Path
    bytes: int
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _row_to_dict(obj: Any) -> dict[str, Any]:
    """Serialize any ORM row by column introspection (datetimes → ISO)."""
    out: dict[str, Any] = {}
    for col in obj.__table__.columns:
        value = getattr(obj, col.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        out[col.name] = value
    return out


def _ndjson(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(r) + "\n" for r in rows).encode()


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _hash_str(value: Any) -> str:
    """FixedString columns come back as NUL-padded bytes; normalize to str."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("ascii", errors="replace").rstrip("\x00")
    return str(value)


def _normalize_event_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map an iter_source_events row onto EVENT_ARROW_SCHEMA dtypes."""
    ts = row.get("timestamp")
    return {
        "event_id": str(row["event_id"]),
        "case_id": row["case_id"],
        "source_id": row["source_id"],
        "source_file": row.get("source_file") or "",
        "byte_offset": row.get("byte_offset") or 0,
        "line_number": row.get("line_number") or 0,
        "content_hash": _hash_str(row.get("content_hash")),
        "file_hash": _hash_str(row.get("file_hash")),
        "parser_name": row.get("parser_name") or "",
        "parser_version": row.get("parser_version") or "",
        "ingest_time": _dt(row["ingest_time"]) if row.get("ingest_time") else NULL_TS_SENTINEL,
        "message": row.get("message") or "",
        "timestamp": _dt(ts) if ts else NULL_TS_SENTINEL,
        "timestamp_desc": row.get("timestamp_desc") or "",
        "artifact": row.get("artifact") or "",
        "artifact_long": row.get("artifact_long") or "",
        "display_name": row.get("display_name") or "",
        "tags": list(row.get("tags") or []),
        "attributes": {str(k): str(v) for k, v in (row.get("attributes") or {}).items()},
        "embedding_model": row.get("embedding_model") or "",
        "embedding_config_hash": _hash_str(row.get("embedding_config_hash")),
    }


def _write_source_events(clickhouse: Any, case_id: str, source_id: str, dest: Path) -> int:
    """Sync: stream one source's events into an Arrow IPC file. Row count."""
    total = 0
    with pa.OSFile(str(dest), "wb") as sink:
        writer = pa.ipc.new_stream(sink, EVENT_ARROW_SCHEMA)
        for rows in clickhouse.iter_source_events(case_id, source_id, batch_size=10_000):
            normalized = [_normalize_event_row(r) for r in rows]
            for batch in pa.Table.from_pylist(normalized, schema=EVENT_ARROW_SCHEMA).to_batches():
                writer.write_batch(batch)
                total += batch.num_rows
        writer.close()
    return total


async def _snapshot_postgres(store: PostgresStore, case_id: str) -> dict[str, Any]:
    """One session; returns case dict, per-stem row lists, and user refs."""
    async with store.session_factory() as session:
        case = (await session.execute(select(Case).where(Case.id == case_id))).scalar_one()
        timeline_ids = (
            (await session.execute(select(Timeline.id).where(Timeline.case_id == case_id)))
            .scalars()
            .all()
        )
        conversation_ids = (
            (
                await session.execute(
                    select(AgentConversation.id).where(AgentConversation.case_id == case_id)
                )
            )
            .scalars()
            .all()
        )
        stems: dict[str, list[dict[str, Any]]] = {}
        for stem, model, scope in _EXPORT_ENTITIES:
            if scope == "case":
                cond = model.case_id == case_id
            elif scope == "timeline":
                cond = model.timeline_id.in_(timeline_ids)
            else:  # conversation
                cond = model.conversation_id.in_(conversation_ids)
            rows = (await session.execute(select(model).where(cond))).scalars().all()
            stems[stem] = [_row_to_dict(r) for r in rows]

        user_ids = {case.owner_id} if case.owner_id else set()
        user_ids |= {r["user_id"] for r in stems["agent_conversations"] if r.get("user_id")}
        users: dict[str, str] = {}
        if user_ids:
            pairs = (
                await session.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
            ).all()
            users = {uid: uname for uid, uname in pairs}
        team_name = None
        if case.team_id:
            team_name = (
                await session.execute(select(Team.name).where(Team.id == case.team_id))
            ).scalar_one_or_none()
        return {
            "case": _row_to_dict(case),
            "stems": stems,
            "user_refs": {"users": users, "team": team_name},
        }


async def export_case(
    store: PostgresStore,
    clickhouse_factory: Callable[[], Any],
    case_id: str,
    *,
    include_blobs: bool,
    exported_by: str,
    dest_dir: Path,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> ExportResult:
    """Build the archive for one case. ClickHouse is only constructed when
    the case has sources (keeps empty-case export — and unit tests — CH-free)."""

    def _progress(phase: str) -> None:
        if progress:
            progress({"phase": phase})

    dest_dir.mkdir(parents=True, exist_ok=True)
    _progress("postgres")
    snapshot = await _snapshot_postgres(store, case_id)
    stems = snapshot["stems"]
    counts: dict[str, int] = {stem: len(rows) for stem, rows in stems.items()}
    counts["events"] = 0
    counts["blobs"] = 0
    warnings: list[str] = []

    archive_path = dest_dir / f"export-{case_id}.vestigo"
    writer = ArchiveWriter(archive_path)
    writer.add_bytes("postgres/case.json", json.dumps(snapshot["case"], indent=2).encode())
    writer.add_bytes("postgres/user_refs.json", json.dumps(snapshot["user_refs"], indent=2).encode())
    for stem, rows in stems.items():
        writer.add_bytes(f"postgres/{stem}.ndjson", _ndjson(rows))

    sources = stems["sources"]
    if sources:
        _progress("events")
        clickhouse = clickhouse_factory()
        for source in sources:
            dest = dest_dir / f"events-{source['id']}.arrow"
            try:
                n = await asyncio.to_thread(
                    _write_source_events, clickhouse, case_id, source["id"], dest
                )
                writer.add_file(f"events/{source['id']}.arrow", dest)
                counts["events"] += n
            finally:
                dest.unlink(missing_ok=True)
        if include_blobs:
            _progress("blobs")
            for source in sources:
                blob = retention_path(source["file_hash"])
                if blob.exists():
                    writer.add_file(f"blobs/{source['file_hash']}", blob)
                    counts["blobs"] += 1
                else:
                    warnings.append(f"source blob missing on disk: {source['name']} ({source['file_hash'][:12]}…)")

    _progress("manifest")
    writer.finish(
        {
            "format_version": FORMAT_VERSION,
            "vestigo_version": __version__,
            "exported_at": datetime.now(UTC).isoformat(),
            "exported_by": exported_by,
            "case": {"id": case_id, "name": snapshot["case"]["name"]},
            "include_blobs": include_blobs,
            "counts": counts,
        }
    )
    return ExportResult(
        path=archive_path,
        bytes=archive_path.stat().st_size,
        counts=counts,
        warnings=warnings,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_transfer_export.py -q`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add src/vestigo/core/retention.py src/vestigo/api/routers/cases.py tests/transfer_fakes.py src/vestigo/transfer/exporter.py tests/test_transfer_export.py
git commit -m "feat(transfer): case exporter — PG snapshot, Arrow event stream, optional blobs"
```

---

### Task 4: Export API endpoints + startup temp sweep

**Files:**
- Create: `src/vestigo/api/routers/transfer.py`
- Modify: `src/vestigo/api/main.py` (import, `include_router`, startup sweep)
- Test: `tests/test_transfer_api.py`

**Interfaces:**
- Consumes: `export_case`/`ExportResult` (Task 3); `temp_root`, `new_archive_path` (Task 2).
- Produces: `POST /api/cases/{case_id}/export?include_blobs=` → `202 {"job_id": ...}`; `GET /api/cases/{case_id}/export/{job_id}/download` → `.vestigo` attachment; job kind `case_export`; audit action `case.export`. Task 6 adds the import endpoint to the same router.

- [ ] **Step 1: Write the failing API tests**

`tests/test_transfer_api.py`:

```python
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
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"] if "id" in resp.json() else resp.json()["case"]["id"]


def _register_user(client, username: str, password: str) -> None:
    resp = client.post(
        "/api/admin/users",
        json={"username": username, "password": password},
    )
    assert resp.status_code in (200, 201), resp.text


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
        rows = audit.json()
        entries = rows["entries"] if isinstance(rows, dict) else rows
        assert any(e["action"] == "case.export" for e in entries)

        # Download deletes the server-side temp archive.
        dl2 = client.get(f"/api/cases/{case_id}/export/{job['id']}/download")
        assert dl2.status_code == 404
```

Note for the implementer: `_create_case` (path is `/api/cases/` with trailing slash — `cases.py:210`) and `_register_user` (`POST /api/admin/users`, `admin.py:105` — read it for required body fields, e.g. it may also want `is_admin`) encode response shapes defensively (`id` top-level vs nested) — check `src/vestigo/api/routers/cases.py` and `admin.py` for the exact shapes and simplify. The admin audit endpoint is `GET /api/admin/audit` (`admin.py:354`) — read it for its envelope shape. TestClient runs FastAPI `BackgroundTasks` before the response returns, so the export job is normally already terminal — `_job_terminal` still polls defensively.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transfer_api.py -q`
Expected: FAIL — 404 on `POST /api/cases/{id}/export` (router does not exist)

- [ ] **Step 3: Implement the router + startup sweep**

`src/vestigo/api/routers/transfer.py`:

```python
"""Case export/import (X1) endpoints. Heavy work runs in JobStore jobs."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, BackgroundTask, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse

from vestigo.api.deps import get_current_user, get_store, require_case_manage
from vestigo.core.jobs import get_job_store
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.postgres import Case, User
from vestigo.transfer.archive import new_archive_path, temp_root
from vestigo.transfer.exporter import export_case

router = APIRouter(prefix="/api", tags=["transfer"])


async def _run_export_job(job_id: str, case_id: str, include_blobs: bool, user: User) -> None:
    job_store = get_job_store()
    job_store.update(job_id, status="running")
    store = get_store()
    try:
        result = await export_case(
            store,
            ClickHouseStore,
            case_id,
            include_blobs=include_blobs,
            exported_by=user.username,
            dest_dir=temp_root() / job_id,
            progress=lambda p: job_store.update(job_id, progress=p),
        )
        # Move next to a stable per-job path for the download endpoint.
        final = new_archive_path(job_id)
        shutil.move(str(result.path), final)
        await store.record_audit(
            action="case.export",
            actor=user,
            case_id=case_id,
            target_type="case",
            target_id=case_id,
            detail={
                "job_id": job_id,
                "include_blobs": include_blobs,
                "bytes": result.bytes,
                "counts": result.counts,
            },
        )
        job_store.update(
            job_id,
            status="completed",
            result={
                "bytes": result.bytes,
                "counts": result.counts,
                "warnings": result.warnings,
            },
        )
    except Exception as exc:  # noqa: BLE001 — job error surface
        job_store.update(job_id, status="failed", error=str(exc))


@router.post("/cases/{case_id}/export", status_code=202)
async def export_case_endpoint(
    case_id: str,
    background_tasks: BackgroundTasks,
    include_blobs: bool = False,
    case: Case = Depends(require_case_manage),
    user: User = Depends(get_current_user),
):
    job = get_job_store().create(
        kind="case_export",
        progress={"phase": "queued"},
        created_by=user.id,
        case_id=case_id,
    )
    background_tasks.add_task(_run_export_job, job.id, case_id, include_blobs, user)
    return {"job_id": job.id}


@router.get("/cases/{case_id}/export/{job_id}/download")
async def download_export(
    case_id: str,
    job_id: str,
    case: Case = Depends(require_case_manage),
    user: User = Depends(get_current_user),
):
    job = get_job_store().get(job_id)
    if job is None or job.kind != "case_export" or job.case_id != case_id:
        raise HTTPException(status_code=404, detail="Export not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail=f"Export not ready ({job.status})")
    path = new_archive_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Archive already downloaded or expired")

    def _cleanup() -> None:
        path.unlink(missing_ok=True)
        shutil.rmtree(temp_root() / job_id, ignore_errors=True)

    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in case.name)
    return FileResponse(
        path,
        media_type="application/vnd.vestigo+zip",
        filename=f"{safe_name}.vestigo",
        background=BackgroundTask(_cleanup),
    )
```

In `src/vestigo/api/main.py`:

1. Add `transfer` to the `from vestigo.api.routers import (...)` import list (line ~19).
2. Add `app.include_router(transfer.router)` after `app.include_router(agent_tokens.router)` (line ~473).
3. Add a module-level helper next to `_reconcile_orphaned_ingests` (~line 108):

```python
async def _sweep_stale_transfer_archives() -> None:
    """Export archives live in temp storage and the job store is in-memory —
    after a restart every leftover is orphaned by definition."""
    from vestigo.transfer.archive import temp_root

    root = temp_root()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
```

(`shutil` is already imported in main.py — verify; add `import shutil` if not.)
4. Call `await _sweep_stale_transfer_archives()` in the lifespan right next to the `_reconcile_orphaned_ingests()` invocation.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transfer_api.py -q`
Expected: PASS (3 tests)

Run: `uv run pytest -m "not clickhouse" -q`
Expected: PASS (full non-CH suite still green — router registration didn't break the app)

- [ ] **Step 5: Commit**

```bash
git add src/vestigo/api/routers/transfer.py src/vestigo/api/main.py tests/test_transfer_api.py
git commit -m "feat(api): case export endpoints (job-based) with startup temp sweep"
```

---

### Task 5: `transfer/importer.py` — verification, ID remap, ordered restore

**Files:**
- Create: `src/vestigo/transfer/importer.py`
- Test: `tests/test_transfer_import.py`

**Interfaces:**
- Consumes: `ArchiveReader`, `ArchiveFormatError` (Task 2); `export_case` (Task 3, test fixture producer); `retention_path`, `retain_file` (Task 3); `refresh_source_field_stats` from `vestigo.db.field_stats`; `generate_id` from `vestigo.db.postgres`.
- Produces (used by Tasks 6–7): `ImportResult` dataclass (`case_id: str`, `counts: dict[str, int]`, `warnings: list[str]`); `async def import_case(store, clickhouse_factory, archive_path: Path, *, owner: User, progress=None) -> ImportResult`. Raises `ArchiveFormatError` on any verification failure **before** writes.

- [ ] **Step 1: Write the failing importer tests**

`tests/test_transfer_import.py`:

```python
"""Importer tests: remap integrity, secrets exclusion, tamper abort, cleanup.

Round-trips through the real exporter against the SQLite store fixture and
the shared fakes from tests/transfer_fakes.py.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from vestigo.db import postgres as pg
from vestigo.transfer.archive import ArchiveFormatError
from vestigo.transfer.exporter import export_case
from vestigo.transfer.importer import import_case
from tests.transfer_fakes import FakeClickHouse, _add, _event_rows


async def _rich_case(store, owner_id):
    case = await _add(store, pg.Case, name="Roundtrip", owner_id=owner_id)
    src = await _add(store, pg.Source, case_id=case.id, name="src", file_hash="ab" * 32)
    tl = await _add(
        store, pg.Timeline, case_id=case.id, name="tl", is_default=True,
        embedding_model="some-model", embedding_config={"dim": 8},
        embedding_config_hash="h", embedded_source_ids=[src.id],
        embedded_at=datetime.now(UTC),
    )
    await _add(store, pg.TimelineSource, timeline_id=tl.id, source_id=src.id)
    await _add(store, pg.View, case_id=case.id, name="v", query="", view_filter={})
    await _add(store, pg.SavedChart, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.DetectorRun, case_id=case.id, timeline_id=tl.id)
    await _add(
        store, pg.Annotation, case_id=case.id, source_id=src.id,
        event_id="evt-1", annotation_type="comment", content="note",
    )
    await _add(store, pg.SigmaRule, case_id=case.id)
    await _add(store, pg.SigmaRun, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.SourceEnrichment, case_id=case.id, source_id=src.id)
    conv = await _add(
        store, pg.AgentConversation, case_id=case.id, timeline_id=tl.id,
        user_id="ghost-user-id",  # not a user on this instance → fallback
    )
    await _add(store, pg.AgentMessage, conversation_id=conv.id)
    await _add(store, pg.AgentProposal, case_id=case.id, conversation_id=conv.id)
    await _add(store, pg.AuditLog, case_id=case.id, user_id=owner_id, username_snapshot="alice")
    return case, src, tl


async def _export(store, case, src, tmp_path, rows=2):
    fake = FakeClickHouse({(case.id, src.id): _event_rows(case.id, src.id, n=rows)})
    result = await export_case(
        store, lambda: fake, case.id,
        include_blobs=False, exported_by="alice", dest_dir=tmp_path,
    )
    return result.path


async def _count(store, model) -> int:
    async with store.session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


class TestRoundTrip:
    async def test_remap_referential_integrity(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        bob = await _add(store, pg.User, username="bob", is_admin=False, is_active=True)
        case, src, tl = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)

        result = await import_case(store, lambda: FakeClickHouse(), archive, owner=bob)

        assert result.case_id != case.id
        new_case = await store.get_case(result.case_id)
        assert new_case.name == "Roundtrip"
        assert new_case.owner_id == bob.id
        assert new_case.team_id is None

        async with store.session_factory() as s:
            new_tl = (await s.execute(
                select(pg.Timeline).where(pg.Timeline.case_id == result.case_id)
            )).scalar_one()
            # Embedding state reset on import.
            assert new_tl.embedding_model is None
            assert new_tl.embedding_config is None
            assert new_tl.embedded_source_ids is None

            new_chart = (await s.execute(
                select(pg.SavedChart).where(pg.SavedChart.case_id == result.case_id)
            )).scalar_one()
            assert new_chart.timeline_id == new_tl.id

            new_ann = (await s.execute(
                select(pg.Annotation).where(pg.Annotation.case_id == result.case_id)
            )).scalar_one()
            new_src = (await s.execute(
                select(pg.Source).where(pg.Source.case_id == result.case_id)
            )).scalar_one()
            assert new_ann.source_id == new_src.id
            assert new_src.id != src.id
            assert new_ann.event_id == "evt-1"  # event IDs preserved verbatim

            new_conv = (await s.execute(
                select(pg.AgentConversation).where(pg.AgentConversation.case_id == result.case_id)
            )).scalar_one()
            assert new_conv.user_id == bob.id  # unknown "ghost-user-id" → importer fallback
            new_msg = (await s.execute(
                select(pg.AgentMessage).where(pg.AgentMessage.conversation_id == new_conv.id)
            )).scalar_one()
            assert new_msg is not None

            new_audit = (await s.execute(
                select(pg.AuditLog).where(pg.AuditLog.case_id == result.case_id)
            )).scalar_one()
            assert new_audit.user_id is None
            assert new_audit.username_snapshot == "alice"

        assert any("ghost" in w or "user" in w.lower() for w in result.warnings)

    async def test_events_rewritten_to_new_ids(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)
        target = FakeClickHouse()

        result = await import_case(store, lambda: target, archive, owner=alice)

        keys = list(target.inserted.keys())
        assert len(keys) == 1
        new_case_id, new_source_id = keys[0]
        assert new_case_id == result.case_id and new_source_id != src.id
        rows = target.inserted[keys[0]]
        assert len(rows) == 2
        assert {r["message"] for r in rows} == {"line 0", "line 1"}
        assert result.counts["events"] == 2


class TestRejection:
    async def test_tampered_archive_aborts_before_writes(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)
        cases_before = await _count(store, pg.Case)

        with zipfile.ZipFile(archive) as zin:
            items = {i.filename: zin.read(i.filename) for i in zin.infolist()}
        items["postgres/sources.ndjson"] = b'{"corrupted": true}\n'
        with zipfile.ZipFile(archive, "w") as zout:
            for name, data in items.items():
                zout.writestr(name, data)

        with pytest.raises(ArchiveFormatError, match="hash mismatch"):
            await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)
        assert await _count(store, pg.Case) == cases_before

    async def test_no_secrets_in_archive(self, store, tmp_path):
        alice = await _add(
            store, pg.User, username="alice", is_admin=False, is_active=True,
            password_hash="$2b$12$somebcrypthashvalue",
        )
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)

        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            assert not any("agent_tokens" in n or "sessions" in n for n in names)
            blob = b"".join(z.read(n) for n in names if n.endswith((".ndjson", ".json")))
        assert b"password_hash" not in blob
        assert b"$2b$" not in blob


class TestFailureCleanup:
    async def test_mid_import_failure_deletes_partial_case(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)
        cases_before = await _count(store, pg.Case)

        class ExplodingClickHouse(FakeClickHouse):
            def insert_events_arrow(self, batch):
                raise RuntimeError("clickhouse went away")

        fake = ExplodingClickHouse()
        with pytest.raises(RuntimeError, match="went away"):
            await import_case(store, lambda: fake, archive, owner=alice)
        assert await _count(store, pg.Case) == cases_before
        assert fake.deleted, "event partition cleanup must run for inserted sources"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transfer_import.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vestigo.transfer.importer'`

- [ ] **Step 3: Implement the importer**

`src/vestigo/transfer/importer.py`:

```python
"""Case import: verify, remap IDs, restore Postgres rows + events + blobs.

Always restores as a NEW case owned by the importer (no merge, no conflict
path). Every Postgres row gets a fresh id; an old→new map rewrites all
references. Event ids are preserved verbatim — event queries are case-scoped
and preserved ids keep annotation→event cross-references intact. Any failure
after case creation deletes the partial case (Postgres cascade + ClickHouse
partitions) before the error propagates.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
from sqlalchemy import select

from vestigo.core.retention import retain_file, retention_path
from vestigo.db.field_stats import refresh_source_field_stats
from vestigo.db.postgres import (
    AgentConversation,
    AgentMessage,
    AgentProposal,
    Annotation,
    AuditLog,
    BaselineDefinition,
    DetectorRun,
    FindingDisposition,
    PostgresStore,
    SavedChart,
    SigmaRule,
    SigmaRun,
    Source,
    SourceEnrichment,
    Timeline,
    TimelineEnricher,
    TimelineSource,
    User,
    View,
    generate_id,
)
from vestigo.transfer.archive import ArchiveFormatError, ArchiveReader

_SHA256_RE = re.compile(r"[0-9a-f]{64}")

# Insertion order = dependency order. Refs map column → remap kind.
_IMPORT_SPECS: list[tuple[str, type, dict[str, str]]] = [
    ("sources", Source, {"id": "source", "case_id": "case"}),
    ("timelines", Timeline, {"id": "timeline", "case_id": "case"}),
    ("timeline_sources", TimelineSource, {"timeline_id": "timeline", "source_id": "source"}),
    ("timeline_enrichers", TimelineEnricher, {"id": "timeline_enricher", "timeline_id": "timeline"}),
    ("views", View, {"id": "view", "case_id": "case"}),
    ("saved_charts", SavedChart, {"id": "chart", "case_id": "case", "timeline_id": "timeline"}),
    ("baseline_definitions", BaselineDefinition, {"id": "baseline", "case_id": "case", "timeline_id": "timeline"}),
    ("detector_runs", DetectorRun, {"id": "detector_run", "case_id": "case", "timeline_id": "timeline"}),
    ("finding_dispositions", FindingDisposition, {"id": "disposition", "case_id": "case", "timeline_id": "timeline"}),
    ("annotations", Annotation, {"id": "annotation", "case_id": "case", "source_id": "source"}),
    ("sigma_rules", SigmaRule, {"id": "sigma_rule", "case_id": "case"}),
    ("sigma_runs", SigmaRun, {"id": "sigma_run", "case_id": "case", "timeline_id": "timeline"}),
    ("source_enrichments", SourceEnrichment, {"id": "source_enrichment", "case_id": "case", "source_id": "source"}),
    ("agent_conversations", AgentConversation, {"id": "conversation", "case_id": "case", "timeline_id": "timeline"}),
    ("agent_messages", AgentMessage, {"id": "message", "conversation_id": "conversation"}),
    ("agent_proposals", AgentProposal, {"id": "proposal", "case_id": "case", "conversation_id": "conversation"}),
    ("audit_log", AuditLog, {"id": "audit", "case_id": "case"}),
]

_TIMELINE_EMBEDDING_COLUMNS = (
    "embedding_model",
    "embedding_config",
    "embedding_config_hash",
    "embedded_source_ids",
    "embedded_at",
)


@dataclass
class ImportResult:
    case_id: str
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class _IdMap:
    def __init__(self) -> None:
        self._map: dict[tuple[str, str], str] = {}

    def remap(self, kind: str, old: Any) -> Any:
        if old is None:
            return None
        key = (kind, str(old))
        if key not in self._map:
            self._map[key] = generate_id(kind)
        return self._map[key]


def _revive(model: type, row: dict[str, Any], idmap: _IdMap, refs: dict[str, str]) -> Any:
    """Build an ORM object from an archived row with remapped refs."""
    values: dict[str, Any] = {}
    for col in model.__table__.columns:
        value = row.get(col.name)
        if col.name in refs:
            value = idmap.remap(refs[col.name], value)
        if isinstance(value, str) and col.type.python_type is datetime:
            value = datetime.fromisoformat(value)
        values[col.name] = value
    return model(**values)


def _insert_source_events(clickhouse: Any, reader: ArchiveReader, arcname: str,
                          new_case_id: str, new_source_id: str) -> int:
    """Sync: rewrite case_id/source_id in every batch and insert. Row count."""
    total = 0
    with reader.open_member(arcname) as f:
        ipc = pa.ipc.open_stream(f)
        for batch in ipc:
            batch = batch.set_column(
                batch.schema.get_field_index("case_id"),
                "case_id",
                pa.array([new_case_id] * batch.num_rows, type=pa.string()),
            )
            batch = batch.set_column(
                batch.schema.get_field_index("source_id"),
                "source_id",
                pa.array([new_source_id] * batch.num_rows, type=pa.string()),
            )
            total += clickhouse.insert_events_arrow(batch)
    return total


async def import_case(
    store: PostgresStore,
    clickhouse_factory: Callable[[], Any],
    archive_path: Path,
    *,
    owner: User,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> ImportResult:
    """Restore an archive as a new case owned by `owner`. All-or-nothing."""

    def _progress(phase: str) -> None:
        if progress:
            progress({"phase": phase})

    _progress("verify")
    reader = ArchiveReader(archive_path)  # raises ArchiveFormatError on bad manifest
    reader.verify_members()  # raises before ANY write
    case_data = reader.read_json("postgres/case.json")
    user_refs = reader.read_json("postgres/user_refs.json")
    member_names = set(reader.member_names())

    counts: dict[str, int] = {"events": 0, "blobs": 0}
    warnings: list[str] = []
    idmap = _IdMap()
    new_case_id = generate_id("case")
    # Pin the case remap to the id we actually create with.
    idmap._map[("case", str(case_data["id"]))] = new_case_id

    created = False
    inserted_sources: list[str] = []
    clickhouse = None
    try:
        await store.create_case(
            new_case_id,
            case_data["name"],
            case_data.get("description"),
            owner_id=owner.id,
            team_id=None,
        )
        created = True

        _progress("postgres")
        # Username → local user id, for conversation attribution mapping.
        async with store.session_factory() as session:
            for stem, model, refs in _IMPORT_SPECS:
                rows = reader.read_ndjson(f"postgres/{stem}.ndjson")
                counts[stem] = len(rows)
                for row in rows:
                    obj = _revive(model, row, idmap, refs)
                    if isinstance(obj, Timeline):
                        for colname in _TIMELINE_EMBEDDING_COLUMNS:
                            setattr(obj, colname, None)
                    elif isinstance(obj, AgentConversation):
                        obj.user_id = await _map_user(
                            session, user_refs, row.get("user_id"), owner, warnings
                        )
                    elif isinstance(obj, AuditLog):
                        obj.user_id = None  # username_snapshot carries attribution
                    session.add(obj)
                await session.flush()
            await session.commit()

        source_rows = reader.read_ndjson("postgres/sources.ndjson")
        event_members = [n for n in member_names if n.startswith("events/") and n.endswith(".arrow")]
        if event_members:
            _progress("events")
            clickhouse = clickhouse_factory()
        for row in source_rows:
            new_source_id = idmap.remap("source", row["id"])
            arcname = f"events/{row['id']}.arrow"
            if arcname in member_names:
                # Track BEFORE inserting: a mid-source failure may leave a
                # partially written partition, and cleanup must drop it.
                inserted_sources.append(new_source_id)
                n = await asyncio.to_thread(
                    _insert_source_events, clickhouse, reader, arcname, new_case_id, new_source_id
                )
                counts["events"] += n

        blob_members = [n for n in member_names if n.startswith("blobs/")]
        if blob_members:
            _progress("blobs")
        for arcname in blob_members:
            sha = arcname.removeprefix("blobs/")
            if not _SHA256_RE.fullmatch(sha):
                warnings.append(f"skipping suspicious blob member: {arcname}")
                continue
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                reader.extract_to(arcname, tmp_path)
                await asyncio.to_thread(retain_file, tmp_path, retention_path(sha))
                counts["blobs"] += 1
            finally:
                tmp_path.unlink(missing_ok=True)
        blobbed = {n.removeprefix("blobs/") for n in blob_members}
        for row in source_rows:
            if blob_members and row["file_hash"] not in blobbed:
                warnings.append(f"no blob in archive for source {row['name']} — events restored, original file absent")

        if clickhouse is not None and inserted_sources:
            _progress("stats")
            for new_source_id in inserted_sources:
                try:
                    await refresh_source_field_stats(store, clickhouse, new_case_id, new_source_id)
                except Exception as exc:  # noqa: BLE001 — stats never fail an import
                    warnings.append(f"field stats recompute failed for a source: {exc}")
    except Exception:
        if clickhouse is not None:
            for new_source_id in inserted_sources:
                try:
                    await asyncio.to_thread(clickhouse.delete_source_events, new_case_id, new_source_id)
                except Exception:  # noqa: BLE001,S110 — best-effort cleanup
                    pass
        if created:
            await store.delete_case(new_case_id)
        raise
    finally:
        reader.close()

    return ImportResult(case_id=new_case_id, counts=counts, warnings=warnings)


async def _map_user(
    session: Any,
    user_refs: dict[str, Any],
    old_user_id: Any,
    owner: User,
    warnings: list[str],
) -> str:
    """Old user id → username → local user id; importer fallback + warning."""
    username = (user_refs.get("users") or {}).get(str(old_user_id)) if old_user_id else None
    if username:
        local = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if local is not None:
            return local.id
    warnings.append(
        f"user {username or old_user_id} not found on this instance — attributed to importer"
    )
    return owner.id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transfer_import.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/vestigo/transfer/importer.py tests/test_transfer_import.py
git commit -m "feat(transfer): case importer — verify, remap, restore, all-or-nothing cleanup"
```

---

### Task 6: Import API endpoint

**Files:**
- Modify: `src/vestigo/api/routers/transfer.py` (add import endpoint + job runner)
- Test: `tests/test_transfer_api.py` (append tests)

**Interfaces:**
- Consumes: `import_case`/`ImportResult` (Task 5); `receive_upload_to_tmp` from `vestigo.api.uploads` (same call shape as `cases.py`: `tmp_path, file_hash, size_bytes = await receive_upload_to_tmp(file, max_bytes=..., suffix=...)`).
- Produces: `POST /api/cases/import` (multipart) → `202 {"job_id": ...}`; job kind `case_import` (`case_id=None` — visible to creator + admin only, per the jobs visibility rule); audit action `case.import`.

- [ ] **Step 1: Append the failing tests**

Append to `tests/test_transfer_api.py`:

```python
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
        body = case.json()
        assert body["name"] == "Imported"
        assert body["owner_id"] == me["user"]["id"] if "user" in me else body["owner_id"]
        assert body["team_id"] is None

        audit = client.get(f"/api/admin/audit?action=case.import")
        assert audit.status_code == 200
        rows = audit.json()
        entries = rows["entries"] if isinstance(rows, dict) else rows
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
```

Note for the implementer: the `login()` helper returns the login response JSON — read `src/vestigo/api/routers/auth.py` for its exact shape (`{"user": {...}}` vs flat) and simplify the `owner_id` assertion. The admin audit query endpoint's envelope likewise (read `admin.py`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transfer_api.py -q`
Expected: FAIL — `POST /api/cases/import` 404/405 (endpoint does not exist)

- [ ] **Step 3: Implement the import endpoint**

Append to `src/vestigo/api/routers/transfer.py`:

```python
import tempfile
from pathlib import Path

from fastapi import File, UploadFile

from vestigo.api.uploads import receive_upload_to_tmp
from vestigo.core.config import get_settings
from vestigo.transfer.importer import import_case


async def _run_import_job(job_id: str, tmp_path: Path, user: User) -> None:
    job_store = get_job_store()
    job_store.update(job_id, status="running")
    store = get_store()
    try:
        result = await import_case(
            store,
            ClickHouseStore,
            tmp_path,
            owner=user,
            progress=lambda p: job_store.update(job_id, progress=p),
        )
        await store.record_audit(
            action="case.import",
            actor=user,
            case_id=result.case_id,
            target_type="case",
            target_id=result.case_id,
            detail={"job_id": job_id, "counts": result.counts, "warnings": result.warnings},
        )
        job_store.update(
            job_id,
            status="completed",
            result={
                "case_id": result.case_id,
                "counts": result.counts,
                "warnings": result.warnings,
            },
        )
    except Exception as exc:  # noqa: BLE001 — job error surface
        job_store.update(job_id, status="failed", error=str(exc))
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/cases/import", status_code=202)
async def import_case_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    settings = get_settings()
    tmp_path, _file_hash, size_bytes = await receive_upload_to_tmp(
        file, max_bytes=settings.max_upload_bytes, suffix=".vestigo"
    )
    job = get_job_store().create(
        kind="case_import",
        progress={"phase": "queued", "bytes": size_bytes},
        created_by=user.id,
    )
    background_tasks.add_task(_run_import_job, job.id, tmp_path, user)
    return {"job_id": job.id}
```

Merge these imports into the existing import block at the top of the file (no duplicate import lines; `Path` is already imported there — drop the redundant one).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transfer_api.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/vestigo/api/routers/transfer.py tests/test_transfer_api.py
git commit -m "feat(api): case import endpoint (upload + job, importer-owned restore)"
```

---

### Task 7: Live-ClickHouse round-trip test (marked)

**Files:**
- Test: `tests/test_transfer_roundtrip_clickhouse.py`

**Interfaces:**
- Consumes: `export_case` (Task 3), `import_case` (Task 5), the `clickhouse` marker (Task 1).
- Produces: proof that events survive export→import byte-identically modulo the `case_id`/`source_id` rewrite.

- [ ] **Step 1: Write the failing test**

`tests/test_transfer_roundtrip_clickhouse.py`:

```python
"""Live-ClickHouse round-trip: ingest → export → import → same events.

Skipped (visibly, via the clickhouse marker) when the dev compose stack is
absent. Uses the SQLite `store` fixture for Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from vestigo.db import postgres as pg
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.models.event import Event, derive_event_id
from vestigo.transfer.exporter import export_case
from vestigo.transfer.importer import import_case

pytestmark = pytest.mark.clickhouse

CASE_ID = f"rt-case-{uuid.uuid4().hex[:8]}"
SOURCE_ID = f"rt-src-{uuid.uuid4().hex[:8]}"
FILE_HASH = "ab" * 32


@pytest.fixture(scope="module")
def ch_store():
    try:
        store = ClickHouseStore()
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    yield store
    store.delete_source_events(CASE_ID, SOURCE_ID)


def _event(i: int) -> Event:
    ts = datetime(2026, 7, 24, 9, 0, i, tzinfo=UTC)
    return Event(
        event_id=derive_event_id(
            case_id=CASE_ID,
            source_id=SOURCE_ID,
            source_identity=FILE_HASH,
            byte_offset=i * 100,
            content_hash=f"{i:064x}",
            parser_name="demo",
            parser_version="1",
        ),
        case_id=CASE_ID,
        source_id=SOURCE_ID,
        source_file="demo.log",
        byte_offset=i * 100,
        line_number=i,
        content_hash=f"{i:064x}",
        file_hash=FILE_HASH,
        parser_name="demo",
        parser_version="1",
        ingest_time=datetime(2026, 7, 24, 10, 0, 0, tzinfo=UTC),
        message=f"round-trip line {i}",
        timestamp=ts.isoformat(),
        timestamp_desc="parsed",
        artifact="x",
        artifact_long="y",
        display_name="d",
        tags=["rt"],
        attributes={"idx": str(i)},
    )


async def _pg_case(store) -> None:
    async with store.session_factory() as session:
        session.add(
            pg.Case(id=CASE_ID, name="Round Trip", owner_id=None, team_id=None)
        )
        session.add(
            pg.Source(
                id=SOURCE_ID, case_id=CASE_ID, name="src", file_hash=FILE_HASH,
                status="ready",
            )
        )
        await session.commit()


def _comparable(rows):
    return sorted(
        (
            str(r["event_id"]),
            r["message"],
            r["content_hash"].decode().rstrip("\x00")
            if isinstance(r["content_hash"], bytes)
            else r["content_hash"],
            r["byte_offset"],
        )
        for r in rows
    )


async def test_roundtrip_events_identical(store, ch_store, tmp_path):
    await _pg_case(store)
    ch_store.insert_events([_event(i) for i in range(3)])
    imported_ch = ClickHouseStore()  # same server; captures nothing — real CH
    try:
        archive = await export_case(
            store, lambda: ch_store, CASE_ID,
            include_blobs=False, exported_by="test", dest_dir=tmp_path,
        )
        assert archive.counts["events"] == 3

        bob = pg.User(id=f"rt-user-{uuid.uuid4().hex[:8]}", username=f"rt-{uuid.uuid4().hex[:6]}",
                      is_admin=False, is_active=True)
        async with store.session_factory() as session:
            session.add(bob)
            await session.commit()

        result = await import_case(store, lambda: imported_ch, archive.path, owner=bob)
        assert result.counts["events"] == 3

        async with store.session_factory() as session:
            new_src = (
                await session.execute(
                    select(pg.Source).where(pg.Source.case_id == result.case_id)
                )
            ).scalar_one()

        original = [r for batch in ch_store.iter_source_events(CASE_ID, SOURCE_ID, 1000) for r in batch]
        restored = [
            r
            for batch in imported_ch.iter_source_events(result.case_id, new_src.id, 1000)
            for r in batch
        ]
        assert _comparable(restored) == _comparable(original)
    finally:
        # Clean up the imported case's events as well (partition drop).
        try:
            async with store.session_factory() as session:
                rows = (
                    await session.execute(
                        select(pg.Source).where(pg.Source.case_id != CASE_ID)
                    )
                ).scalars().all()
                for r in rows:
                    imported_ch.delete_source_events(r.case_id, r.id)
        except Exception:
            pass
```

Note for the implementer: the `Event` constructor call above mirrors `to_clickhouse_row`'s field list in `src/vestigo/models/event.py` — read that file first and adjust field names/types if the model differs (e.g. `timestamp` may take a datetime). The test must construct events exactly the way existing CH tests do — see the `_event` helper in `tests/test_field_stats.py` and match it.

- [ ] **Step 2: Run it against the dev stack (or observe the skip)**

Run: `docker compose ps` — if ClickHouse is up: `uv run pytest tests/test_transfer_roundtrip_clickhouse.py -q`
Expected with stack: PASS. Without stack: `1 skipped` (visible skip, not silent pass — that is the point of Task 1's marker).

- [ ] **Step 3: Commit**

```bash
git add tests/test_transfer_roundtrip_clickhouse.py
git commit -m "test(transfer): live-ClickHouse export/import round-trip (marked)"
```

---

### Task 8: Frontend — Export button + Import dialog

**Files:**
- Create: `frontend/src/api/transfer.ts`
- Create: `frontend/src/components/cases/ExportCaseDialog.tsx`
- Create: `frontend/src/components/cases/ImportCaseDialog.tsx`
- Modify: `frontend/src/components/cases/CaseCard.tsx`
- Modify: `frontend/src/components/cases/CaseList.tsx`

**Interfaces:**
- Consumes: Task 4/6 endpoints; `jobsApi.get` (`frontend/src/api/jobs.ts`); ui primitives used by `CreateCaseDialog.tsx` (`@/components/ui/Dialog`, `Button`, `Input`); `triggerDownload` from `@/lib/download`.
- Produces: `transferApi.startExport(caseId, includeBlobs) → Promise<{job_id: string}>`; `transferApi.downloadExport(caseId, jobId, caseName) → Promise<void>`; `transferApi.startImport(file) → Promise<{job_id: string}>`; `transferApi.getJob(jobId) → Promise<Job>`.

- [ ] **Step 1: API helpers**

`frontend/src/api/transfer.ts`:

```ts
import { get, post, postForm, fetchBlobGet } from "./client";
import { triggerDownload } from "@/lib/download";
import type { Job } from "./types";

export const transferApi = {
  startExport: (caseId: string, includeBlobs: boolean) =>
    post<{ job_id: string }>(`/cases/${caseId}/export?include_blobs=${includeBlobs}`),

  downloadExport: async (caseId: string, jobId: string, caseName: string): Promise<void> => {
    const blob = await fetchBlobGet(`/cases/${caseId}/export/${jobId}/download`);
    triggerDownload(blob, `${caseName}.vestigo`);
  },

  startImport: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return postForm<{ job_id: string }>("/cases/import", form);
  },

  getJob: (jobId: string) => get<{ job: Job }>(`/jobs/${jobId}`).then((r) => r.job),
};
```

- [ ] **Step 2: Export dialog**

`frontend/src/components/cases/ExportCaseDialog.tsx` — mirrors `CreateCaseDialog`'s Dialog/Button structure; polls the job itself (JobTray invalidation doesn't trigger downloads):

```tsx
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Download } from "lucide-react";
import { transferApi } from "@/api/transfer";
import type { Case } from "@/api/types";

interface Props {
  case_: Case;
}

export function ExportCaseDialog({ case_ }: Props) {
  const [open, setOpen] = useState(false);
  const [includeBlobs, setIncludeBlobs] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { data: job } = useQuery({
    queryKey: ["transfer-export", jobId],
    queryFn: () => transferApi.getJob(jobId!),
    enabled: !!jobId,
    refetchInterval: (query) =>
      query.state.data?.status === "completed" || query.state.data?.status === "failed"
        ? false
        : 2000,
  });

  useEffect(() => {
    if (job?.status === "completed") {
      transferApi
        .downloadExport(case_.id, job.id, case_.name)
        .then(() => setOpen(false))
        .catch((e) => setError((e as Error).message));
    } else if (job?.status === "failed") {
      setError(job.error ?? "Export failed");
    }
  }, [job, case_.id, case_.name]);

  const start = () => {
    setError(null);
    transferApi
      .startExport(case_.id, includeBlobs)
      .then((r) => setJobId(r.job_id))
      .catch((e) => setError((e as Error).message));
  };

  const running = !!jobId && (!job || (job.status !== "completed" && job.status !== "failed"));

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="sm" title="Export case as .vestigo archive">
          <Download size={14} />
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Export Case"
        description="Downloads a single .vestigo archive: all case data, events, and analyst work. Restorable on any Vestigo instance."
      >
        <div className="space-y-3">
          <label className="flex items-center gap-2 text-sm text-[var(--color-fg-primary)]">
            <input
              type="checkbox"
              checked={includeBlobs}
              onChange={(e) => setIncludeBlobs(e.target.checked)}
            />
            Include original source files (larger archive, full backup)
          </label>
          {error && <p className="text-xs text-[var(--color-danger)]">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Cancel</Button>
            </DialogClose>
            <Button variant="accent" size="sm" disabled={running} onClick={start}>
              {running ? "Exporting…" : "Export"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 3: Import dialog**

`frontend/src/components/cases/ImportCaseDialog.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Upload } from "lucide-react";
import { transferApi } from "@/api/transfer";

export function ImportCaseDialog() {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: job } = useQuery({
    queryKey: ["transfer-import", jobId],
    queryFn: () => transferApi.getJob(jobId!),
    enabled: !!jobId,
    refetchInterval: (query) =>
      query.state.data?.status === "completed" || query.state.data?.status === "failed"
        ? false
        : 2000,
  });

  useEffect(() => {
    if (job?.status === "completed" && job.result?.case_id) {
      qc.invalidateQueries({ queryKey: ["cases"] });
      setOpen(false);
      navigate(`/cases/${job.result.case_id}`);
    } else if (job?.status === "failed") {
      setError(job.error ?? "Import failed");
    }
  }, [job, qc, navigate]);

  const start = () => {
    if (!file) return;
    setError(null);
    transferApi
      .startImport(file)
      .then((r) => setJobId(r.job_id))
      .catch((e) => setError((e as Error).message));
  };

  const running = !!jobId && (!job || (job.status !== "completed" && job.status !== "failed"));

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="sm">
          <Upload size={14} /> Import
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Import Case"
        description="Restore a .vestigo archive as a new case owned by you. Nobody else gets access automatically."
      >
        <div className="space-y-3">
          <input
            ref={inputRef}
            type="file"
            accept=".vestigo"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="block w-full text-sm text-[var(--color-fg-primary)]"
          />
          {error && <p className="text-xs text-[var(--color-danger)]">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Cancel</Button>
            </DialogClose>
            <Button variant="accent" size="sm" disabled={!file || running} onClick={start}>
              {running ? "Importing…" : "Import"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
```

Note for the implementer: check `frontend/src/api/types.ts` for the `Job` type's exact `result` shape (`job.result?.case_id` access) and adjust with a cast if `result` is typed loosely. 

- [ ] **Step 4: Wire into the case UI**

In `frontend/src/components/cases/CaseCard.tsx`: add the import and the button in the existing action row (lines 53-55):

```tsx
import { ExportCaseDialog } from "./ExportCaseDialog";
// ...
      <div className="flex items-center gap-1">
        {canManage && <ExportCaseDialog case_={case_} />}
        {canManage && <ChangeCaseScopeDialog case_={case_} />}
        {canManage && <DeleteCaseDialog case_={case_} />}
```

In `frontend/src/components/cases/CaseList.tsx`: read the file, find where `<CreateCaseDialog />` is rendered, and render `<ImportCaseDialog />` immediately before it (import + component).

- [ ] **Step 5: Verify**

Run inside `frontend/`: `npm run typecheck && npm run lint && npm test`
Expected: all pass (no new frontend tests — the existing suite must stay green; UI verified manually later via `/verify` convention).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/transfer.ts frontend/src/components/cases/ExportCaseDialog.tsx frontend/src/components/cases/ImportCaseDialog.tsx frontend/src/components/cases/CaseCard.tsx frontend/src/components/cases/CaseList.tsx
git commit -m "feat(ui): case export button + import dialog on case list"
```

---

### Task 9: Docs sync (spec deviations, ROADMAP, PROGRESS, CHANGELOG)

**Files:**
- Modify: `docs/superpowers/specs/2026-07-24-case-export-import-design.md`
- Modify: `docs/ROADMAP.md`
- Modify: `docs/PROGRESS.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: finished implementation.
- Produces: accurate docs; X1 + marker items removed from the open backlog.

- [ ] **Step 1: Sync the spec to the implementation**

In `docs/superpowers/specs/2026-07-24-case-export-import-design.md`:

1. §3 step 1 — replace "Snapshot Postgres entities via the existing `PostgresStore` list methods (one new method needed: `list_detector_runs_for_case(case_id)`; audit via existing `query_audit(case_id=...)`)." with: "Snapshot Postgres entities via direct ORM selects in the exporter (generic column serialization — no new `PostgresStore` methods; audit rows included as a case-scoped entity)."
2. §4 step 4 — delete the parenthetical "; name reused, suffixed ` (imported)` when the importer already owns a case with that name" → replace with "; archived name kept verbatim — case names are not unique-constrained".
3. §8 — replace "`src/vestigo/db/postgres.py` (add `list_detector_runs_for_case`; reuse everything else)" with "`src/vestigo/core/retention.py` (new home for the content-addressed blob helpers, moved from `api/routers/cases.py`); no `postgres.py` changes".

- [ ] **Step 2: ROADMAP**

In `docs/ROADMAP.md`:
1. Remove the X1 bullet from "Milestone 9 — case portability" (leave the milestone header + intro; shipped work moves to PROGRESS.md per house style).
2. Remove the "Make the ClickHouse-dependent tests visibly skipped" bullet from Milestone 2.
3. Update the "Priority order" paragraph: remove the "Milestone 9 (X1 case export/import) trails that same gate" sentence fragment about X1 (keep the S1+E1 gate wording for the remaining work).

- [ ] **Step 3: PROGRESS + CHANGELOG**

Read the top of `docs/PROGRESS.md` and mirror its entry format for a new entry: X1 case export/import (`.vestigo` archive: export/import endpoints, transfer package, RBAC/audit gates, importer-owned restore) + the ClickHouse pytest marker. Read `CHANGELOG.md`'s head and add a matching entry in its current style (new `## [1.7.0]` section if it follows Keep-a-Changelog; bump nothing else).

- [ ] **Step 4: Final full-suite verification**

Run: `uv run pytest -m "not clickhouse" -q`
Expected: PASS
Run (dev stack up): `uv run pytest -m clickhouse -q`
Expected: PASS (or visibly skipped without the stack)
Run inside `frontend/`: `npm run build`
Expected: builds clean

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-07-24-case-export-import-design.md docs/ROADMAP.md docs/PROGRESS.md CHANGELOG.md
git commit -m "docs: record X1 case export/import + clickhouse marker as shipped"
```
