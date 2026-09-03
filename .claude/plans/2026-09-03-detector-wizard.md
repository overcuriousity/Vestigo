# Opt-in Detectors (Detector Wizard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** No statistical detector runs unprompted; an analyst configures each one through a wizard, the configuration is a shared audited list on the timeline, and the Investigate rail runs exactly that list.

**Architecture:** One new JSONB column `timelines.detectors` (a list of `{method, params, frame, baseline_id, added_by, added_at}` entries, one per method) written through two audited endpoints that validate with the same per-method Pydantic models `/analysis/findings` uses. The frontend's `useStreamingSweep` iterates that list instead of the analysis plan, so the findings endpoint and its fingerprint cache are untouched. The mute list, the per-user field focus and the preset pills are removed; a three-step `DetectorWizard` dialog reusing `method-registry.ts` and the sheet's knob form replaces them.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy async / Alembic / pytest against real PostgreSQL + ClickHouse; React 19 / TypeScript / TanStack Query / Radix Dialog / vitest.

**Spec:** `docs/ROADMAP.md` § "Milestone 12 — opt-in detectors" (commit `fca6133a`). Read it first; every task below is one of its W-items.

## Global Constraints

- `uv run ruff check .` and `uv run ruff format --check .` must both pass; line length 100, `E501` ignored, Google-style docstrings.
- Frontend: `npm run typecheck`, `npm run lint`, `npm run test` from `frontend/` must pass. `frontend/src/test/designSystemBudget.ts` holds per-file budgets for raw `fontSize`/`rawButton` usage; a new or grown file fails that test unless its budget is adjusted honestly.
- Tests require running PostgreSQL and ClickHouse (`podman compose up -d`). Never add `pytest.skip` around store construction.
- Schema changes are Alembic revisions only, never `ALTER TABLE` in `init_schema`.
- Every `require_case_contribute` write records an audit row via `store.record_audit(action=..., actor=user, case_id=..., target_type="timeline", target_id=..., detail={...})`.
- The analysis plan and `/analysis/findings` endpoints do not change behavior. The gate stays advice: a `not_applicable` method must still be configurable and runnable.
- Copy tone: confident about what ships, no claim we cannot point at code for. Retired mechanisms are named as removed in `CHANGELOG.md`.
- Commit messages end with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01SjejUfyBb797trhrRF73Tp
  ```
  Commits are GPG-signed by repo config (`commit.gpgsign true`, identity `overcuriousity`). Work on a branch `feature/detector-wizard`, never directly on `main`.

## File map

Backend
- Create `src/vestigo/db/migrations/versions/0034_timeline_detectors.py` — add `detectors`, drop `muted_methods`.
- Modify `src/vestigo/db/postgres.py` — `Timeline.detectors`, `to_dict`, `set_timeline_detector`, `remove_timeline_detector`; delete `update_timeline_muted_methods`.
- Modify `src/vestigo/api/routers/analysis.py` — `DetectorEntryIn`, `validate_detector_entry`.
- Modify `src/vestigo/api/routers/cases.py` — `PUT`/`DELETE …/detectors/{method}`; delete the muted-methods endpoint and its request model.
- Modify `src/vestigo/api/routers/auth.py` — drop `analysis_method_focus` and `_check_method_focus_value`.
- Modify `src/vestigo/agent/tools.py` — `list_configured_detectors` tool + `ToolInfo`.
- Modify `src/vestigo/demo/metadata.py`, `src/vestigo/demo/build.py` — seeded detectors.
- Tests: create `tests/test_migration_0034.py`, `tests/test_timeline_detectors_api.py`; delete `tests/test_timeline_muted_methods_api.py`; modify `tests/test_auth_api.py`, `tests/test_postgres_store.py`, `tests/test_enrichers.py`, `tests/test_demo_detector_coverage_clickhouse.py`, `tests/test_agent_tools.py`.

Frontend (`frontend/src/`)
- Modify `api/types.ts`, `api/timelines.ts`.
- Create `hooks/useTimelineDetectors.ts`; delete `hooks/useMutedMethods.ts`, `hooks/useMethodFocus.ts`.
- Modify `hooks/useMethodFindings.ts`, `hooks/useScopeChange.ts`.
- Modify `components/analysis/method-registry.ts` — `useWhen` per method.
- Create `components/analysis/MethodKnobForm.tsx` (extracted from `InvestigateSheet.tsx`).
- Create `components/analysis/DetectorWizard.tsx`, `components/analysis/detector-wizard-summary.ts`, `components/analysis/DetectorStrip.tsx`.
- Modify `components/analysis/InvestigateRail.tsx`, `InvestigateSheet.tsx`, `InvestigateSheetHost.tsx`, `ToolsSheet.tsx`, `MethodRow.tsx`, `ScopeChangeDialog.tsx`; delete `DetectorMuteStrip.tsx`, `MethodFocusStrip.tsx`.
- Modify `pages/ExplorerPage.tsx` — wizard open state and "Add detector" button wiring.
- Tests: delete `test/detectorMute.test.tsx`, `test/methodFocus.test.tsx`, `test/methodFocusSheet.test.tsx`, `test/methodFocusSweep.test.tsx`; create `test/timelineDetectors.test.tsx`, `test/detectorWizard.test.tsx`, `test/detectorWizardSummary.test.ts`; modify `test/methodRegistry.test.ts`, `test/investigateRail.test.tsx`, `test/investigateSheet.test.tsx`, `test/investigateSheetHost.test.tsx`, `test/toolsSheet.test.tsx`, `test/scopeChange.test.tsx`, `test/designSystemBudget.ts`.

Docs
- Modify `docs/ANOMALY_DETECTION.md`, `docs/AGENT.md`, `docs/ROADMAP.md`, `docs/PROGRESS.md`, `CHANGELOG.md`, `CLAUDE.md`.

---

### Task 1: Migration 0034 and the `Timeline.detectors` column (W1, backend half)

**Files:**
- Create: `src/vestigo/db/migrations/versions/0034_timeline_detectors.py`
- Modify: `src/vestigo/db/postgres.py:380-386` (column), `:428` (`to_dict`), `:3383-3415` (delete `update_timeline_muted_methods`)
- Modify: `tests/test_postgres_store.py:290-291`, `tests/test_enrichers.py:476-477`
- Test: `tests/test_migration_0034.py`

**Interfaces:**
- Produces: `Timeline.detectors: list | None`; `Timeline.to_dict()["detectors"]: list[dict]` (empty list when None); `to_dict()` no longer has `muted_methods`.

- [ ] **Step 1: Write the failing migration test**

`tests/test_migration_0034.py`:

```python
"""Migration 0034 adds ``timelines.detectors`` and drops ``muted_methods``.

Every pre-existing timeline must come through the upgrade with *no* detector
configured — the new default is that nothing runs unprompted, and a column
that decides what an analyst is shown may not be guessed. The downgrade
restores ``muted_methods`` empty: the mute list only ever subtracted from a
sweep that no longer exists, so there is nothing to translate back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

import vestigo.db.postgres as pg


def _alembic(sync_conn: Any, verb: str, target: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(pg.__file__).parent / "migrations"))
    cfg.attributes["connection"] = sync_conn
    getattr(command, verb)(cfg, target)


async def _columns(conn) -> set[str]:
    rows = await conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'timelines'")
    )
    return {r[0] for r in rows.all()}


@pytest_asyncio.fixture()
async def engine(blank_pg_database):
    store = pg.PostgresStore(url=blank_pg_database)
    yield store.engine
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_preexisting_timeline_upgrades_with_no_detectors(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0033"))
        await conn.execute(
            text(
                "INSERT INTO timelines (id, case_id, name, is_default, muted_methods) "
                "VALUES ('t1', 'c1', 'All sources', true, '[\"entropy\"]')"
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0034"))
        row = (
            await conn.execute(text("SELECT name, detectors FROM timelines WHERE id = 't1'"))
        ).one()
        columns = await _columns(conn)

    assert row.name == "All sources"
    assert row.detectors is None
    assert "muted_methods" not in columns


@pytest.mark.asyncio
async def test_downgrade_restores_muted_methods_empty_and_keeps_the_row(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0034"))
        await conn.execute(
            text(
                "INSERT INTO timelines (id, case_id, name, is_default, detectors) "
                "VALUES ('t1', 'c1', 'All sources', true, "
                '\'[{"method": "entropy", "params": {}, "frame": "self", '
                '"baseline_id": null, "added_by": null, "added_at": "2026-09-03T00:00:00+00:00"}]\')'
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "downgrade", "0033"))
        columns = await _columns(conn)
        row = (
            await conn.execute(text("SELECT name, muted_methods FROM timelines WHERE id = 't1'"))
        ).one()

    assert "detectors" not in columns
    assert row.name == "All sources"
    assert row.muted_methods is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_migration_0034.py -v`
Expected: FAIL — alembic cannot resolve revision `0034`.

- [ ] **Step 3: Write the migration**

`src/vestigo/db/migrations/versions/0034_timeline_detectors.py`:

```python
"""Configured detectors per timeline; the mute list retired.

``detectors`` is the list of analysis methods an analyst has configured for
this timeline — ``{method, params, frame, baseline_id, added_by, added_at}``
per entry, at most one per method — and is the *only* thing the Investigate
rail runs. Nullable, so every pre-existing timeline reads as "nothing
configured": no statistical detector runs unprompted any more, and the safe
upgrade for a column that decides what an analyst is shown is the one that
shows nothing until asked.

``muted_methods`` (0028) existed only to subtract from the unprompted sweep.
With no sweep there is nothing to subtract from, so it is dropped rather than
carried as dead state. The downgrade restores it empty: a mute never said
which detector *should* run, so nothing translates in either direction.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("timelines", sa.Column("detectors", sa.JSON(), nullable=True))
    op.drop_column("timelines", "muted_methods")


def downgrade() -> None:
    op.add_column("timelines", sa.Column("muted_methods", sa.JSON(), nullable=True))
    op.drop_column("timelines", "detectors")
```

- [ ] **Step 4: Update the model**

In `src/vestigo/db/postgres.py`, replace the `muted_methods` column and its comment block (the `# A reading preference, never a gate…` comment through `muted_methods: Mapped[list | None] = mapped_column(JSON, nullable=True)`) with:

```python
    # The detectors an analyst has configured for this timeline — the only
    # thing the Investigate rail runs. Entries are
    # ``{method, params, frame, baseline_id, added_by, added_at}``; ``params``
    # is the same object ``/analysis/findings`` accepts for that method and is
    # validated with the same models before it is stored, so nothing here can
    # describe a run the runner would refuse. At most one entry per method:
    # the shape is a list so that rule can be lifted later without a migration.
    #
    # Shared, not per-browser, and audited on every change: which detectors an
    # investigation runs is a decision the next analyst inherits. Nothing runs
    # unprompted — an empty list is an empty rail, never a sweep.
    detectors: Mapped[list | None] = mapped_column(JSON, nullable=True)
```

In `to_dict`, replace `"muted_methods": self.muted_methods or [],` with `"detectors": self.detectors or [],`.

Delete the whole `update_timeline_muted_methods` method (`:3383-3415`).

- [ ] **Step 5: Fix the two pre-Alembic adoption tests**

In `tests/test_postgres_store.py:290-291` and `tests/test_enrichers.py:476-477`, replace

```python
        # 0028 adds the per-timeline muted analysis methods.
        await conn.execute(text("ALTER TABLE timelines DROP COLUMN muted_methods"))
```

with

```python
        # 0034 adds the per-timeline configured detectors.
        await conn.execute(text("ALTER TABLE timelines DROP COLUMN detectors"))
```

(The adoption test simulates a pre-Alembic schema by dropping columns later migrations add; `muted_methods` no longer exists at head, so dropping it would itself error.)

- [ ] **Step 6: Run the migration test and the adoption tests**

Run: `uv run pytest tests/test_migration_0034.py tests/test_migration_0028.py tests/test_postgres_store.py -v`
Expected: all PASS. (`test_migration_0028.py` still passes: it upgrades to 0028 only.)

- [ ] **Step 7: Commit**

```bash
git add src/vestigo/db/migrations/versions/0034_timeline_detectors.py src/vestigo/db/postgres.py tests/test_migration_0034.py tests/test_postgres_store.py tests/test_enrichers.py
git commit -m "feat(db): Timeline.detectors column, muted_methods dropped (migration 0034)"
```

Note: `cases.py` still references `update_timeline_muted_methods` after this commit; Task 2 removes it. The suite as a whole is red between Task 1 and Task 2 — that is expected, run only the files named in each task until Task 3.

---

### Task 2: Detector entry validation, store writers, and the two endpoints (W1 + W2)

**Files:**
- Modify: `src/vestigo/api/routers/analysis.py` (after `_adapt_params`, `:~409`)
- Modify: `src/vestigo/db/postgres.py` (where `update_timeline_muted_methods` was)
- Modify: `src/vestigo/api/routers/cases.py:100-110` (request models), `:1370-1414` (delete muted endpoint), add the two new endpoints after `update_timeline_field_overrides`
- Modify: `src/vestigo/api/routers/auth.py:80-124` (drop `_check_method_focus_value`, the `analysis_method_focus` key, `_MAX_FOCUS_METHODS`, `_MAX_FOCUS_FIELDS`)
- Delete: `tests/test_timeline_muted_methods_api.py`
- Modify: `tests/test_auth_api.py:250-330` (delete the four `*method_focus*` tests)
- Test: `tests/test_timeline_detectors_api.py`

**Interfaces:**
- Produces (analysis.py):
  ```python
  class DetectorEntryIn(BaseModel):
      params: dict[str, Any] = Field(default_factory=dict)
      frame: Literal["self", "baseline"] = "self"
      baseline_id: str | None = None


  def validate_detector_entry(method: str, entry: DetectorEntryIn) -> dict[str, Any]:
      """422 on unknown method, bad scope pair, or params the runner would reject.
      Returns {"method", "params", "frame", "baseline_id"} ready to store."""
  ```
- Produces (postgres.py):
  ```python
  async def set_timeline_detector(self, case_id: str, timeline_id: str, entry: dict[str, Any]) -> Timeline | None
  async def remove_timeline_detector(self, case_id: str, timeline_id: str, method: str) -> Timeline | None
  ```
- Produces (HTTP): `PUT /api/cases/{case_id}/timelines/{timeline_id}/detectors/{method}` body `DetectorEntryIn` → `{"timeline": {...}}`; `DELETE …/detectors/{method}` → `{"timeline": {...}}`; both `require_case_contribute` + `require_password_current`; audit actions `timeline.set_detector` / `timeline.remove_detector` with `detail={"method", "previous", "new"}`.

