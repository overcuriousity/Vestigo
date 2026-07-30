"""Demo-case seeding: setting, capability, gate, and job dispatch."""

from __future__ import annotations

import pytest

from vestigo.api import deps
from vestigo.core import demo_case as demo_mod
from vestigo.core import settings_registry
from vestigo.core.config import Settings, get_settings
from vestigo.core.jobs import get_job_store


def test_demo_case_enabled_defaults_on():
    assert Settings().demo_case_enabled is True


def test_demo_case_setting_is_registered_and_editable():
    spec = next(s for s in settings_registry.all_specs() if s.field == "demo_case_enabled")
    assert spec.group == "onboarding"
    assert spec.env_only is False
    assert "onboarding" in {g.key for g in settings_registry.GROUPS}


@pytest.mark.asyncio
async def test_claim_demo_seed_is_once_only(store):
    await store.init_schema()
    user = await store.create_user(user_id="u_claimer", username="claimer")
    assert await store.claim_demo_seed(user.id) is True
    assert await store.claim_demo_seed(user.id) is False
    refreshed = await store.get_user(user.id)
    assert refreshed.demo_case_seeded_at is not None


@pytest.mark.asyncio
async def test_maybe_seed_dispatches_once(store, builds, monkeypatch):
    await store.init_schema()
    monkeypatch.setattr(deps, "_store", store)
    user = await store.create_user(user_id="u_seeded", username="seeded")

    job_id = await demo_mod.maybe_seed_demo_case(user)
    assert job_id is not None
    await demo_mod._await_pending_seeds()
    assert len(builds) == 1

    refreshed = await store.get_user(user.id)
    assert await demo_mod.maybe_seed_demo_case(refreshed) is None
    assert len(builds) == 1


@pytest.mark.asyncio
async def test_maybe_seed_declines_when_disabled(store, builds, monkeypatch):
    await store.init_schema()
    monkeypatch.setattr(deps, "_store", store)
    monkeypatch.setenv("VESTIGO_DEMO_CASE_ENABLED", "false")
    get_settings.cache_clear()
    user = await store.create_user(user_id="u_nodemo", username="nodemo")

    assert await demo_mod.maybe_seed_demo_case(user) is None
    assert builds == []
    refreshed = await store.get_user(user.id)
    # Not consumed, so turning the setting back on still seeds this user.
    assert refreshed.demo_case_seeded_at is None
    monkeypatch.delenv("VESTIGO_DEMO_CASE_ENABLED")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_failed_build_marks_the_job_failed(store, monkeypatch):
    await store.init_schema()
    monkeypatch.setattr(deps, "_store", store)

    async def _boom(*args, **kwargs):
        raise RuntimeError("clickhouse is unreachable")

    monkeypatch.setattr(demo_mod, "build_demo_case", _boom)
    user = await store.create_user(user_id="u_broken", username="broken")

    job_id = await demo_mod.maybe_seed_demo_case(user)
    await demo_mod._await_pending_seeds()
    job = get_job_store().get(job_id)
    assert job.status == "failed"
    assert "unreachable" in job.error
