"""Instance settings: registry coverage, the admin API, and env precedence.

The premise the whole feature rests on is that *every* ``Settings`` field is
reachable from the console — so the first test here is a coverage assertion
that fails the moment someone adds a tunable without a spec.
"""

from __future__ import annotations

import pytest

from tests.conftest import as_admin, login
from vestigo.core.config import Settings, get_settings
from vestigo.core.settings_registry import (
    GROUPS,
    all_specs,
    editable_fields,
    field_kind,
    is_nullable,
    secret_fields,
)


def test_registry_covers_every_settings_field():
    registered = {s.field for s in all_specs()}
    declared = set(Settings.model_fields)
    assert declared - registered == set(), "Settings field(s) missing from the registry"
    assert registered - declared == set(), "registry names a field Settings does not declare"


def test_every_spec_lands_in_a_declared_group():
    groups = {g.key for g in GROUPS}
    assert {s.group for s in all_specs()} <= groups


def test_bootstrap_fields_are_env_only():
    """A setting read before (or in order to reach) the database cannot be
    stored in it."""
    editable = editable_fields()
    for field in ("postgres_url", "environment", "log_level", "admin_password", "secrets_mode"):
        assert field not in editable


def test_secret_fields_render_as_secrets():
    for field in secret_fields():
        assert field_kind(field) == "secret"


def test_nullability_is_read_off_the_annotation():
    """The console needs this to tell "unset" from a literal empty string."""
    assert is_nullable("oidc_issuer") is True
    assert is_nullable("embedding_api_base_url") is True
    # A plain str: empty is a value (it disables the global ruleset), not "unset".
    assert is_nullable("sigma_rules_path") is False
    assert is_nullable("stat_rarity_floor") is False