- [ ] **Step 1: Write the failing API tests**

`tests/test_timeline_detectors_api.py`:

```python
"""API tests for the per-timeline configured detectors (Milestone 12).

The list is the only thing the Investigate rail runs, so what it can hold is
held to the runner's own contract: an entry the findings endpoint would 422
on must not be storable, a baseline frame must name a baseline that exists on
this timeline, and every change is audited. One entry per method — a PUT on a
configured method edits it in place.
"""

from __future__ import annotations

import pytest

from tests.conftest import as_admin


def _case_and_timeline(client) -> tuple[str, str]:
    case_id = client.post("/api/cases/", json={"name": "Detector case"}).json()["case"]["id"]
    tid = client.post(f"/api/cases/{case_id}/timelines", json={"name": "t"}).json()["timeline"][
        "id"
    ]
    return case_id, tid


def _put(client, case_id, tid, method, body):
    return client.put(f"/api/cases/{case_id}/timelines/{tid}/detectors/{method}", json=body)


def _baseline(client, case_id, tid) -> str:
    resp = client.post(
        f"/api/cases/{case_id}/timelines/{tid}/baselines",
        json={
            "name": "week before",
            "baseline_start": "2026-01-01T00:00:00Z",
            "baseline_end": "2026-01-08T00:00:00Z",
            "suspect_windows": [
                {
                    "id": "w0",
                    "label": "day",
                    "start": "2026-01-08T00:00:00Z",
                    "end": "2026-01-09T00:00:00Z",
                }
            ],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["baseline"]["id"]


def test_timeline_starts_with_no_detectors(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    body = client.get(f"/api/cases/{case_id}/timelines/{tid}").json()["timeline"]
    assert body["detectors"] == []
    assert "muted_methods" not in body


def test_put_stores_the_entry_and_get_round_trips_it(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = _put(client, case_id, tid, "value_novelty", {"params": {"fields": ["user", "process"]}})
    assert resp.status_code == 200, resp.text
    [entry] = resp.json()["timeline"]["detectors"]
    assert entry["method"] == "value_novelty"
    assert entry["params"] == {"fields": ["user", "process"]}
    assert entry["frame"] == "self"
    assert entry["baseline_id"] is None
    assert (
        entry["added_by"] == admin_bootstrap["user_id"]
        if "user_id" in admin_bootstrap
        else entry["added_by"]
    )
    assert entry["added_at"].endswith("+00:00") or entry["added_at"].endswith("Z")

    again = client.get(f"/api/cases/{case_id}/timelines/{tid}").json()["timeline"]["detectors"]
    assert again == [entry]


def test_put_on_a_configured_method_replaces_it_in_place(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    _put(client, case_id, tid, "value_novelty", {"params": {}})
    _put(client, case_id, tid, "timestamp_order", {"params": {}})
    resp = _put(client, case_id, tid, "value_novelty", {"params": {"fields": ["user"]}})
    methods = [e["method"] for e in resp.json()["timeline"]["detectors"]]
    # One per method, and the edited one keeps its position.
    assert methods == ["value_novelty", "timestamp_order"]
    assert resp.json()["timeline"]["detectors"][0]["params"] == {"fields": ["user"]}


def test_delete_removes_only_that_method(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    _put(client, case_id, tid, "value_novelty", {"params": {}})
    _put(client, case_id, tid, "timestamp_order", {"params": {}})
    resp = client.delete(f"/api/cases/{case_id}/timelines/{tid}/detectors/value_novelty")
    assert resp.status_code == 200, resp.text
    assert [e["method"] for e in resp.json()["timeline"]["detectors"]] == ["timestamp_order"]


def test_delete_of_an_unconfigured_method_is_404(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = client.delete(f"/api/cases/{case_id}/timelines/{tid}/detectors/entropy")
    assert resp.status_code == 404


def test_unknown_method_is_rejected(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = _put(client, case_id, tid, "timestamp_oder", {"params": {}})
    assert resp.status_code == 422
    assert "timestamp_oder" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("value_novelty", {"fields": ["user"], "z_threshold": 2}),  # not a value_novelty key
        ("frequency", {"z_threshold": -1}),  # gt=0
        ("sequence_novelty", {"ngram_size": 9}),  # le=5
        ("log_template", {"order": "sideways"}),  # not in the Literal
    ],
)
def test_params_the_findings_endpoint_rejects_are_not_storable(
    client, admin_bootstrap, method, params
):
    """Exactly the validation `/analysis/findings` applies — same models."""
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = _put(client, case_id, tid, method, {"params": params})
    assert resp.status_code == 422, resp.text
    assert f"Invalid parameters for {method}" in resp.json()["detail"]
    assert client.get(f"/api/cases/{case_id}/timelines/{tid}").json()["timeline"]["detectors"] == []


def test_frame_and_baseline_must_agree(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    assert _put(client, case_id, tid, "frequency", {"frame": "baseline"}).status_code == 422
    assert (
        _put(client, case_id, tid, "frequency", {"frame": "self", "baseline_id": "b1"}).status_code
        == 422
    )


def test_baseline_must_exist_on_this_timeline(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = _put(client, case_id, tid, "frequency", {"frame": "baseline", "baseline_id": "nope"})
    assert resp.status_code == 422
    assert "baseline" in resp.json()["detail"].lower()

    baseline_id = _baseline(client, case_id, tid)
    resp = _put(
        client, case_id, tid, "frequency", {"frame": "baseline", "baseline_id": baseline_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["timeline"]["detectors"][0]["baseline_id"] == baseline_id


@pytest.mark.asyncio
async def test_changes_are_audited(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    _put(client, case_id, tid, "entropy", {"params": {}})
    client.delete(f"/api/cases/{case_id}/timelines/{tid}/detectors/entropy")

    entries = await store.query_audit(case_id=case_id)
    actions = [e.action for e in entries]
    assert "timeline.set_detector" in actions
    assert "timeline.remove_detector" in actions
    set_row = next(e for e in entries if e.action == "timeline.set_detector")
    assert set_row.detail["method"] == "entropy"
    assert set_row.detail["previous"] is None
    assert set_row.detail["new"]["method"] == "entropy"
    rm_row = next(e for e in entries if e.action == "timeline.remove_detector")
    assert rm_row.detail["previous"]["method"] == "entropy"
    assert rm_row.detail["new"] is None


def test_read_only_member_cannot_configure(client, admin_bootstrap):
    """Contribute access, like field overrides: the list changes what everyone runs."""
    from tests.conftest import login

    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    client.post(
        "/api/admin/users",
        json={"username": "reader", "password": "reader-pw-123456", "role": "analyst"},
    )
    client.post(f"/api/cases/{case_id}/members", json={"username": "reader", "access": "read"})
    login(client, "reader", "reader-pw-123456")
    resp = _put(client, case_id, tid, "entropy", {"params": {}})
    assert resp.status_code == 403
```

Adjust the admin-user and member-add calls in the last test to whatever `tests/test_timeline_field_overrides_api.py` uses for its read-only test — copy that test's setup verbatim rather than the guess above. Same for the baseline-create body: copy the payload from `tests/test_baselines_api.py` (or whichever test creates one through `POST …/baselines`). Drop the `added_by` assertion if `admin_bootstrap` does not expose the user id; assert `entry["added_by"]` is a non-empty string instead.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_timeline_detectors_api.py -v`
Expected: FAIL — 404/405 on the routes (and ImportError in `cases.py` for `update_timeline_muted_methods` until Step 5).

- [ ] **Step 3: Add the validator to `analysis.py`**

After `_adapt_params` in `src/vestigo/api/routers/analysis.py`:

```python
class DetectorEntryIn(BaseModel):
    """What the wizard stores for one configured detector.

    ``params`` is *exactly* the object ``GET …/analysis/findings`` takes for the
    method, kept in the client's own shape (a ``fields`` list stays a list) so
    the rail can hand it back verbatim; it is validated here with the same
    per-method model, so nothing storable describes a run the runner refuses.
    ``frame`` and ``baseline_id`` live in the entry rather than on the panel
    because they are part of what the detector *means*.
    """

    params: dict[str, Any] = Field(default_factory=dict)
    frame: Literal["self", "baseline"] = "self"
    baseline_id: str | None = None


def validate_detector_entry(method: str, entry: DetectorEntryIn) -> dict[str, Any]:
    """Reject what the findings endpoint would reject; return the storable entry.

    Three checks, each a 422 with the same wording the request it stands in
    for would have produced: the method must be one the plan knows, the
    frame/baseline pair must describe one question (``_validate_scope_args``),
    and the params must pass the method's own model (``_adapt_params``).
    Baseline *existence* is the caller's check — it needs the store.
    """
    if method not in METHOD_IDS:
        raise HTTPException(status_code=422, detail=f"Unknown analysis method: {method}")
    _validate_scope_args(entry.frame, entry.baseline_id)
    _adapt_params(method, entry.params)
    return {
        "method": method,
        "params": entry.params,
        "frame": entry.frame,
        "baseline_id": entry.baseline_id,
    }
```

`METHOD_IDS` is already imported in this module (`from vestigo.db.analysis_plan import … METHOD_IDS …`); confirm, and add `Literal`/`BaseModel` imports if missing.

- [ ] **Step 4: Add the store writers**

In `src/vestigo/db/postgres.py`, where `update_timeline_muted_methods` was (just before `update_timeline_field_overrides`):

```python
async def set_timeline_detector(
    self,
    case_id: str,
    timeline_id: str,
    entry: dict[str, Any],
) -> Timeline | None:
    """Add or replace the configured detector for ``entry["method"]``.

    One entry per method: an existing entry is replaced *in place*, so the
    rail's order — the order analysts added things — survives an edit.
    ``added_by``/``added_at`` are the caller's; validation of the entry
    against the runner's models happens at the API layer.

    Returns the updated timeline with sources eagerly loaded, or None.
    """
    from sqlalchemy import select

    async with self.session_factory() as session:
        result = await session.execute(
            select(Timeline).where(Timeline.case_id == case_id, Timeline.id == timeline_id)
        )
        timeline = result.scalar_one_or_none()
        if timeline is None:
            return None
        current = list(timeline.detectors or [])
        index = next((i for i, e in enumerate(current) if e.get("method") == entry["method"]), None)
        if index is None:
            current.append(entry)
        else:
            current[index] = entry
        timeline.detectors = current
        await session.commit()
        await session.refresh(timeline)
        await session.refresh(timeline, attribute_names=["sources"])
        return timeline


async def remove_timeline_detector(
    self,
    case_id: str,
    timeline_id: str,
    method: str,
) -> Timeline | None:
    """Remove the configured detector for ``method`` (no-op if absent).

    An emptied list is stored as None so "nothing configured" has one
    representation. Returns the updated timeline, or None if it does not
    exist.
    """
    from sqlalchemy import select

    async with self.session_factory() as session:
        result = await session.execute(
            select(Timeline).where(Timeline.case_id == case_id, Timeline.id == timeline_id)
        )
        timeline = result.scalar_one_or_none()
        if timeline is None:
            return None
        remaining = [e for e in (timeline.detectors or []) if e.get("method") != method]
        timeline.detectors = remaining or None
        await session.commit()
        await session.refresh(timeline)
        await session.refresh(timeline, attribute_names=["sources"])
        return timeline
```

- [ ] **Step 5: Replace the muted-methods endpoint with the two detector endpoints**

In `src/vestigo/api/routers/cases.py`:

1. Delete the `TimelineMutedMethodsUpdate` model (`:~100-104`, the one with `muted_methods: list[str] | None`).
2. Delete the whole `update_timeline_muted_methods` route (`:1370-1414`).
3. Add, after `update_timeline_field_overrides`:

```python
@router.put("/{case_id}/timelines/{timeline_id}/detectors/{method}")
async def set_timeline_detector(
    timeline_id: str,
    method: str,
    payload: DetectorEntryIn,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Configure ``method`` on this timeline, replacing an existing entry in place.

    The configured list is the only thing the Investigate rail runs, so what
    it may hold is held to the runner's contract: params are validated with
    the same models ``/analysis/findings`` uses, the frame/baseline pair must
    describe one question, and a baseline frame must name a definition that
    exists on *this* timeline — an id from another timeline would store a
    comparison that can never be run. Every change is audited: which
    detectors an investigation ran is part of its record.
    """
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    entry = validate_detector_entry(method, payload)
    if entry["baseline_id"] is not None:
        baseline = await store.get_baseline_definition(case.id, timeline_id, entry["baseline_id"])
        if baseline is None:
            raise HTTPException(
                status_code=422,
                detail=f"No baseline definition {entry['baseline_id']} on this timeline.",
            )
    entry["added_by"] = user.id
    entry["added_at"] = datetime.now(UTC).isoformat()
    previous = next((e for e in (timeline.detectors or []) if e.get("method") == method), None)
    updated = await store.set_timeline_detector(case.id, timeline_id, entry)
    if updated is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    await store.record_audit(
        action="timeline.set_detector",
        actor=user,
        case_id=case.id,
        target_type="timeline",
        target_id=timeline_id,
        detail={"method": method, "previous": previous, "new": entry},
    )
    return {"timeline": updated.to_dict()}


@router.delete("/{case_id}/timelines/{timeline_id}/detectors/{method}")
async def remove_timeline_detector(
    timeline_id: str,
    method: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Take ``method`` out of this timeline's configured detectors.

    404 when it was not configured: a delete that "succeeds" against nothing
    would audit a removal that removed nothing.
    """
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    previous = next((e for e in (timeline.detectors or []) if e.get("method") == method), None)
    if previous is None:
        raise HTTPException(status_code=404, detail=f"{method} is not configured on this timeline")
    updated = await store.remove_timeline_detector(case.id, timeline_id, method)
    if updated is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    await store.record_audit(
        action="timeline.remove_detector",
        actor=user,
        case_id=case.id,
        target_type="timeline",
        target_id=timeline_id,
        detail={"method": method, "previous": previous, "new": None},
    )
    return {"timeline": updated.to_dict()}
```

