"""Demo-case seeding: setting, capability, gate, and job dispatch."""

from __future__ import annotations

import pytest

from vestigo.core import settings_registry
from vestigo.core.config import Settings


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