def test_clearing_a_nullable_field_stores_null_not_empty_string(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    client.put("/api/admin/settings", json={"values": {"oidc_issuer": "https://idp.local"}})

    resp = client.put("/api/admin/settings", json={"values": {"oidc_issuer": None}})
    assert resp.status_code == 200, resp.text
    by_field = {f["field"]: f for f in resp.json()["settings"]}
    assert by_field["oidc_issuer"]["source"] == "default"
    assert by_field["oidc_issuer"]["value"] is None
    assert by_field["oidc_issuer"]["nullable"] is True


def test_empty_string_stays_a_value_for_a_non_nullable_field(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    resp = client.put("/api/admin/settings", json={"values": {"sigma_rules_path": ""}})
    assert resp.status_code == 200, resp.text
    by_field = {f["field"]: f for f in resp.json()["settings"]}
    assert by_field["sigma_rules_path"]["nullable"] is False
    # Stored as an explicit override, not treated as a clear.
    assert by_field["sigma_rules_path"]["source"] == "db"
    assert by_field["sigma_rules_path"]["value"] == ""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_get_settings_lists_fields_with_source_and_masks_secrets(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    body = client.get("/api/admin/settings").json()

    by_field = {f["field"]: f for f in body["settings"]}
    assert {g["key"] for g in body["groups"]}

    rarity = by_field["stat_rarity_floor"]
    assert rarity["source"] == "default"
    assert rarity["value"] == 3
    assert rarity["editable"] is True
    assert rarity["env_var"] == "VESTIGO_STAT_RARITY_FLOOR"

    # Env-pinned by the transfer_temp fixture — read-only, environment wins.
    assert by_field["transfer_temp_path"]["source"] == "env"
    assert by_field["transfer_temp_path"]["editable"] is False

    # A secret's value never leaves the process; only whether one is set.
    key = by_field["embedding_api_key"]
    assert key["value"] is None
    assert key["value_set"] is False


def test_put_persists_applies_and_audits(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    resp = client.put("/api/admin/settings", json={"values": {"stat_rarity_floor": 9}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] == ["stat_rarity_floor"]

    # Applied to the running process, not just stored.
    assert get_settings().stat_rarity_floor == 9

    by_field = {f["field"]: f for f in resp.json()["settings"]}
    assert by_field["stat_rarity_floor"]["source"] == "db"

    actions = [a["action"] for a in client.get("/api/admin/audit").json()["audit"]]
    assert "admin.settings_update" in actions


def test_put_null_clears_the_override(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    client.put("/api/admin/settings", json={"values": {"stat_rarity_floor": 9}})
    resp = client.put("/api/admin/settings", json={"values": {"stat_rarity_floor": None}})
    assert resp.status_code == 200, resp.text
    assert get_settings().stat_rarity_floor == 3
    by_field = {f["field"]: f for f in resp.json()["settings"]}
    assert by_field["stat_rarity_floor"]["source"] == "default"


def test_put_rejects_out_of_range_without_persisting(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    before = get_settings().stat_scan_concurrency
    resp = client.put("/api/admin/settings", json={"values": {"stat_scan_concurrency": 0}})
    assert resp.status_code == 422
    assert get_settings().stat_scan_concurrency == before


def test_put_rejects_env_pinned_field(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    resp = client.put(
        "/api/admin/settings", json={"values": {"transfer_temp_path": "/tmp/elsewhere"}}
    )
    assert resp.status_code == 422
    assert "environment" in resp.json()["detail"]


def test_put_rejects_env_only_and_agent_managed_fields(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    for field, value in (("postgres_url", "postgresql://x/y"), ("agent_model", "gpt-4o")):
        resp = client.put("/api/admin/settings", json={"values": {field: value}})
        assert resp.status_code == 422, field


def test_settings_require_admin(client, admin_bootstrap):
    """A plain analyst cannot read or write instance configuration."""
    as_admin(client, admin_bootstrap)
    client.post(
        "/api/admin/users",
        json={"username": "analyst", "password": "analyst-pass-123", "is_admin": False},
    )
    client.post("/api/auth/logout")
    login(client, "analyst", "analyst-pass-123")

    assert client.get("/api/admin/settings").status_code == 403
    assert (
        client.put("/api/admin/settings", json={"values": {"stat_rarity_floor": 4}}).status_code
        == 403
    )


@pytest.mark.parametrize("mode", ["env-only", "db"])
def test_secrets_mode_gates_secret_storage(client, admin_bootstrap, monkeypatch, mode):
    monkeypatch.setenv("VESTIGO_SECRETS_MODE", mode)
    get_settings.cache_clear()
    as_admin(client, admin_bootstrap)
    resp = client.put("/api/admin/settings", json={"values": {"embedding_api_key": "sk-test-123"}})
    if mode == "env-only":
        assert resp.status_code == 422
        assert "env-only" in resp.json()["detail"]
    else:
        assert resp.status_code == 200, resp.text
        by_field = {f["field"]: f for f in resp.json()["settings"]}
        assert by_field["embedding_api_key"]["value_set"] is True
        assert by_field["embedding_api_key"]["value"] is None


async def test_stored_override_loses_to_the_environment(store, monkeypatch):
    """An override stored before the operator pinned the field must not
    resurface once the pin exists."""
    from vestigo.core.runtime_settings import load_runtime_settings

    await store.init_schema()
    await store.set_app_settings({"stat_rarity_floor": 7}, "u1")

    await load_runtime_settings()
    assert get_settings().stat_rarity_floor == 7

    monkeypatch.setenv("VESTIGO_STAT_RARITY_FLOOR", "11")
    get_settings.cache_clear()
    await load_runtime_settings()
    assert get_settings().stat_rarity_floor == 11


async def test_clearing_is_allowed_for_a_field_the_environment_later_pinned(store, monkeypatch):
    """A pin must not strand the row it made irrelevant.

    Writing to a pinned field is refused, but ``null`` only deletes a row the
    merge already ignores — and the console shows no reset control for a
    read-only field, so refusing it would leave the row uncleanable.
    """
    from vestigo.core.runtime_settings import (
        SettingsValidationError,
        load_runtime_settings,
        save_runtime_settings,
    )

    await store.init_schema()
    await store.set_app_settings({"stat_rarity_floor": 7}, "u1")

    monkeypatch.setenv("VESTIGO_STAT_RARITY_FLOOR", "11")
    get_settings.cache_clear()
    await load_runtime_settings()
    assert get_settings().stat_rarity_floor == 11

    # Writing to the pinned field is still refused...
    with pytest.raises(SettingsValidationError):
        await save_runtime_settings({"stat_rarity_floor": 4}, "u1")

    # ...but clearing the now-dead row works.
    await save_runtime_settings({"stat_rarity_floor": None}, "u1")
    assert await store.list_app_settings() == []


async def test_load_accepts_an_explicit_store(store, monkeypatch):
    """Callers that own a store pass it in — no reach for the API singleton.

    The CLI builds its own ``PostgresStore``; falling through to
    ``api.deps.get_store`` there would open a second engine for one read.
    """
    from vestigo.api import deps
    from vestigo.core.runtime_settings import load_runtime_settings

    await store.init_schema()
    await store.set_app_settings({"stat_rarity_floor": 8}, "u1")

    def _boom() -> None:
        raise AssertionError("the API singleton must not be consulted")

    monkeypatch.setattr(deps, "get_store", _boom)
    assert await load_runtime_settings(store) == {"stat_rarity_floor": 8}
    assert get_settings().stat_rarity_floor == 8


async def test_invalid_stored_value_is_ignored_not_fatal(store, caplog):
    """A row written by an older version (or by hand) degrades to a warning."""
    from vestigo.core.runtime_settings import load_runtime_settings

    await store.init_schema()
    await store.set_app_settings({"stat_rarity_floor": 5, "stat_scan_concurrency": 0}, "u1")

    applied = await load_runtime_settings()
    assert applied == {"stat_rarity_floor": 5}
    assert get_settings().stat_scan_concurrency == 2