Add imports at the top of `cases.py`: `from datetime import UTC, datetime` (check whether `datetime` is already imported) and `from vestigo.api.routers.analysis import DetectorEntryIn, validate_detector_entry`. If importing `analysis` into `cases` creates a circular import (analysis imports `register_source_for_ingest`? — check `grep -n "from vestigo.api.routers.cases" src/vestigo/api/routers/analysis.py`), move `DetectorEntryIn`/`validate_detector_entry` and the two endpoints into `analysis.py` instead, under the same `/{case_id}/timelines/{timeline_id}/detectors/{method}` paths (its router prefix is also `/api/cases`).

- [ ] **Step 6: Remove the focus preference**

In `src/vestigo/api/routers/auth.py`: delete `_check_method_focus_value`, the `"analysis_method_focus": (dict, _check_method_focus_value),` line and its comment, and `_MAX_FOCUS_METHODS` / `_MAX_FOCUS_FIELDS` with their comment. In `tests/test_auth_api.py`, delete the four tests `test_update_my_preferences_stores_a_method_focus`, `test_method_focus_on_a_second_timeline_keeps_the_first`, `test_method_focus_rejects_anything_but_field_token_lists`, `test_method_focus_is_bounded_in_size`. Delete `tests/test_timeline_muted_methods_api.py`.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_timeline_detectors_api.py tests/test_timeline_field_overrides_api.py tests/test_auth_api.py tests/test_analysis_api.py -v` (substitute the real name of the analysis-endpoints test file: `ls tests | grep analysis`).
Expected: all PASS.

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add -A src/vestigo/api src/vestigo/db/postgres.py tests
git commit -m "feat(api): PUT/DELETE timeline detectors, validated with the findings models; mute list and method focus retired"
```

---

### Task 3: Agent parity — `list_configured_detectors` (W5)

**Files:**
- Modify: `src/vestigo/agent/tools.py:138-222` (`TOOL_REGISTRY`), and the tool registrations near `run_anomaly_detector` (`:1943`)
- Modify: `docs/AGENT.md:171` (tool table)
- Test: `tests/test_agent_tools.py` (new test beside `test_tool_registry_matches_registered_tools`, `:1966`)

**Interfaces:**
- Produces: MCP/agent tool `list_configured_detectors() -> {"detectors": [entry, ...], "note": str}`.

- [ ] **Step 1: Write the failing test**

Find how `tests/test_agent_tools.py` builds a tool server and calls a tool (look at the test nearest `list_baselines`, e.g. `grep -n "list_baselines" tests/test_agent_tools.py`), and copy that harness. The new test:

```python
@pytest.mark.asyncio
async def test_list_configured_detectors_reads_the_timeline_list(store, monkeypatch):
    # Same harness as the list_baselines test above this one.
    case_id, timeline_id, call = await _tool_harness(
        store, monkeypatch
    )  # whatever the file's helper is
    await store.set_timeline_detector(
        case_id,
        timeline_id,
        {
            "method": "value_novelty",
            "params": {"fields": ["user"]},
            "frame": "self",
            "baseline_id": None,
            "added_by": "u1",
            "added_at": "2026-09-03T00:00:00+00:00",
        },
    )
    result = await call("list_configured_detectors")
    assert [d["method"] for d in result["detectors"]] == ["value_novelty"]
    assert result["detectors"][0]["params"] == {"fields": ["user"]}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_agent_tools.py -k list_configured_detectors -v`
Expected: FAIL — unknown tool.

- [ ] **Step 3: Register the tool**

In `TOOL_REGISTRY`, directly after the `list_baselines` entry:

```python
(
    ToolInfo(
        "list_configured_detectors",
        "The detectors the case team has configured on this timeline (method, params, frame, baseline) — what the Investigate rail runs.",
    ),
)
```

In `build_tool_server`, directly before `run_anomaly_detector`:

```python
    @server.tool()
    async def list_configured_detectors() -> dict[str, Any]:
        """List the detectors configured on this timeline.

        These are the analysts' choices — the Investigate rail runs exactly this
        list and nothing else. Each entry names the method, the params it runs
        with (in the shape run_anomaly_detector takes: `fields`, `series_field`,
        thresholds), the frame (`self` or `baseline`) and the baseline id. Use
        it to say what has already been looked at before running something new.
        Read-only: configuring a detector is the analyst's act in the UI.
        """
        timeline = await get_store().get_timeline(scope.case_id, scope.timeline_id)
        detectors = list(timeline.detectors or []) if timeline is not None else []
        return {
            "detectors": detectors,
            "note": "Configured by analysts; this is what the Investigate rail runs. "
            "run_anomaly_detector runs a method ad hoc without changing this list.",
        }
```

(`get_store` and `scope` are what the neighbouring tools use — match their names.)

- [ ] **Step 4: Document it**

In `docs/AGENT.md` tool table, after the `run_anomaly_detector` row:

```
| `list_configured_detectors` | | The detectors analysts configured on the timeline — the list the Investigate rail runs. Read-only; `run_anomaly_detector` stays the ad hoc path. |
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_agent_tools.py tests/test_agent_api.py tests/test_agent_schema.py -v`
Expected: PASS, including `test_tool_registry_matches_registered_tools`.

- [ ] **Step 6: Commit**

```bash
git add src/vestigo/agent/tools.py docs/AGENT.md tests/test_agent_tools.py
git commit -m "feat(agent): list_configured_detectors tool"
```

---

### Task 4: Demo seed (W6)

**Files:**
- Modify: `src/vestigo/demo/metadata.py` (after `baseline_windows`, `:901`)
- Modify: `src/vestigo/demo/build.py:212-220` (`_artifacts`, after `create_baseline_definition`)
- Test: `tests/test_demo_detector_coverage_clickhouse.py`

**Interfaces:**
- Produces: `metadata.DEMO_DETECTORS: tuple[DemoDetector, ...]`, `metadata.detector_entries(baseline_id: str, user_id: str) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_demo_detector_coverage_clickhouse.py`:

```python
#: Service function per configured method, for the seeded-detector check.
_SERVICE_BY_METHOD = {
    "value_novelty": "find_value_novelty",
    "timestamp_order": "find_order_violations",
    "frequency": "find_frequency_anomalies",
    "sequence_novelty": "find_sequence_novelty",
    "proportion_shift": "find_proportion_shifts",
}


def test_seeded_detectors_validate_against_the_runner():
    """Every entry the demo stores must be one the endpoint would accept."""
    from vestigo.api.routers.analysis import DetectorEntryIn, validate_detector_entry
    from vestigo.demo import metadata

    for entry in metadata.detector_entries(baseline_id="b", user_id="u"):
        validate_detector_entry(
            entry["method"],
            DetectorEntryIn(
                params=entry["params"], frame=entry["frame"], baseline_id=entry["baseline_id"]
            ),
        )
    methods = [e["method"] for e in metadata.detector_entries("b", "u")]
    assert len(methods) == len(set(methods)), "one entry per method"


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(d, id=d.method)
        for d in __import__("vestigo.demo.metadata", fromlist=["DEMO_DETECTORS"]).DEMO_DETECTORS
    ],
)
def test_seeded_detector_finds_something(demo, ch_store, entry):
    """The first thing a new user sees must not be an empty rail."""
    case_id, sources, windows = demo
    service = StatisticalAnomalyService(ch_store)
    kwargs = dict(entry.params)
    if "fields" in kwargs and isinstance(kwargs["fields"], str):
        kwargs["fields"] = kwargs["fields"].split(",")
    if entry.frame == "baseline":
        kwargs["windows"] = windows
    result = getattr(service, _SERVICE_BY_METHOD[entry.method])(case_id, sources, **kwargs)
    assert _findings(result), f"seeded {entry.method} found nothing in the demo case"
```

Replace the `__import__` trick with a plain `from vestigo.demo import metadata` at the top of the file and `metadata.DEMO_DETECTORS` in the parametrize. Check how `test_windowed_detector_finds_something` instantiates the service (`service = …` at `:96-98`) and use the same construction.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_demo_detector_coverage_clickhouse.py -k seeded -v`
Expected: FAIL — `metadata` has no `DEMO_DETECTORS`.

- [ ] **Step 3: Add the seed data**

In `src/vestigo/demo/metadata.py`, after `baseline_windows()`:

```python
@dataclass(frozen=True)
class DemoDetector:
    """One detector the demo timeline ships pre-configured.

    Chosen so the first open of the Investigate rail shows real findings and a
    worked example of what the wizard produces — every entry is asserted to
    find something in ``tests/test_demo_detector_coverage_clickhouse.py``.
    """

    method: str
    params: dict[str, Any]
    #: ``baseline`` entries resolve to the demo's one baseline definition.
    frame: str = "self"


#: On the "Full incident" timeline. Two windowless methods that work the
#: moment ingestion finishes, three comparisons against the three quiet weeks.
DEMO_DETECTORS: tuple[DemoDetector, ...] = (
    DemoDetector("value_novelty", {}),
    DemoDetector("timestamp_order", {}),
    DemoDetector("frequency", {"series_field": "artifact"}, frame="baseline"),
    # Host order is what lateral movement changes; the default series
    # (``artifact``) is one value per demo source and would say nothing.
    DemoDetector("sequence_novelty", {"series_field": "attr:computer_name"}, frame="baseline"),
    DemoDetector("proportion_shift", {}, frame="baseline"),
)


def detector_entries(baseline_id: str, user_id: str) -> list[dict[str, Any]]:
    """The stored shape of :data:`DEMO_DETECTORS`, in the order the rail shows them."""
    added_at = scenario.BASELINE_END.isoformat()
    return [
        {
            "method": d.method,
            "params": dict(d.params),
            "frame": d.frame,
            "baseline_id": baseline_id if d.frame == "baseline" else None,
            "added_by": user_id,
            "added_at": added_at,
        }
        for d in DEMO_DETECTORS
    ]
```

Add `from dataclasses import dataclass` and `from typing import Any` if not already imported in `metadata.py`.

In `src/vestigo/demo/build.py::_artifacts`, change the `create_baseline_definition` call to keep its result and seed the entries:

```python
    baseline = await store.create_baseline_definition(
        case_id=case_id,
        timeline_id=full_timeline,
        name="May 2026 — three quiet weeks vs the intrusion",
        baseline_start=scenario.SCENARIO_START,
        baseline_end=scenario.BASELINE_END,
        suspect_windows=metadata.baseline_windows(),
        created_by=user_id,
    )
    # The detectors the analyst "already configured": what the rail runs on
    # first open, and a filled-in example of the wizard's output.
    for entry in metadata.detector_entries(baseline.id, user_id):
        await store.set_timeline_detector(case_id, full_timeline, entry)
```

- [ ] **Step 4: Run the coverage file**

Run: `uv run pytest tests/test_demo_detector_coverage_clickhouse.py -v`
Expected: PASS. If a seeded entry finds nothing, change the entry's params (not the detector) — the file's docstring says why.

- [ ] **Step 5: Commit**

```bash
git add src/vestigo/demo tests/test_demo_detector_coverage_clickhouse.py
git commit -m "feat(demo): seed five configured detectors on the Full incident timeline"
```

---

### Task 5: Frontend types, API client, and `useTimelineDetectors` (W4 plumbing)

**Files:**
- Modify: `frontend/src/api/types.ts:147-152`, `frontend/src/api/timelines.ts:51-62`
- Create: `frontend/src/hooks/useTimelineDetectors.ts`
- Delete: `frontend/src/hooks/useMutedMethods.ts`, `frontend/src/hooks/useMethodFocus.ts`, `frontend/src/components/analysis/DetectorMuteStrip.tsx`, `frontend/src/components/analysis/MethodFocusStrip.tsx`, `frontend/src/test/detectorMute.test.tsx`, `frontend/src/test/methodFocus.test.tsx`, `frontend/src/test/methodFocusSheet.test.tsx`, `frontend/src/test/methodFocusSweep.test.tsx`
- Modify: `frontend/src/test/designSystemBudget.ts:42` (remove the `DetectorMuteStrip.tsx` line, and any `MethodFocusStrip.tsx` line)
- Test: `frontend/src/test/timelineDetectors.test.tsx`

**Interfaces:**
- Produces (`api/types.ts`):
  ```ts
  export interface DetectorEntry {
    method: MethodId;
    params: Record<string, unknown>;
    frame: "self" | "baseline";
    baseline_id: string | null;
    added_by: string | null;
    added_at: string;
  }
  // Timeline: `detectors: DetectorEntry[]` replaces `muted_methods: string[]`.
  ```
- Produces (`api/timelines.ts`):
  ```ts
  putDetector(caseId, timelineId, method: MethodId, body: { params: Record<string, unknown>; frame: "self" | "baseline"; baseline_id: string | null }): Promise<Timeline>
  deleteDetector(caseId, timelineId, method: MethodId): Promise<Timeline>
  ```
- Produces (`hooks/useTimelineDetectors.ts`):
  ```ts
  export interface TimelineDetectors {
    entries: DetectorEntry[];              // in stored order, unknown methods dropped
    byMethod: Map<MethodId, DetectorEntry>;
    set: (method: MethodId, body: DetectorBody) => Promise<Timeline>;
    remove: (method: MethodId) => Promise<Timeline>;
    canEdit: boolean;
    isSaving: boolean;
    saveError: string | null;
  }
  export function useTimelineDetectors(caseId: string, timelineId: string): TimelineDetectors
  export function scopeOf(entry: DetectorEntry): ScopeParams   // {frame:"self"} | {frame:"baseline", baseline_id}
  ```

- [ ] **Step 1: Write the failing hook test**

`frontend/src/test/timelineDetectors.test.tsx`:

```tsx
/**
 * useTimelineDetectors — the configured list is shared server state on the
 * Timeline, read through the same ["timeline", case, timeline] query the
 * Explorer already holds, and every write lands straight in that cache so a
 * just-added detector starts fetching without a round trip.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useTimelineDetectors, scopeOf } from "@/hooks/useTimelineDetectors";

const server = vi.hoisted(() => ({
  detectors: [] as Record<string, unknown>[],
  puts: [] as unknown[],
  deletes: [] as string[],
}));

vi.mock("@/api/timelines", () => ({
  timelinesApi: {
    get: async () => ({ id: "t1", case_id: "c1", detectors: server.detectors }),
    putDetector: async (_c: string, _t: string, method: string, body: unknown) => {
      server.puts.push({ method, body });
      const entry = { method, ...(body as object), added_by: "u1", added_at: "2026-09-03T00:00:00Z" };
      server.detectors = [...server.detectors.filter((d) => d.method !== method), entry];
      return { id: "t1", case_id: "c1", detectors: server.detectors };
    },
    deleteDetector: async (_c: string, _t: string, method: string) => {
      server.deletes.push(method);
      server.detectors = server.detectors.filter((d) => d.method !== method);
      return { id: "t1", case_id: "c1", detectors: server.detectors };
    },
  },
}));
vi.mock("@/api/cases", () => ({
  casesApi: { get: async () => ({ id: "c1", access: "contribute", role: "owner" }) },
}));
vi.mock("@/lib/caseAccess", () => ({ canContributeToCase: () => true }));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useTimelineDetectors", () => {
  beforeEach(() => {
    server.detectors = [];
    server.puts = [];
    server.deletes = [];
  });

  it("reads the timeline's list and drops methods this build does not know", async () => {
    server.detectors = [
      { method: "value_novelty", params: {}, frame: "self", baseline_id: null },
      { method: "from_the_future", params: {}, frame: "self", baseline_id: null },
    ];
    const { result } = renderHook(() => useTimelineDetectors("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.entries).toHaveLength(1));
    expect(result.current.byMethod.get("value_novelty")?.method).toBe("value_novelty");
  });

  it("writes through the API and updates the cache from the response", async () => {
    const { result } = renderHook(() => useTimelineDetectors("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.canEdit).toBe(true));
    await result.current.set("entropy", { params: { fields: ["user"] }, frame: "self", baseline_id: null });
    await waitFor(() => expect(result.current.entries.map((e) => e.method)).toEqual(["entropy"]));
    expect(server.puts).toEqual([
      { method: "entropy", body: { params: { fields: ["user"] }, frame: "self", baseline_id: null } },
    ]);
    await result.current.remove("entropy");
    await waitFor(() => expect(result.current.entries).toEqual([]));
    expect(server.deletes).toEqual(["entropy"]);
  });

  it("derives scope params from an entry", () => {
    expect(scopeOf({ method: "frequency", params: {}, frame: "self", baseline_id: null, added_by: null, added_at: "" })).toEqual({ frame: "self" });
    expect(scopeOf({ method: "frequency", params: {}, frame: "baseline", baseline_id: "b1", added_by: null, added_at: "" })).toEqual({ frame: "baseline", baseline_id: "b1" });
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/test/timelineDetectors.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Types and API client**

In `frontend/src/api/types.ts`, replace the `muted_methods` field and its doc comment on `Timeline` with:

```ts
  /**
   * The detectors analysts configured for this timeline — the only thing the
   * Investigate rail runs. One entry per method; shared and audited.
   */
  detectors: DetectorEntry[];
