"""Migration 0033 folds ``agent_settings`` into ``app_settings`` and drops it.

Unlike the additive migrations around it, this one moves *live configuration*
between two storage shapes: an instance that had an admin-edited agent must
come out of the upgrade resolving exactly the same values, or the agent
silently stops working on a deploy. The API key moves with the rest — both
sides hold it as plaintext under the same contract — so the round trip is
asserted on it too, as is the one case where it must *not* move: an instance
whose retired ``VESTIGO_AGENT_SECRET_MODE=env-only`` meant the stored key was
never used, where copying it would put a switched-off credential back in play.
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


@pytest_asyncio.fixture()
async def engine(blank_pg_database):
    store = pg.PostgresStore(url=blank_pg_database)
    yield store.engine
    await store.engine.dispose()


async def _stored(conn) -> dict[str, Any]:
    rows = (await conn.execute(text("SELECT key, value FROM app_settings"))).all()
    return {r.key: r.value for r in rows}


@pytest.mark.asyncio
async def test_the_row_moves_into_app_settings_under_prefixed_keys(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0032"))
        await conn.execute(
            text(
                "INSERT INTO agent_settings "
                "(id, model, api_base_url, api_key, max_turns, tool_fidelity, "
                " extra_headers, disabled_tools, updated_by) "
                "VALUES ('global', 'qwen3:32b', 'http://llm.example/v1', 'sk-stored', 20, "
                " 'auto', '{\"X-Custom\": \"v\"}', '[\"semantic_search\"]', 'root')"
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0033"))

        stored = await _stored(conn)
        tables = {
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).all()
        }

    assert "agent_settings" not in tables
    assert stored["agent_model"] == "qwen3:32b"
    assert stored["agent_api_base_url"] == "http://llm.example/v1"
    assert stored["agent_max_turns"] == 20
    assert stored["agent_tool_fidelity"] == "auto"
    assert stored["agent_extra_headers"] == {"X-Custom": "v"}
    assert stored["agent_disabled_tools"] == ["semantic_search"]
    # The key moves rather than being dropped: both tables held it as plaintext
    # under the same contract, and dropping it breaks a working instance.
    assert stored["agent_api_key"] == "sk-stored"
    # A NULL column carried no opinion, so it must not become a stored override
    # — that is the difference between "unset" and "explicitly this value".
    assert "agent_user_agent" not in stored
    assert "agent_provider" not in stored


@pytest.mark.asyncio
async def test_an_untouched_instance_gains_no_overrides(engine):
    """The overwhelmingly common case: the row was never written."""
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0032"))
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0033"))
        assert await _stored(conn) == {}


@pytest.mark.asyncio
async def test_a_newer_app_settings_value_wins_over_the_retired_row(engine):
    """A key in `app_settings` was written by the newer surface, which means an
    admin decided it more recently than the row being retired — so the copy
    must not clobber it."""
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0032"))
        await conn.execute(
            text("INSERT INTO agent_settings (id, model) VALUES ('global', 'from-the-row')")
        )
        await conn.execute(
            text("INSERT INTO app_settings (key, value) VALUES ('agent_model', '\"newer\"')")
        )
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0033"))
        assert (await _stored(conn))["agent_model"] == "newer"


@pytest.mark.asyncio
async def test_a_key_the_env_only_mode_ignored_is_not_revived(engine, monkeypatch):
    """Under the retired `env-only` switch the resolver ignored a stored key, so
    the row may hold a rotated or abandoned one. The new `secrets_mode` defaults
    to `db`, so copying it would start sending a credential the operator had
    switched off — the key alone stays behind, everything else still moves."""
    monkeypatch.setenv("VESTIGO_AGENT_SECRET_MODE", "env-only")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0032"))
        await conn.execute(
            text(
                "INSERT INTO agent_settings (id, model, api_key) "
                "VALUES ('global', 'qwen3:32b', 'sk-rotated-away')"
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0033"))
        stored = await _stored(conn)

    assert "agent_api_key" not in stored
    assert stored["agent_model"] == "qwen3:32b"


@pytest.mark.asyncio
async def test_env_only_stored_as_an_override_also_holds_the_key_back(engine, monkeypatch):
    """The switch was an ordinary editable setting, so an admin could have set it
    from the console rather than the environment. Both sources have to be read,
    or the protection depends on how it was turned on."""
    monkeypatch.delenv("VESTIGO_AGENT_SECRET_MODE", raising=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0032"))
        await conn.execute(
            text(
                "INSERT INTO agent_settings (id, model, api_key) "
                "VALUES ('global', 'qwen3:32b', 'sk-rotated-away')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO app_settings (key, value) VALUES ('agent_secret_mode', '\"env-only\"')"
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0033"))
        stored = await _stored(conn)

    assert "agent_api_key" not in stored
    assert stored["agent_model"] == "qwen3:32b"


@pytest.mark.asyncio
async def test_the_retired_secret_switch_is_deleted(engine):
    """`agent_secret_mode` is no longer a Settings field, so a stored override
    of it would sit there forever as a row nothing can read or clear."""
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0032"))
        await conn.execute(
            text(
                "INSERT INTO app_settings (key, value) VALUES ('agent_secret_mode', '\"env-only\"')"
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0033"))
        assert "agent_secret_mode" not in await _stored(conn)


@pytest.mark.asyncio
async def test_downgrade_restores_the_row_and_consumes_the_overrides(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0033"))
        await conn.execute(
            text(
                "INSERT INTO app_settings (key, value) VALUES "
                "('agent_model', '\"qwen3:32b\"'), "
                "('agent_api_key', '\"sk-stored\"'), "
                "('agent_disabled_tools', '[\"semantic_search\"]'), "
                "('stat_rarity_floor', '7')"
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "downgrade", "0032"))

        row = (
            await conn.execute(
                text("SELECT model, api_key, disabled_tools FROM agent_settings WHERE id='global'")
            )
        ).one()
        stored = await _stored(conn)

    assert row.model == "qwen3:32b"
    assert row.api_key == "sk-stored"
    assert row.disabled_tools == ["semantic_search"]
    # The moved keys are consumed so the two stores cannot disagree; anything
    # that was never the agent's is left alone.
    assert "agent_model" not in stored
    assert stored["stat_rarity_floor"] == 7