```

and add, above `Timeline`:

```ts
import type { MethodId } from "@/components/analysis/method-registry";

/** One configured detector, as stored on the Timeline. */
export interface DetectorEntry {
  method: MethodId;
  /** Exactly what `/analysis/findings` takes as `params` for this method. */
  params: Record<string, unknown>;
  frame: "self" | "baseline";
  baseline_id: string | null;
  added_by: string | null;
  added_at: string;
}
```

(If `types.ts` importing from `components/` creates an import cycle that vitest complains about, declare `method: string` here and narrow in the hook — `api/analysis.ts` already imports `MethodId` from the registry, so it should be fine.)

In `frontend/src/api/timelines.ts`, replace `patchMutedMethods` with:

```ts
  /**
   * Configure one detector on this timeline (replaces an existing entry for
   * the method). Shared, audited state: it is the list the rail runs.
   */
  putDetector: (
    caseId: string,
    timelineId: string,
    method: MethodId,
    body: { params: Record<string, unknown>; frame: "self" | "baseline"; baseline_id: string | null },
  ) =>
    put<{ timeline: Timeline }>(
      `/cases/${caseId}/timelines/${timelineId}/detectors/${method}`,
      body,
    ).then((r) => r.timeline),

  deleteDetector: (caseId: string, timelineId: string, method: MethodId) =>
    del<{ timeline: Timeline }>(
      `/cases/${caseId}/timelines/${timelineId}/detectors/${method}`,
    ).then((r) => r.timeline),
```

Check `frontend/src/api/client.ts` exports a `put` helper; if only `patch`/`post`/`get`/`del` exist, add `put` beside `patch` with the same shape. Import `MethodId` type.

- [ ] **Step 4: The hook**

`frontend/src/hooks/useTimelineDetectors.ts`:

```ts
/**
 * useTimelineDetectors — the detectors configured on this timeline.
 *
 * Shared server state on the Timeline, not a browser preference: which
 * detectors an investigation runs is a decision the next analyst inherits,
 * and every change is audited. This list is the *only* thing the Investigate
 * rail runs — nothing runs unprompted.
 *
 * Reads through the same `["timeline", caseId, timelineId]` query ExplorerPage
 * already holds; writes land in that cache from the response so a just-added
 * detector starts fetching without a round trip.
 */
import { useCallback, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { timelinesApi } from "@/api/timelines";
import { casesApi } from "@/api/cases";
import { canContributeToCase } from "@/lib/caseAccess";
import { METHODS, type MethodId } from "@/components/analysis/method-registry";
import type { ScopeParams } from "@/api/analysis";
import type { DetectorEntry, Timeline } from "@/api/types";

const KNOWN = new Set<string>(METHODS.map((m) => m.id));

export type DetectorBody = Pick<DetectorEntry, "params" | "frame" | "baseline_id">;

export interface TimelineDetectors {
  /** Stored order, filtered to methods this client knows about. */
  entries: DetectorEntry[];
  byMethod: Map<MethodId, DetectorEntry>;
  set: (method: MethodId, body: DetectorBody) => Promise<Timeline>;
  remove: (method: MethodId) => Promise<Timeline>;
  canEdit: boolean;
  isSaving: boolean;
  saveError: string | null;
}

/** The scope params a configured entry's findings request carries. */
export function scopeOf(entry: DetectorEntry): ScopeParams {
  return entry.frame === "baseline" && entry.baseline_id
    ? { frame: "baseline", baseline_id: entry.baseline_id }
    : { frame: "self" };
}

export function useTimelineDetectors(caseId: string, timelineId: string): TimelineDetectors {
  const queryClient = useQueryClient();
  const timelineKey = ["timeline", caseId, timelineId];

  const { data: timeline } = useQuery({
    queryKey: timelineKey,
    queryFn: () => timelinesApi.get(caseId, timelineId),
    enabled: Boolean(caseId && timelineId),
  });
  const { data: case_ } = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => casesApi.get(caseId),
    enabled: Boolean(caseId),
  });

  // An entry for a method this build does not know is dropped rather than
  // carried: the rail could neither run nor name it.
  const entries = useMemo(
    () => (timeline?.detectors ?? []).filter((e) => KNOWN.has(e.method)),
    [timeline?.detectors],
  );
  const byMethod = useMemo(() => new Map(entries.map((e) => [e.method, e])), [entries]);

  const onSuccess = (updated: Timeline) => {
    queryClient.setQueryData(timelineKey, updated);
    queryClient.invalidateQueries({ queryKey: ["timelines", caseId] });
  };
  const setMutation = useMutation({
    mutationFn: ({ method, body }: { method: MethodId; body: DetectorBody }) =>
      timelinesApi.putDetector(caseId, timelineId, method, body),
    onSuccess,
  });
  const removeMutation = useMutation({
    mutationFn: (method: MethodId) => timelinesApi.deleteDetector(caseId, timelineId, method),
    onSuccess,
  });

  const canEdit = case_ ? canContributeToCase(case_) : false;
  const error = setMutation.error ?? removeMutation.error;

  return {
    entries,
    byMethod,
    set: useCallback(
      (method: MethodId, body: DetectorBody) => setMutation.mutateAsync({ method, body }),
      [setMutation],
    ),
    remove: useCallback((method: MethodId) => removeMutation.mutateAsync(method), [removeMutation]),
    canEdit,
    isSaving: setMutation.isPending || removeMutation.isPending,
    saveError: error ? (error as Error).message : null,
  };
}
```

- [ ] **Step 5: Delete the retired files**

```bash
cd frontend/src
git rm hooks/useMutedMethods.ts hooks/useMethodFocus.ts components/analysis/DetectorMuteStrip.tsx components/analysis/MethodFocusStrip.tsx test/detectorMute.test.tsx test/methodFocus.test.tsx test/methodFocusSheet.test.tsx test/methodFocusSweep.test.tsx
```

Remove the `DetectorMuteStrip.tsx` line (and its comment, `:38-42`) from `frontend/src/test/designSystemBudget.ts`, plus a `MethodFocusStrip.tsx` line if present.

- [ ] **Step 6: Run the new test**

Run: `cd frontend && npx vitest run src/test/timelineDetectors.test.tsx`
Expected: PASS. (Typecheck will still fail elsewhere — importers of the deleted hooks are fixed in Tasks 6–10.)

- [ ] **Step 7: Commit**

```bash
git add -A frontend/src
git commit -m "feat(frontend): DetectorEntry type, detectors API, useTimelineDetectors; mute and focus hooks removed"
```

---

### Task 6: The sweep runs the configured list (W4, hook half)

**Files:**
- Modify: `frontend/src/hooks/useMethodFindings.ts`
- Modify: `frontend/src/hooks/useScopeChange.ts` (drop `methodsToRerun`), `frontend/src/components/analysis/ScopeChangeDialog.tsx:29-50`, `frontend/src/test/scopeChange.test.tsx:24`, `frontend/src/test/investigateSheetHost.test.tsx:48`
- Test: extend `frontend/src/test/timelineDetectors.test.tsx` with a sweep section (model it on the deleted `methodFocusSweep.test.tsx`, which drove the real hooks with `@/api/analysis` mocked — see `git show HEAD~1:frontend/src/test/methodFocusSweep.test.tsx`).

**Interfaces:**
- Consumes: `useTimelineDetectors`, `scopeOf` (Task 5).
- Produces:
  ```ts
  export interface MethodState { …existing…; entry?: DetectorEntry; configured: boolean }
  export function useMethodFindings(caseId, timelineId, method, opts: { enabled: boolean; params?: Record<string, unknown>; scope?: ScopeParams })
  export function useStreamingSweep(caseId, timelineId): { byMethod; scope; done; total; planLoading; configured: DetectorEntry[]; planById }
  ```
  `runnable(id)` is now "has a configured entry". `findingsQueryOptions` is unchanged in key shape — the sweep passes `scopeOf(entry)` and `entry.params`, so the key for a configured run is `["anomalies", case, timeline, method, "analysis", entry.frame, entry.baseline_id ?? "none", JSON(entry.params), 50, dismissedKey]`.

- [ ] **Step 1: Write the failing sweep tests**

Append to `frontend/src/test/timelineDetectors.test.tsx` a second `describe` that mocks `@/api/analysis` (`analysisApi.plan` returning every method `applicable`; `analysisApi.findings` recording `{method, frame, baseline_id, params}` and returning `{method, results: [], total_findings: 0, dismissed_count: 0, warnings: [], scope: {...}, cache: "miss"}`), renders `useStreamingSweep("c1","t1")` with `renderHook`, and asserts:

```tsx
  it("issues no findings query when nothing is configured", async () => {
    server.detectors = [];
    const { result } = renderHook(() => useStreamingSweep("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.planLoading).toBe(false));
    await new Promise((r) => setTimeout(r, 50));
    expect(asked.calls).toEqual([]);
    expect(result.current.total).toBe(0);
  });

  it("runs exactly the configured entries with their own params and scope", async () => {
    server.detectors = [
      { method: "value_novelty", params: { fields: ["user"] }, frame: "self", baseline_id: null },
      { method: "frequency", params: { series_field: "artifact" }, frame: "baseline", baseline_id: "b1" },
    ];
    const { result } = renderHook(() => useStreamingSweep("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.done).toBe(2));
    expect(asked.calls).toEqual(
      expect.arrayContaining([
        { method: "value_novelty", frame: "self", baseline_id: undefined, params: { fields: ["user"] } },
        { method: "frequency", frame: "baseline", baseline_id: "b1", params: { series_field: "artifact" } },
      ]),
    );
    expect(asked.calls).toHaveLength(2);
    expect(result.current.byMethod.entropy.configured).toBe(false);
    expect(result.current.byMethod.frequency.entry?.baseline_id).toBe("b1");
  });

  it("runs a configured method even when the plan calls it not_applicable", async () => {
    plan.status = "not_applicable";
    server.detectors = [{ method: "charset", params: {}, frame: "self", baseline_id: null }];
    const { result } = renderHook(() => useStreamingSweep("c1", "t1"), { wrapper });
    await waitFor(() => expect(result.current.done).toBe(1));
    expect(asked.calls.map((c) => c.method)).toEqual(["charset"]);
  });
```

`asked` and `plan` are `vi.hoisted` recorders like `server`. Sigma is not involved in this hook.

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/test/timelineDetectors.test.tsx`
Expected: the new cases FAIL (the sweep still reads the plan and the deleted hooks).

- [ ] **Step 3: Rewrite `useStreamingSweep`**

In `frontend/src/hooks/useMethodFindings.ts`:

1. Replace the imports of `useMethodFocus` and `useMutedMethods` with `import { scopeOf, useTimelineDetectors } from "./useTimelineDetectors";` and `import type { DetectorEntry } from "@/api/types";`.
2. Update the header comment: the sweep runs the timeline's configured detectors, each under its own scope and params; the plan is fetched only for the wizard's cards and the Tools accounting.
3. Extend `MethodState` with `entry?: DetectorEntry;` and `configured: boolean;`.
4. `useMethodFindings` gains `scope?: ScopeParams` in `opts`, used instead of `useScopeParams()` when given:
   ```ts
   const globalScope = useScopeParams();
   const scopeParams = opts.scope ?? globalScope;
   ```
5. Replace the body of `useStreamingSweep` from `const { muted } = useMutedMethods…` through the `heavyResults` block with:

```ts
  const { entries, byMethod: entryByMethod } = useTimelineDetectors(caseId, timelineId);
  // A configured entry is the whole decision: not the plan (advice, never a
  // lock — a not_applicable method an analyst configured anyway still runs),
  // not a mute (retired), not a per-user focus (retired). One list, shared.
  const runnable = useCallback((id: MethodId) => entryByMethod.has(id), [entryByMethod]);

  const optionsFor = (id: MethodId, enabled: boolean) => {
    const entry = entryByMethod.get(id);
    return findingsQueryOptions(
      caseId,
      timelineId,
      id,
      entry ? scopeOf(entry) : scopeParams,
      entry?.params ?? {},
      enabled && entry !== undefined,
      includeDismissed,
    );
  };

  const cheapResults = useQueries({ queries: CHEAP_IDS.map((id) => optionsFor(id, true)) });
  const cheapSettled = cheapResults.every(
    (q, i) => !runnable(CHEAP_IDS[i]) || q.isFetched || q.isError,
  );
  const heavyResults = useQueries({
    queries: HEAVY_IDS.map((id) => optionsFor(id, cheapSettled)),
  });
```

   Keep `scopeParams = useScopeParams()` (still used for unconfigured placeholders and returned `scope`). In `byMethod` construction add `entry: entryByMethod.get(meta.id), configured: entryByMethod.has(meta.id),` and add `entryByMethod` to the memo deps. Return `{ byMethod, scope, done: settled, total: expected.length, planLoading, planById, configured: entries }`.

- [ ] **Step 4: Scope-change dialog no longer promises re-runs**

Configured entries carry their own scope, so changing the panel scope re-runs nothing. In `hooks/useScopeChange.ts` delete `methodsToRerun` (and the now-unused `METHODS`/`planById` reads if nothing else uses them). In `components/analysis/ScopeChangeDialog.tsx` remove the `methodsToRerun` prop and the sentence rendering it; the dialog keeps the affected-verdicts count and says the new scope applies to ad hoc runs from the sheet and the Explore tab. Update `test/scopeChange.test.tsx:24` and `test/investigateSheetHost.test.tsx:48` to drop the prop, and `InvestigateSheetHost.tsx:172`.

- [ ] **Step 5: Run the tests**

Run: `cd frontend && npx vitest run src/test/timelineDetectors.test.tsx src/test/scopeChange.test.tsx`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A frontend/src
git commit -m "feat(frontend): the sweep runs the configured detectors, each under its own scope"
```

---

### Task 7: Registry `useWhen` copy and `MethodKnobForm` extraction (W3 prerequisites)

**Files:**
- Modify: `frontend/src/components/analysis/method-registry.ts` (`MethodMeta`, all twelve entries)
- Create: `frontend/src/components/analysis/MethodKnobForm.tsx`
- Modify: `frontend/src/components/analysis/InvestigateSheet.tsx:110-390` (`MethodBody` uses the form; focus UI removed)
- Modify: `frontend/src/test/methodRegistry.test.ts`, `frontend/src/test/investigateSheet.test.tsx` (remove focus assertions; knob tests keep passing via the same `data-testid`s)

**Interfaces:**
- Produces (`method-registry.ts`): `MethodMeta.useWhen: string` — one sentence, "Use this when …".
- Produces (`MethodKnobForm.tsx`):
  ```ts
  export function buildParams(meta: MethodMeta, raw: Record<string,string>, fields: Record<string, string[]|null>): Record<string, unknown>
  export function knobBlocker(meta: MethodMeta, fields: Record<string, string[]|null>): string | null
  export function seedFromParams(meta: MethodMeta, params: Record<string, unknown>): { values: Record<string,string>; fields: Record<string, string[]|null> }
  export function MethodKnobForm(props: {
    caseId: string; timelineId: string; methodId: MethodId;
    initialParams?: Record<string, unknown>;
    /** Called on every change with the params as they stand and why they cannot run (or null). */
    onChange: (params: Record<string, unknown>, blocker: string | null) => void;
    /** Show each knob's help text under the control (the wizard); the sheet keeps it compact. */
    verbose?: boolean;
  }): JSX.Element
  ```

- [ ] **Step 1: Write the failing registry test**

In `frontend/src/test/methodRegistry.test.ts` add:

```ts
  it("tells a beginner when each method is worth configuring", () => {
    for (const m of METHODS) {
      expect(m.useWhen.startsWith("Use this when")).toBe(true);
      expect(m.useWhen.length).toBeGreaterThan(30);
      expect(m.useWhen.length).toBeLessThan(200);
    }
  });
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/test/methodRegistry.test.ts`
Expected: FAIL — `useWhen` undefined.

- [ ] **Step 3: Add `useWhen` to the registry**

In `MethodMeta`, after `hint`:

```ts
  /**
   * When to configure it, in one sentence for the wizard's card. Starts with
   * "Use this when" — a test enforces it — so the twelve cards read as one list.
   */
  useWhen: string;
```

And per method (add the line after `hint:` in each entry):

| id | useWhen |
|---|---|
| value_novelty | `Use this when you want the rarest values in a field surfaced first — a user, host or process that almost never appears. Works with no baseline.` |
| value_combo | `Use this when two fields are each ordinary on their own but their pairing might not be — an account on a host it never logs into.` |
| numeric_range | `Use this when a numeric field has a normal band and you want values far outside it — bytes transferred, durations, ports.` |
| charset | `Use this when a field should only ever contain a fixed alphabet and a stray script or injected byte would matter — usernames, hostnames, paths.` |
| entropy | `Use this when random-looking strings would be a lead — encoded payloads, generated domains, packed command lines.` |
| frequency | `Use this when a change in volume over time is the question — a burst, a silence, a series that spiked in one window. Best with a baseline.` |
| proportion_shift | `Use this when you have a baseline and want the values whose share of events changed most in the suspect window. Needs a baseline.` |
| value_distribution_drift | `Use this when whole fields, not single values, may have changed shape between the baseline and the suspect window. Needs a baseline.` |
| interval_periodicity | `Use this when beaconing or a missed heartbeat is the question — a value that arrives on a new rhythm, or stopped arriving on its old one. Needs a baseline.` |
| timestamp_order | `Use this when you need to know whether the records themselves are sound — timestamps running backwards inside a source. No baseline, cheap.` |
| sequence_novelty | `Use this when the order things happened in matters — a host sequence, a command chain — and you want orderings never seen in the baseline. Needs a baseline.` |
| log_template | `Use this when the logs are unstructured and you want to see their shapes first — rare line structures surface without naming a field.` |

- [ ] **Step 4: Extract `MethodKnobForm`**

Create `frontend/src/components/analysis/MethodKnobForm.tsx` by moving `buildParams` and `knobBlocker` verbatim from `InvestigateSheet.tsx:110-165` (export both), adding:

```ts
/**
 * Turn a stored params object back into the form's two state maps, so the
 * wizard's edit mode opens on what is configured rather than on "auto".
 * A `fields` value may be a list or the comma string the backend also accepts.
 */
export function seedFromParams(
  meta: MethodMeta,
  params: Record<string, unknown>,
): { values: Record<string, string>; fields: Record<string, string[] | null> } {
  const values: Record<string, string> = {};
  const fields: Record<string, string[] | null> = {};
  for (const knob of meta.knobs) {
    const raw = params[knob.param];
    if (raw === undefined || raw === null) continue;
    if (knob.kind === "fields") {
      fields[knob.param] = Array.isArray(raw) ? raw.map(String) : String(raw).split(",");
    } else {
      values[knob.param] = String(raw);
    }
  }
  return { values, fields };
}
```

and the component. Its body is the `<form>`'s children from `MethodBody` (`InvestigateSheet.tsx:236-290`: the `meta.knobs.map(...)` rendering `AnomalyFieldPicker` / `MethodFieldSelect` / `<input>`) **without** the Run button, the focus button, the focus note, or the `<form>` element itself, with this state and reporting:

```tsx
export function MethodKnobForm({ caseId, timelineId, methodId, initialParams, onChange, verbose = false }: Props) {
  const meta = METHODS_BY_ID[methodId];
  const seed = useMemo(() => seedFromParams(meta, initialParams ?? {}), [meta, initialParams]);
  const [values, setValues] = useState<Record<string, string>>(seed.values);
  const [fields, setFields] = useState<Record<string, string[] | null>>(seed.fields);
  // Re-seed only when the method changes — the same guard MethodBody had.
  const seededFor = useRef(methodId);
  useEffect(() => {
    if (seededFor.current === methodId) return;
    seededFor.current = methodId;
    setValues(seed.values);
    setFields(seed.fields);
  }, [methodId, seed]);

  const { forMethod, declare, canEdit, saveError } = useFieldOverrides(caseId, timelineId);
  const overrides = forMethod(methodId);

  useEffect(() => {
    onChange(buildParams(meta, values, fields), knobBlocker(meta, fields));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- report on state change only
  }, [values, fields, meta]);

  return (
    <div className="flex flex-wrap items-start gap-2" data-testid="method-knob-form">
      {meta.knobs.map((knob) => (
        <div key={knob.param} className={verbose ? "w-full" : undefined}>
          {/* …the existing per-knob rendering, unchanged, with the same data-testids… */}
          {verbose && <p className="mt-1 text-[11px] text-[var(--color-fg-muted)]">{knobHelp(knob)}</p>}
        </div>
      ))}
      {saveError && (
        <p data-testid="field-declare-error" className="w-full text-xs text-[var(--color-danger)]">
          Field declaration not saved: {saveError}
        </p>
      )}
    </div>
  );
}

/** One line under each control in the wizard — the guidance attached to the knob. */
function knobHelp(knob: MethodKnob): string {
  switch (knob.param) {
    case "fields":
      return "Which fields to scan. Auto lets Vestigo pick from this timeline's inventory, applying the pins and exclusions the case team declared.";
    case "series_field":
      return "The field whose values form the series — one series per value. Pick something with a few distinct values that mean something, like a host.";
    case "group_field":
      return "Learn one alphabet per value of this field instead of one for the whole scope.";
    case "z_threshold":
      return "How far from the expected count a bucket must be to count as a spike or silence. Higher is stricter.";
    case "fdr_q":
      return "The share of reported findings you are willing to have be false discoveries. 0.05 means one in twenty.";
    case "min_ratio":
      return "The smallest change worth reporting, as a ratio. 2.0 means the share at least doubled or halved.";
    case "min_skew_seconds":
      return "Ignore timestamps that run backwards by less than this many seconds.";
    case "ngram_size":
      return "How many consecutive events form one sequence. Three is a good default.";
    case "max_gap_seconds":
      return "Break a sequence when consecutive events are farther apart than this.";
    case "field":
      return "The text field to cluster into templates. Usually the message.";
    case "order":
      return "Which templates to show first: most common, first seen, or last seen.";
    case "only_new":
      return "Only templates that never appeared in the baseline window.";
    default:
      return knob.label;
  }
}
```

Then in `InvestigateSheet.tsx`, rewrite `MethodBody` to: keep the prose, the Parameters subhead, the query-shape block; replace its knob state, `useMethodFocus`, `seededFields`, the focus button and note with

```tsx
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [blocker, setBlocker] = useState<string | null>(null);
  const onFormChange = useCallback((p: Record<string, unknown>, b: string | null) => {
    setParams(p);
    setBlocker(b);
  }, []);
  …
      <form onSubmit={(e) => { e.preventDefault(); if (blocker) return; onRun?.(params); }}>
        <MethodKnobForm caseId={caseId} timelineId={timelineId} methodId={methodId} onChange={onFormChange} />
        {onRun && (<Button type="submit" …>…</Button>)}
      </form>
      {onRun && blocker && (<p data-testid="method-knob-blocker" …>{blocker}</p>)}
```

Delete the `useMethodFocus` import, `Crosshair` icon import, and the `saveError` rendering now inside the form. Remove `buildParams`/`knobBlocker` from the sheet (import from the new file if anything else there needs them).

- [ ] **Step 5: Fix sheet tests**

In `frontend/src/test/investigateSheet.test.tsx`, delete any test mentioning focus (`method-focus-note`, "Focus on this selection", `useMethodFocus`), and delete the `vi.mock("@/hooks/useMethodFocus", …)` block. Knob tests keyed on `method-knob`, `method-knob-<param>`, `method-knob-blocker` keep working.

- [ ] **Step 6: Run tests and typecheck**

Run: `cd frontend && npx vitest run src/test/methodRegistry.test.ts src/test/investigateSheet.test.tsx`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A frontend/src
git commit -m "feat(frontend): method-registry useWhen copy; MethodKnobForm extracted from the sheet"
```

---

### Task 8: `DetectorWizard` (W3)

**Files:**
- Create: `frontend/src/components/analysis/detector-wizard-summary.ts`, `frontend/src/components/analysis/DetectorWizard.tsx`
- Test: `frontend/src/test/detectorWizardSummary.test.ts`, `frontend/src/test/detectorWizard.test.tsx`
- Modify: `frontend/src/test/designSystemBudget.ts` (add a budget line for `DetectorWizard.tsx` only if the test demands one; prefer `Button` and `Card`-like primitives so it does not)

**Interfaces:**
- Consumes: `useTimelineDetectors` (Task 5), `useAnalysisPlan` (existing), `MethodKnobForm` (Task 7), `baselinesApi.list` (existing, returns `{ baselines: { id, name, … }[] }`), `Dialog`/`DialogContent` (`components/ui/Dialog.tsx`).
- Produces:
  ```ts
  // detector-wizard-summary.ts
  export const NEEDS_BASELINE: ReadonlySet<MethodId>  // proportion_shift, value_distribution_drift, interval_periodicity, sequence_novelty
  export function summarize(meta: MethodMeta, params: Record<string, unknown>, frame: "self"|"baseline", baselineName: string | null): string
  // DetectorWizard.tsx
  export function DetectorWizard(props: {
    caseId: string; timelineId: string;
    open: boolean; onOpenChange: (open: boolean) => void;
    /** Open straight on step 2 for this method (edit mode when configured). */
    initialMethod?: MethodId | null;
    onOpenSignatures: () => void;
  }): JSX.Element
  ```

- [ ] **Step 1: Write the failing summary test**

`frontend/src/test/detectorWizardSummary.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { METHODS_BY_ID } from "@/components/analysis/method-registry";
import { NEEDS_BASELINE, summarize } from "@/components/analysis/detector-wizard-summary";

describe("detector wizard summary", () => {
  it("names the method, the fields and the scope in one sentence", () => {
    expect(
      summarize(METHODS_BY_ID.value_novelty, { fields: ["user", "process"] }, "baseline", "week before"),
    ).toBe("Rare values over user and process, comparing to baseline “week before”. Cheap scan.");
  });

  it("says auto when the method picks its own fields", () => {
    expect(summarize(METHODS_BY_ID.charset, {}, "self", null)).toBe(
      "Charset novelty over fields Vestigo picks, across the whole timeline. Full scan.",
    );
  });

  it("names a series field and a threshold when set", () => {
    expect(
      summarize(METHODS_BY_ID.frequency, { series_field: "attr:host", z_threshold: 3 }, "self", null),
    ).toBe("Frequency anomalies per attr:host, z threshold 3, across the whole timeline. Cheap scan.");
  });

  it("knows which methods cannot run without a baseline", () => {
    expect([...NEEDS_BASELINE].sort()).toEqual([
      "interval_periodicity",
      "proportion_shift",
      "sequence_novelty",
      "value_distribution_drift",
    ]);
  });
});
```

Check `METHODS_BY_ID.frequency.costClass` and `.label` in the registry and adjust the expected strings to the real labels/cost classes rather than the other way round.

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/test/detectorWizardSummary.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the summary module**

`frontend/src/components/analysis/detector-wizard-summary.ts`:

```ts
/**
 * The plain-language sentence the wizard's confirm step shows, kept pure so
 * it can be tested without rendering: it is the one place the analyst reads
 * back what is about to be stored, so it has to say every part of it.
 */
import type { MethodId, MethodMeta } from "./method-registry";

/** Methods that compare a baseline window against suspect windows — no self frame. */
export const NEEDS_BASELINE: ReadonlySet<MethodId> = new Set<MethodId>([
  "proportion_shift",
  "value_distribution_drift",
  "interval_periodicity",
  "sequence_novelty",
]);

function fieldsClause(params: Record<string, unknown>): string | null {
  const raw = params.fields;
  if (raw === undefined || raw === null) return null;
  const list = Array.isArray(raw) ? raw.map(String) : String(raw).split(",");
  if (list.length === 0) return null;
  if (list.length === 1) return list[0];
  return `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

export function summarize(
  meta: MethodMeta,
  params: Record<string, unknown>,
  frame: "self" | "baseline",
  baselineName: string | null,
): string {
  const parts: string[] = [];
  const hasFieldsKnob = meta.knobs.some((k) => k.kind === "fields");
  if (hasFieldsKnob) {
    parts.push(`over ${fieldsClause(params) ?? "fields Vestigo picks"}`);
  }
  if (typeof params.series_field === "string") parts.push(`per ${params.series_field}`);
  for (const knob of meta.knobs) {
    if (knob.kind !== "number") continue;
    const v = params[knob.param];
    if (v === undefined || v === null || v === "") continue;
    parts.push(`${knob.label.toLowerCase()} ${v}`);
  }
  const scope =
    frame === "baseline"
      ? `comparing to baseline “${baselineName ?? "(unnamed)"}”`
      : "across the whole timeline";
  const cost = meta.costClass === "heavy" ? "Full scan." : "Cheap scan.";
  const head = parts.length ? `${meta.label} ${parts.join(", ")}` : meta.label;
  return `${head}, ${scope}. ${cost}`;
}
```

Run the summary test; adjust joins until it passes.

- [ ] **Step 4: Write the failing wizard component test**

`frontend/src/test/detectorWizard.test.tsx` — mock `@/hooks/useTimelineDetectors` (recording `set`), `@/hooks/useAnalysisPlan` (`useAnalysisPlan` returning `planById` with `charset` `not_applicable` and reason facts, others `applicable`, `interval_periodicity` `needs_setup`), `@/components/analysis/MethodKnobForm` (a stub that calls `onChange({ fields: ["user"] }, null)` once on mount), and `@/api/baselines` (`list` → `{ baselines: [{ id: "b1", name: "week before" }] }`). Render `<DetectorWizard caseId="c1" timelineId="t1" open onOpenChange={() => {}} onOpenSignatures={sig} />` inside `QueryClientProvider`. Assert:

```tsx
  it("lists every method with its use-when line, its plan verdict and its cost", () => {
    for (const m of METHODS) {
      const card = screen.getByTestId(`wizard-card-${m.id}`);
      expect(card).toHaveTextContent(m.useWhen);
      expect(card).toHaveTextContent(m.costClass === "heavy" ? "Full scan" : "Cheap");
    }
    expect(screen.getByTestId("wizard-card-charset")).toHaveTextContent("Cannot apply");
    expect(screen.getByTestId("wizard-card-interval_periodicity")).toHaveTextContent("Needs a baseline");
  });

  it("still lets a not_applicable method be chosen", () => {
    fireEvent.click(screen.getByTestId("wizard-card-charset"));
    expect(screen.getByTestId("wizard-step-configure")).toBeInTheDocument();
  });

  it("routes the Signatures card to the Signatures tab", () => {
    fireEvent.click(screen.getByTestId("wizard-card-sigma"));
    expect(sig).toHaveBeenCalled();
  });

  it("walks choose → configure → confirm and stores the entry", async () => {
    fireEvent.click(screen.getByTestId("wizard-card-value_novelty"));
    fireEvent.click(screen.getByTestId("wizard-next"));
    expect(screen.getByTestId("wizard-summary")).toHaveTextContent("Rare values over user, across the whole timeline.");
    fireEvent.click(screen.getByTestId("wizard-apply"));
    await waitFor(() => expect(setCalls).toEqual([
      { method: "value_novelty", body: { params: { fields: ["user"] }, frame: "self", baseline_id: null } },
    ]));
  });

  it("requires a baseline for the comparison methods and offers the timeline's definitions", () => {
    fireEvent.click(screen.getByTestId("wizard-card-proportion_shift"));
    expect(screen.getByTestId("wizard-next")).toBeDisabled();
    fireEvent.change(screen.getByTestId("wizard-baseline"), { target: { value: "b1" } });
    expect(screen.getByTestId("wizard-next")).not.toBeDisabled();
    fireEvent.click(screen.getByTestId("wizard-next"));
    expect(screen.getByTestId("wizard-summary")).toHaveTextContent("comparing to baseline “week before”");
  });

  it("opens in edit mode on a configured method with its stored params", () => {
    // render with initialMethod="value_novelty" and the hook's byMethod holding an entry
    expect(screen.getByTestId("wizard-step-configure")).toBeInTheDocument();
    expect(screen.getByTestId("wizard-apply-label")).toHaveTextContent("Save changes");
  });
```

- [ ] **Step 5: Run to verify it fails**

Run: `cd frontend && npx vitest run src/test/detectorWizard.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 6: Write the wizard**

`frontend/src/components/analysis/DetectorWizard.tsx`:

```tsx
/**
 * DetectorWizard — how a detector gets onto a timeline.
 *
 * Nothing runs unprompted. An analyst picks a method from a list that says,
 * per card, when it is useful, what the analysis gate thinks of it on *this*
 * data and what it will cost; configures it with the same knob form the sheet
 * uses; reads back one sentence saying exactly what will be stored; applies.
 * The result is a shared, audited entry on the Timeline that the rail then
 * runs — through the same findings endpoint and cache as any ad hoc run.
 *
 * The gate stays advice here as everywhere: a `not_applicable` card is still
 * selectable, with the arithmetic behind the verdict beside it, and the
 * analyst who disagrees with it configures the method anyway.
 */
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, ShieldCheck } from "lucide-react";
import { Dialog, DialogContent } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { baselinesApi } from "@/api/baselines";
import { useAnalysisPlan } from "@/hooks/useAnalysisPlan";
import { useTimelineDetectors } from "@/hooks/useTimelineDetectors";
import { METHODS, METHODS_BY_ID, type MethodId } from "./method-registry";
import { MethodKnobForm } from "./MethodKnobForm";
import { NEEDS_BASELINE, summarize } from "./detector-wizard-summary";
import { cn } from "@/lib/cn";

type Step = "choose" | "configure" | "confirm";

interface Props {
  caseId: string;
  timelineId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialMethod?: MethodId | null;
  onOpenSignatures: () => void;
}

function facts(reasonFacts: Record<string, number | string | boolean> | undefined): string | null {
  if (!reasonFacts) return null;
  const parts = Object.entries(reasonFacts).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`);
  return parts.length ? parts.join(" · ") : null;
}

export function DetectorWizard({ caseId, timelineId, open, onOpenChange, initialMethod, onOpenSignatures }: Props) {
  const detectors = useTimelineDetectors(caseId, timelineId);
  const { planById } = useAnalysisPlan(caseId, timelineId);
  const { data: baselines } = useQuery({
    queryKey: ["baselines", caseId, timelineId],
    queryFn: () => baselinesApi.list(caseId, timelineId),
    enabled: open,
  });

  const [step, setStep] = useState<Step>("choose");
  const [method, setMethod] = useState<MethodId | null>(null);
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [blocker, setBlocker] = useState<string | null>(null);
  const [frame, setFrame] = useState<"self" | "baseline">("self");
  const [baselineId, setBaselineId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const existing = method ? detectors.byMethod.get(method) : undefined;

  // Reset on every open. `initialMethod` opens straight on configure —
  // editing a configured entry, or adding one from a group header.
  useEffect(() => {
    if (!open) return;
    setError(null);
    const entry = initialMethod ? detectors.byMethod.get(initialMethod) : undefined;
    setMethod(initialMethod ?? null);
    setParams(entry?.params ?? {});
    setBlocker(null);
    setFrame(entry?.frame ?? (initialMethod && NEEDS_BASELINE.has(initialMethod) ? "baseline" : "self"));
    setBaselineId(entry?.baseline_id ?? null);
    setStep(initialMethod ? "configure" : "choose");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- seed once per open
  }, [open, initialMethod]);

  const choose = (id: MethodId) => {
    const entry = detectors.byMethod.get(id);
    setMethod(id);
    setParams(entry?.params ?? {});
    setFrame(entry?.frame ?? (NEEDS_BASELINE.has(id) ? "baseline" : "self"));
    setBaselineId(entry?.baseline_id ?? null);
    setStep("configure");
  };

  const baselineName = useMemo(
    () => baselines?.baselines.find((b) => b.id === baselineId)?.name ?? null,
    [baselines, baselineId],
  );
  const scopeOk = frame === "self" || baselineId !== null;
  const canProceed = method !== null && blocker === null && scopeOk;

  const apply = async () => {
    if (!method || !canProceed) return;
    setError(null);
    try {
      await detectors.set(method, { params, frame, baseline_id: frame === "baseline" ? baselineId : null });
      onOpenChange(false);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={step === "choose" ? "Add a detector" : existing ? `Edit ${METHODS_BY_ID[method!].label}` : `Configure ${METHODS_BY_ID[method!].label}`}
        description={
          step === "choose"
            ? "Nothing runs until you choose it. Each detector answers one kind of question; pick the one that matches yours."
            : step === "configure"
              ? METHODS_BY_ID[method!].what
              : "This is exactly what will be stored and run for everyone on the case."
        }
        className="max-w-2xl"
      >
        {step === "choose" && (
          <div className="grid gap-2 sm:grid-cols-2" data-testid="wizard-step-choose">
            {METHODS.map((m) => {
              const plan = planById[m.id];
              const configured = detectors.byMethod.has(m.id);
              return (
                <button
                  key={m.id}
                  type="button"
                  data-testid={`wizard-card-${m.id}`}
                  onClick={() => choose(m.id)}
                  className={cn(
                    "flex flex-col gap-1 rounded border p-2 text-left text-xs transition-base hover:border-[var(--color-border-strong)]",
                    plan?.status === "not_applicable" ? "border-dashed border-[var(--color-border)]" : "border-[var(--color-border)] bg-[var(--color-bg-elevated)]",
                  )}
                >
                  <span className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
                    <m.icon size={12} className="text-[var(--color-fg-muted)]" />
                    {m.label}
                    {configured && <Check size={11} className="text-[var(--color-accent)]" aria-label="configured" />}
                    <span className="ml-auto font-normal text-[var(--color-fg-muted)]">
                      {m.costClass === "heavy" ? "Full scan" : "Cheap"}
                    </span>
                  </span>
                  <span className="text-[var(--color-fg-secondary)]">{m.useWhen}</span>
                  {plan?.status === "not_applicable" && (
                    <span className="text-[var(--color-warning)]">
                      Cannot apply here: {plan.reason}
                      {facts(plan.reason_facts) ? ` (${facts(plan.reason_facts)})` : ""}. You can still configure it.
                    </span>
                  )}
                  {plan?.status === "needs_setup" && (
                    <span className="text-[var(--color-fg-muted)]">Needs a baseline — you will pick one next.</span>
                  )}
                </button>
              );
            })}
            <button
              type="button"
              data-testid="wizard-card-sigma"
              onClick={() => { onOpenChange(false); onOpenSignatures(); }}
              className="flex flex-col gap-1 rounded border border-[var(--color-border)] p-2 text-left text-xs transition-base hover:border-[var(--color-border-strong)]"
            >
              <span className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
                <ShieldCheck size={12} className="text-[var(--color-fg-muted)]" />
                Signatures (Sigma)
              </span>
              <span className="text-[var(--color-fg-secondary)]">
                Use this when you want known techniques matched by name. Runs from the Signatures tab and stays there.
              </span>
            </button>
          </div>
        )}

        {step === "configure" && method && (
          <div data-testid="wizard-step-configure" className="space-y-3">
            <MethodKnobForm
              caseId={caseId}
              timelineId={timelineId}
              methodId={method}
              initialParams={existing?.params}
              onChange={(p, b) => { setParams(p); setBlocker(b); }}
              verbose
            />
            <fieldset className="space-y-1 text-xs">
              <legend className="font-semibold text-[var(--color-fg-secondary)]">Compare against</legend>
              {!NEEDS_BASELINE.has(method) && (
                <label className="flex items-center gap-2">
                  <input type="radio" name="frame" checked={frame === "self"} onChange={() => setFrame("self")} />
                  The whole timeline (self-baseline)
                </label>
              )}
              <label className="flex items-center gap-2">
                <input type="radio" name="frame" checked={frame === "baseline"} onChange={() => setFrame("baseline")} />
                A baseline definition
              </label>
              {frame === "baseline" && (
                <select
                  data-testid="wizard-baseline"
                  value={baselineId ?? ""}
                  onChange={(e) => setBaselineId(e.target.value || null)}
                  className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1 text-xs"
                >
                  <option value="">Pick a baseline…</option>
                  {(baselines?.baselines ?? []).map((b) => (
                    <option key={b.id} value={b.id}>{b.name}</option>
                  ))}
                </select>
              )}
              {frame === "baseline" && (baselines?.baselines.length ?? 0) === 0 && (
                <p className="text-[var(--color-fg-muted)]">
                  No baseline on this timeline yet. Build one from Tools → Scope, then come back.
                </p>
              )}
              <p className="text-[var(--color-fg-muted)]">
                {NEEDS_BASELINE.has(method)
                  ? "This method compares a known-normal window against suspect windows, so it needs a baseline."
                  : "With a baseline, the method learns from the baseline window and reports on the suspect windows instead of the whole timeline."}
              </p>
            </fieldset>
            {blocker && <p data-testid="wizard-blocker" className="text-xs text-[var(--color-warning)]">{blocker}</p>}
            <div className="flex justify-between">
              <Button variant="ghost" size="sm" onClick={() => setStep("choose")}><ArrowLeft size={11} /> Back</Button>
              <Button variant="outline" size="sm" data-testid="wizard-next" disabled={!canProceed} onClick={() => setStep("confirm")}>
                Next <ArrowRight size={11} />
              </Button>
            </div>
          </div>
        )}

        {step === "confirm" && method && (
          <div data-testid="wizard-step-confirm" className="space-y-3">
            <p data-testid="wizard-summary" className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3 text-sm text-[var(--color-fg-primary)]">
              {summarize(METHODS_BY_ID[method], params, frame, baselineName)}
            </p>
            <p className="text-xs text-[var(--color-fg-muted)]">
              Stored on the timeline for everyone on the case and recorded in the audit trail. The first open pays the scan; later opens read the cached answer until the data or the settings change.
            </p>
            {error && <p data-testid="wizard-error" className="text-xs text-[var(--color-danger)]">Not saved: {error}</p>}
            <div className="flex justify-between">
              <Button variant="ghost" size="sm" onClick={() => setStep("configure")}><ArrowLeft size={11} /> Back</Button>
              <Button size="sm" data-testid="wizard-apply" disabled={detectors.isSaving || !detectors.canEdit} onClick={() => void apply()}>
                <span data-testid="wizard-apply-label">{existing ? "Save changes" : "Apply"}</span>
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
```

Check `Button`'s variant/size names against `components/ui/Button.tsx` and the `baselinesApi.list` response type against `api/baselines.ts`.

- [ ] **Step 7: Run the wizard tests**

Run: `cd frontend && npx vitest run src/test/detectorWizard.test.tsx src/test/detectorWizardSummary.test.ts src/test/designSystemBudget.test.ts`
Expected: PASS. If the budget test flags `DetectorWizard.tsx`, replace raw `<button>` cards with the `Button` primitive or add an honest budget line with a comment saying the cards are selectable surfaces, not buttons.

- [ ] **Step 8: Commit**

```bash
git add -A frontend/src
git commit -m "feat(frontend): DetectorWizard — choose, configure, confirm"
```

---

### Task 9: The rail — configured detectors only (W4, UI half)

**Files:**
- Create: `frontend/src/components/analysis/DetectorStrip.tsx`
- Modify: `frontend/src/components/analysis/InvestigateRail.tsx` (rewrite), `frontend/src/pages/ExplorerPage.tsx:1700-1740` (wizard state + button), `frontend/src/test/designSystemBudget.ts:47`
- Test: `frontend/src/test/investigateRail.test.tsx`

**Interfaces:**
- Consumes: `useStreamingSweep` (Task 6: `byMethod[id].configured`, `.entry`, `configured`), `useTimelineDetectors`, `DetectorWizard` (Task 8).
- Produces: `InvestigateRail` props gain `onAddDetector: (method?: MethodId) => void` (opens the wizard, optionally on a method) and lose nothing else; `DetectorStrip({ entries, byMethod, canEdit, onEdit(method), onRemove(method), baselineNames })`.

Deviation from the roadmap text: the per-detector edit/remove lives in a strip above the feed (`DetectorStrip`) rather than on the evidence-class group headers. The groups interleave methods by rank inside one list, so a header per detector would either split the feed or repeat a control; the strip is one place that names every configured detector, its scope, and its count. Task 12 updates the roadmap wording.

- [ ] **Step 1: Rewrite the rail tests**

In `frontend/src/test/investigateRail.test.tsx`: the existing harness mocks `useStreamingSweep` via `sweep.current` and builds per-method `state(id, …)`. Add `configured: true, entry: {method: id, params: {}, frame: "self", baseline_id: null, added_by: null, added_at: ""}` to the `state()` helper's defaults, mock `@/hooks/useTimelineDetectors` (returning `entries` derived from `sweep.current`, `canEdit: true`, recording `remove`), and replace the preset/mute/skipped tests with:

```tsx
  it("shows the empty state with the wizard entry when nothing is configured", () => {
    sweep.current = { byMethod: {}, done: 0, total: 0, planLoading: false, configured: [] };
    render(<InvestigateRail {...props} />, { wrapper });
    expect(screen.getByTestId("no-detectors")).toHaveTextContent("No detectors configured");
    fireEvent.click(screen.getByTestId("add-detector"));
    expect(props.onAddDetector).toHaveBeenCalledWith(undefined);
  });

  it("lists each configured detector with its scope and count, editable and removable", () => {
    sweep.current = { byMethod: { value_novelty: state("value_novelty", { findings: [finding()], total: 1 }) }, done: 1, total: 1, planLoading: false, configured: [entryOf("value_novelty")] };
    render(<InvestigateRail {...props} />, { wrapper });
    const chip = screen.getByTestId("detector-chip-value_novelty");
    expect(chip).toHaveTextContent("Rare values");
    expect(chip).toHaveTextContent("whole timeline");
    expect(chip).toHaveTextContent("1");
    fireEvent.click(within(chip).getByTitle("Edit"));
    expect(props.onAddDetector).toHaveBeenCalledWith("value_novelty");
    fireEvent.click(within(chip).getByTitle("Remove"));
    expect(removed).toEqual(["value_novelty"]);
  });

  it("never renders a group for an unconfigured method", () => {
    sweep.current = { byMethod: { entropy: state("entropy", { configured: false, entry: undefined }) }, done: 0, total: 0, planLoading: false, configured: [] };
    render(<InvestigateRail {...props} />, { wrapper });
    expect(screen.queryByTestId("detector-chip-entropy")).toBeNull();
  });

  it("says 'no findings' only once every configured detector settled", () => {
    sweep.current = { byMethod: { entropy: state("entropy", { pending: true }) }, done: 0, total: 1, planLoading: false, configured: [entryOf("entropy")] };
    const { rerender } = render(<InvestigateRail {...props} />, { wrapper });
    expect(screen.queryByText(/No findings/)).toBeNull();
    sweep.current = { byMethod: { entropy: state("entropy", { pending: false }) }, done: 1, total: 1, planLoading: false, configured: [entryOf("entropy")] };
    rerender(<InvestigateRail {...props} />);
    expect(screen.getByText(/No findings from the configured detectors/)).toBeInTheDocument();
  });
```

Keep the existing tests for the dismissed toggle, the errored-methods banner, the sweep progress bar and the marker publication; delete tests referencing presets, mutes, focus, `skipped-summary`, or `ScopeStrip`.

- [ ] **Step 2: Run to verify they fail**

Run: `cd frontend && npx vitest run src/test/investigateRail.test.tsx`
Expected: FAIL.

- [ ] **Step 3: `DetectorStrip`**

`frontend/src/components/analysis/DetectorStrip.tsx`:

```tsx
/**
 * DetectorStrip — every detector configured on this timeline, in one row.
 *
 * Replaces the mute strip. There is nothing to mute any more: a detector is
 * either configured (and runs) or not (and does not exist here). Each chip
 * names the method, the scope it runs under and what it found, and carries
 * the two acts an analyst with contribute access can take — edit (the wizard
 * on this method) and remove. Read-only members see the chips without them.
 */
import { Pencil, X } from "lucide-react";
import type { DetectorEntry } from "@/api/types";
import type { MethodState } from "@/hooks/useMethodFindings";
import { METHODS_BY_ID, type MethodId } from "./method-registry";

interface Props {
  entries: DetectorEntry[];
  byMethod: Record<MethodId, MethodState>;
  baselineNames: Record<string, string>;
  canEdit: boolean;
  onEdit: (method: MethodId) => void;
  onRemove: (method: MethodId) => void;
}

export function DetectorStrip({ entries, byMethod, baselineNames, canEdit, onEdit, onRemove }: Props) {
  if (entries.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1" data-testid="detector-strip">
      {entries.map((entry) => {
        const meta = METHODS_BY_ID[entry.method];
        const state = byMethod[entry.method];
        const scope =
          entry.frame === "baseline"
            ? `vs ${baselineNames[entry.baseline_id ?? ""] ?? "baseline"}`
            : "whole timeline";
        return (
          <span
            key={entry.method}
            data-testid={`detector-chip-${entry.method}`}
            className="flex items-center gap-1 rounded-full border border-[var(--color-border)] px-2 py-0.5 text-[10px] text-[var(--color-fg-secondary)]"
          >
            <meta.icon size={10} className="text-[var(--color-fg-muted)]" />
            {meta.label}
            <span className="text-[var(--color-fg-muted)]">· {scope}</span>
            <span className="font-mono">
              {state?.pending ? "…" : state?.error ? "!" : String(state?.total ?? 0)}
            </span>
            {canEdit && (
              <>
                <button type="button" title="Edit" onClick={() => onEdit(entry.method)} className="rounded p-0.5 hover:text-[var(--color-accent)]">
                  <Pencil size={9} />
                </button>
                <button type="button" title="Remove" onClick={() => onRemove(entry.method)} className="rounded p-0.5 hover:text-[var(--color-danger)]">
                  <X size={9} />
                </button>
              </>
            )}
          </span>
        );
      })}
    </div>
  );
}
```

Add `"../components/analysis/DetectorStrip.tsx": { fontSize: 3, rawButton: 2 }` to `designSystemBudget.ts` if the test asks for it (the mute strip's line, removed in Task 5, is the precedent).

- [ ] **Step 4: Rewrite `InvestigateRail`**

In `frontend/src/components/analysis/InvestigateRail.tsx`:

- Remove: `PRESETS`, `preset` state, the preset pill row (keep the Dismissed toggle, now alone right-aligned in that row), `useMutedMethods`, `useMethodFocus`, `DetectorMuteStrip`, `MethodFocusStrip`, `ScopeStrip` (scope is per detector now and shown on its chip), `skipped`, `presetMuted`, the `skipped-summary` button, and the three old empty states.
- Add props: `onAddDetector: (method?: MethodId) => void`.
- Add: `const detectors = useTimelineDetectors(caseId, timelineId);` and a `baselines` query (`["baselines", caseId, timelineId]`, `baselinesApi.list`) to build `baselineNames`.
- `visible` becomes `METHODS.filter((m) => byMethod[m.id]?.configured)`; markers iterate `visible` only.
- Header row, before the strip:

```tsx
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
          Detectors
        </span>
        {detectors.canEdit && (
          <Button variant="ghost" size="sm" data-testid="add-detector" onClick={() => onAddDetector()}>
            <Plus size={11} /> Add detector
          </Button>
        )}
      </div>
      <DetectorStrip
        entries={detectors.entries}
        byMethod={byMethod}
        baselineNames={baselineNames}
        canEdit={detectors.canEdit}
        onEdit={(m) => onAddDetector(m)}
        onRemove={(m) => void detectors.remove(m)}
      />
```

- Empty state, rendered instead of the groups when `detectors.entries.length === 0 && sigmaFindings.length === 0`:

```tsx
        <AnalysisEmptyState
          hint="Nothing runs until you choose it. Each detector answers one kind of question — the wizard says which."
        >
          <span data-testid="no-detectors">No detectors configured on this timeline.</span>
          {detectors.canEdit && (
            <Button variant="outline" size="sm" className="mt-2" onClick={() => onAddDetector()}>
              <Plus size={11} /> Add a detector
            </Button>
          )}
        </AnalysisEmptyState>
```

  (If `AnalysisEmptyState` only accepts text children, give it the sentence and render the button as a sibling.) With detectors configured but nothing found and every one settled: `No findings from the configured detectors.` with hint `Edit a detector's fields or scope, or add another kind of question.`

- Sigma rows still render in the `named` group when present (unchanged).

- [ ] **Step 5: Wire the wizard in `ExplorerPage`**

In `frontend/src/pages/ExplorerPage.tsx` next to the `sheet` state (`:395`):

```tsx
  const [wizard, setWizard] = useState<{ open: boolean; method: MethodId | null }>({ open: false, method: null });
```

Pass `onAddDetector={(m) => setWizard({ open: true, method: m ?? null })}` to `<InvestigateRail>` (`:1700-1740`), and render beside `InvestigateSheetHost` (`:1766`):

```tsx
        {timeline && (
          <DetectorWizard
            caseId={caseId}
            timelineId={timeline.id}
            open={wizard.open}
            onOpenChange={(open) => setWizard((w) => ({ ...w, open }))}
            initialMethod={wizard.method}
            onOpenSignatures={() => setSheet({ kind: "tools", section: "signatures" })}
          />
        )}
```

- [ ] **Step 6: Run tests and typecheck**

Run: `cd frontend && npx vitest run src/test/investigateRail.test.tsx && npm run typecheck`
Expected: rail tests PASS; typecheck may still list `ToolsSheet.tsx`/`MethodRow.tsx`/`InvestigateSheetHost.tsx` (Task 10).

- [ ] **Step 7: Commit**

```bash
git add -A frontend/src
git commit -m "feat(frontend): the rail runs only configured detectors; DetectorStrip, wizard entry, presets and mute strip removed"
```

---

### Task 10: Sheet host reads the entry; Tools Methods tab becomes the configured list (W4, rest)

**Files:**
- Modify: `frontend/src/components/analysis/InvestigateSheetHost.tsx:93-96`
- Modify: `frontend/src/components/analysis/ToolsSheet.tsx:95-252`, `frontend/src/components/analysis/MethodRow.tsx`
- Modify: `frontend/src/test/investigateSheetHost.test.tsx`, `frontend/src/test/toolsSheet.test.tsx`, `frontend/src/test/designSystemBudget.ts:58`

**Interfaces:**
- Consumes: `useMethodFindings(..., { scope })` (Task 6), `useTimelineDetectors`, `scopeOf`.
- Produces: `ToolsSheet` props gain `onAddDetector: (method?: MethodId) => void`; `MethodRow` loses the `mute` prop.

- [ ] **Step 1: Failing host test**

In `frontend/src/test/investigateSheetHost.test.tsx`, add (mocking `@/hooks/useTimelineDetectors` to return a `value_novelty` entry with `params: { fields: ["user"] }, frame: "baseline", baseline_id: "b1"`, and spying on `useMethodFindings`):

```tsx
  it("opens a finding under the configured entry's params and scope, so it matches the rail", () => {
    render(<InvestigateSheetHost {...props} sheet={{ kind: "finding", method: "value_novelty", rank: 0 }} />, { wrapper });
    expect(findingsSpy).toHaveBeenCalledWith("c1", "t1", "value_novelty", expect.objectContaining({
      enabled: true,
      params: { fields: ["user"] },
      scope: { frame: "baseline", baseline_id: "b1" },
    }));
  });
```

- [ ] **Step 2: Failing Tools test**

In `frontend/src/test/toolsSheet.test.tsx`, replace mute-related tests with:

```tsx
  it("lists only configured detectors on the Methods tab and offers the wizard", () => {
    // sweep.current byMethod: value_novelty configured, entropy not
    render(<ToolsSheet {...props} section="methods" />, { wrapper });
    expect(screen.getByTestId("method-row-value_novelty")).toBeInTheDocument();
    expect(screen.queryByTestId("method-row-entropy")).toBeNull();
    expect(screen.getByTestId("methods-summary")).toHaveTextContent("1 configured · 1 ran");
    fireEvent.click(screen.getByTestId("tools-add-detector"));
    expect(props.onAddDetector).toHaveBeenCalled();
  });
```

- [ ] **Step 3: Run both to verify they fail**

Run: `cd frontend && npx vitest run src/test/investigateSheetHost.test.tsx src/test/toolsSheet.test.tsx`
Expected: FAIL.

- [ ] **Step 4: Sheet host**

In `InvestigateSheetHost.tsx`, add `const { byMethod: entries } = useTimelineDetectors(caseId, timelineId);` and change the findings call:

```tsx
  // Finding mode addresses a row of the rail's list, which the rail fetched
  // under the *configured* entry's params and scope. Method mode with the
  // analyst's own knobs runs under the panel scope, as before.
  const entry = sheet.kind === "finding" ? entries.get(sheet.method) : undefined;
  const findings = useMethodFindings(caseId, timelineId, methodOf(sheet), {
    enabled: sheet.kind === "finding" || (sheet.kind === "method" && runParams !== null),
    params: runParams ?? entry?.params ?? {},
    scope: runParams === null && entry ? scopeOf(entry) : undefined,
  });
```

- [ ] **Step 5: Tools sheet and MethodRow**

`MethodRow.tsx`: delete the `mute` prop, the `MutedMethods` import, the `muted` derivation, the muted border class and any mute toggle button (`Bell`/`BellOff`). Keep run/retry and open.

`ToolsSheet.tsx`: delete the `useMutedMethods` import and `mute`; compute the summary over configured states only:

```tsx
  const configured = METHODS.map((m) => byMethod[m.id]).filter((s) => s?.configured);
  const running = configured.filter((s) => s.pending).length;
  const ran = configured.filter((s) => !s.error && !s.pending).length;
  const failed = configured.filter((s) => s.error).length;
```

Summary line: `{configured.length} configured · {ran} ran{running > 0 && ` · ${running} still running`}{failed > 0 && ` · ${failed} failed`}`. Replace the `METHODS.map(... <MethodRow mute={mute} …/>)` list with `configured.map((state) => <MethodRow key={state.meta.id} state={state} onRun={onRunMethod} onOpen={onOpenMethod} onSetupBaseline={openBaselineBuilder} />)` and add above it:

```tsx
          <Button variant="outline" size="sm" data-testid="tools-add-detector" className="mb-2" onClick={() => onAddDetector()}>
            <Plus size={11} /> Add detector
          </Button>
          <p className="mb-2 text-[11px] text-[var(--color-fg-muted)]">
            Only configured detectors run. A method the analysis gate marks not applicable can still be configured — the gate is advice.
          </p>
```

Add `onAddDetector` to `ToolsSheet`'s props, thread it through `InvestigateSheet` (tools mode) and `InvestigateSheetHost` from `ExplorerPage` (`onAddDetector={(m) => setWizard({ open: true, method: m ?? null })}`). Lower `ToolsSheet.tsx`'s `rawButton` budget in `designSystemBudget.ts:58` if the removed mute control drops it.

- [ ] **Step 6: Run the whole frontend suite**

Run: `cd frontend && npm run typecheck && npm run lint && npm run test`
Expected: all green. Fix stragglers (any remaining import of the deleted hooks: `grep -rn "useMutedMethods\|useMethodFocus\|DetectorMuteStrip\|MethodFocusStrip\|muted_methods\|PRESETS" src/`).

- [ ] **Step 7: Commit**

```bash
git add -A frontend/src
git commit -m "feat(frontend): finding sheet reads the configured entry; Tools Methods tab is the configured list"
```

---

### Task 11: Full backend suite, frontend build, end-to-end check (W7 close-out)

**Files:** none new.

- [ ] **Step 1: Backend suite**

Run: `uv run pytest -q`
Expected: green apart from the three environmental embeddings failures noted in memory (`vestigo-local-venv-no-embeddings`). Any other failure is this branch's.

- [ ] **Step 2: Lint and format**

Run: `uv run ruff check . && uv run ruff format --check . && cd frontend && npm run lint && npm run typecheck`
Expected: clean.

- [ ] **Step 3: Real-app check**

Use the `verify` skill (`/verify`) to build the frontend, launch against isolated databases, log in, open the demo case's "Full incident" timeline and confirm through the API: `GET /api/cases/{id}/timelines/{tid}` lists five `detectors`; the Investigate panel shows five chips and findings; a fresh timeline shows the empty state; adding `entropy` through the wizard produces a `timeline.set_detector` audit row (`GET /api/cases/{id}/audit` or the admin audit page). Record what was checked in the commit message of Task 12.

- [ ] **Step 4: Commit any fixes**

```bash
git commit -am "fix: <what the end-to-end check found>"
```
(only if something was found)

---

### Task 12: Documentation (W8)

**Files:**
- Modify: `docs/ANOMALY_DETECTION.md:214-418` (gate intro, replace "Muting a method" and "Declared fields vs. a personal focus"), plus every remaining "sweep"/"unprompted" mention (`grep -n "sweep\|unprompted\|focus\|mute" docs/ANOMALY_DETECTION.md`)
- Modify: `docs/ROADMAP.md` (Milestone 12: delete W1–W8 as they are shipped; keep the diagnosis paragraph and the "Not planned here" block as a standing decision, moved under "Standing decisions"; fix the W4 wording to name `DetectorStrip`), the Milestone 3 guidance item (delete — absorbed), the priority list entry 8 (delete)
- Modify: `docs/PROGRESS.md` (new top entry), `CHANGELOG.md` (Unreleased section), `CLAUDE.md` (frontend layout paragraph: replace the `DetectorMuteStrip` sentence with the strip/wizard sentence; `db/anomaly_stats.py` paragraph's "every auto path" sentence still holds)

- [ ] **Step 1: `ANOMALY_DETECTION.md`**

Replace the first paragraph under "### The analysis gate: which methods are offered up front" so it opens with: "No statistical method runs unprompted. The Investigate rail runs exactly the detectors an analyst configured on the timeline (next section); the gate's job is to advise the wizard — per method and without scanning an event, whether that method *can* produce a finding on this data — and to explain, in the Tools accounting, what a configured method's verdict was." Keep the table and the two load-bearing properties, rewording "A `not_applicable` method runs through … exactly as any other, returning what an unconditional sweep would have returned" to "… exactly as any other, and can be configured through the wizard exactly as any other".

Replace "### Muting a method" (through the end of "#### Declared fields vs. a personal focus") with:

```markdown
### Which detectors run: configured, never unprompted

Opening a timeline runs nothing. `Timeline.detectors` is the list of methods an
analyst configured through the detector wizard — `{method, params, frame,
baseline_id, added_by, added_at}` per entry, one per method — and the rail runs
exactly that list: one `GET …/analysis/findings` per entry, with the entry's own
params and scope, through the same fingerprint cache as any ad hoc run. First
open after configuring pays the scan; every later open, by anyone on the case,
hits the cache.

Written through `PUT …/timelines/{id}/detectors/{method}` (add or replace in
place) and `DELETE …/detectors/{method}`, both contribute access, both audited
(`timeline.set_detector`, `timeline.remove_detector`). `params` is validated
with the same per-method models the findings endpoint uses, so nothing storable
describes a run the runner would refuse; a baseline frame must name a definition
on this timeline. **Shared, not per-browser**: which detectors an investigation
ran is part of its record.

Why this replaced the unprompted sweep: up to twelve heavy scans nobody asked
for, over recommender-chosen fields, presented as one feed, read as noise. Three
mechanisms existed only to subtract from that sweep — a shared mute list, a
per-user field focus, preset filter pills — and none of them *configured* a
detector. All three are gone (1.19). The one that stays is
`Timeline.field_overrides` (next section): the wizard's "let Vestigo pick"
choice consults it exactly as the sweep did.

The gate is still advice. A `not_applicable` card in the wizard is selectable,
with the arithmetic beside it; a configured method runs whatever the plan says.
The sheet's method mode still runs a method with ad hoc knobs without storing
anything — that is a different act, and it runs under the panel scope rather
than an entry's.

The demo case ships five configured detectors on its "Full incident" timeline
(`demo/metadata.py::DEMO_DETECTORS`), each asserted to find something in
`tests/test_demo_detector_coverage_clickhouse.py`.
```

In "### Declaring which fields a method reads", change "this records the decision at the one place both the unprompted sweep and the picker's auto default derive from" to "…the one place the wizard's auto default and the picker's auto preview derive from", and drop "like the mute list," from "and, like the mute list, **shared rather than per-browser**". Remove the mute/focus comparison table. Sweep through the remaining `sweep` mentions (the analysis-cache and scope-provenance sections) and reword each to "a configured run" or "the rail's requests".

- [ ] **Step 2: `CHANGELOG.md`**

Under a new `## [Unreleased]` heading:

```markdown
### Changed

- **Detectors are opt-in.** Opening a timeline no longer runs every applicable
  statistical detector. The Investigate rail runs only the detectors configured on the
  timeline through the new detector wizard (choose → configure → confirm), stored as a
  shared, audited list (`Timeline.detectors`, `PUT`/`DELETE
  …/timelines/{id}/detectors/{method}`). The demo case ships five pre-configured.
- The agent gains `list_configured_detectors`.

### Removed

- **Breaking for API clients:** `PATCH …/timelines/{id}/muted-methods` and the
  `muted_methods` timeline field (migration 0034 drops the column); the
  `analysis_method_focus` user preference; the Investigate rail's preset pills. All
  three only subtracted from the unprompted sweep, which no longer exists.
```

- [ ] **Step 3: `PROGRESS.md`, `ROADMAP.md`, `CLAUDE.md`**

`PROGRESS.md`: a dated entry on top, six to ten lines: what changed, why (the diagnosis), the deviation (`DetectorStrip` instead of group headers), what was verified end to end in Task 11.

`ROADMAP.md`: delete the W1–W8 checkboxes; move the Milestone 12 "Not planned here" paragraph under "## Standing decisions" as "**Detectors are opt-in (2026-09)**: no unprompted sweep, one config per method, run on open through the findings cache, no job. Revisit triggers: a request for several instances of one method; single scans exceeding the request timeout in the field." Delete the Milestone 3 guidance-restructure item and the priority-list entry 8. Fix the "State (verified …)" line if it counts items.

`CLAUDE.md`: in the frontend layout paragraph, replace the `DetectorMuteStrip` sentence with: "`DetectorStrip` sits above the feed and names every configured detector with its scope and count, with edit and remove for contribute access; `DetectorWizard` is how a detector gets configured — nothing runs unprompted, and the list it writes (`Timeline.detectors`, shared, audited, one entry per method) is the only thing `useStreamingSweep` runs;" and delete the `MethodFocus`/preset mentions if any.

- [ ] **Step 4: Verify docs mention nothing that no longer exists**

Run: `grep -rn "muted_methods\|muted-methods\|analysis_method_focus\|MethodFocusStrip\|DetectorMuteStrip\|PRESETS" docs CLAUDE.md README.md src frontend/src --include=*.md --include=*.py --include=*.ts --include=*.tsx | grep -v "migrations/versions\|CHANGELOG\|PROGRESS"`
Expected: no output.

- [ ] **Step 5: Commit and open the PR**

```bash
git add docs CHANGELOG.md CLAUDE.md
git commit -m "docs: opt-in detectors — ANOMALY_DETECTION 'Which detectors run', changelog, roadmap M12 closed"
git push -u origin feature/detector-wizard
gh pr create --title "Opt-in detectors: the detector wizard replaces the unprompted sweep (Milestone 12)" --body "…"
```

The PR body: the diagnosis paragraph from the roadmap, the list of removed endpoints/fields, the end-to-end check from Task 11, and the generated-with footer. Merge via `gh pr merge` only after CI is green — never push to `main` directly.

---

## Self-review

**Spec coverage.** W1 → Tasks 1–2. W2 → Task 2. W3 → Tasks 7–8 (the three steps, plan badges, cost, Sigma card, edit mode, inline knob help). W4 → Tasks 6, 9, 10 (sweep over the list, strip with edit/remove, empty state, Tools Methods tab, presets/mute/focus/skipped-button removed; the sheet's ad hoc method mode kept). W5 → Task 3. W6 → Task 4. W7 → migration test (1), endpoint-rejects-what-findings-rejects (2, parametrized against `_adapt_params`), registry `useWhen` test (7), wizard step/summary vitest (8), no-query-when-empty (6), demo seed assertions (4). W8 → Task 12. The scope-change dialog change (Task 6 Step 4) is a consequence the spec did not name: entries carry their own scope, so the "N methods will re-run" sentence became false.

**Placeholders.** Task 2 Step 1 tells the implementer to copy the read-only-member and baseline-create setup from named existing tests rather than trusting the sketch; Task 3 Step 1 likewise names the harness to copy. Both are deliberate: those helpers exist and their exact names were not verified here. Everything else is concrete.

**Type consistency.** `DetectorEntry` fields (`method, params, frame, baseline_id, added_by, added_at`) are identical in the migration test, the store, `to_dict`, `types.ts`, the hook and the demo seed. `DetectorBody = Pick<DetectorEntry, "params"|"frame"|"baseline_id">` is what `putDetector`, `useTimelineDetectors.set` and the wizard's `apply` all pass. `scopeOf` is defined in Task 5 and consumed in Tasks 6, 9, 10. `MethodState.configured/entry` defined in Task 6, consumed in 9 and 10. `onAddDetector(method?: MethodId)` has the same signature on `InvestigateRail`, `ToolsSheet` and `ExplorerPage`. `useWhen` defined in Task 7, consumed in Task 8.
