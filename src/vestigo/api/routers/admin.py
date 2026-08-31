"""Admin console API: user, team, and membership management; global audit access.

Every endpoint in this router requires ``require_admin`` — only the
administrator role can create/delete users, rotate passwords, or manage
teams and memberships.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from vestigo.agent.availability import agent_available, list_models, reset_probe_cache
from vestigo.agent.config import resolve_agent_config
from vestigo.agent.fidelity import fidelity_config_warning
from vestigo.agent.tools import TOOL_NAMES, TOOL_REGISTRY
from vestigo.api.deps import get_store, require_admin
from vestigo.api.uploads import receive_upload_to_tmp
from vestigo.core.config import env_pinned, get_settings, runtime_overrides
from vestigo.core.runtime_settings import SettingsValidationError, save_runtime_settings
from vestigo.core.security import hash_password
from vestigo.core.settings_registry import (
    GROUPS,
    all_specs,
    default_value,
    env_var_name,
    field_constraints,
    field_kind,
    is_nullable,
    secret_fields,
)
from vestigo.db.postgres import User, generate_id

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class UserCreate(BaseModel):
    """Payload to create a new local user account."""

    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8, max_length=255)
    is_admin: bool = False
    display_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)


class UserUpdate(BaseModel):
    """Payload to patch mutable fields on a user."""

    username: str | None = Field(default=None, min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    is_admin: bool | None = None
    is_active: bool | None = None


class PasswordRotate(BaseModel):
    """Payload for an admin-initiated password rotation."""

    new_password: str = Field(..., min_length=8, max_length=255)
    force_change: bool = True


class TeamCreate(BaseModel):
    """Payload to create a new investigation team."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)


class MembershipCreate(BaseModel):
    """Payload to add a user to a team."""

    user_id: str = Field(..., min_length=1)
    role: str = Field(default="member", pattern="^(member|manager)$")


class MembershipRoleUpdate(BaseModel):
    """Payload to change a member's role within a team."""

    role: str = Field(..., pattern="^(member|manager)$")


# ═════════════════════════════════════════════════════════════════════════════
# Users
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/users")
async def list_users(unassigned: bool = Query(default=False)) -> dict[str, Any]:
    """List all users, optionally filtered to the team-less default pool."""
    store = get_store()
    users = await store.list_unassigned_users() if unassigned else await store.list_users()
    return {"users": [u.to_dict() for u in users]}


@router.post("/users")
async def create_user(payload: UserCreate, admin: User = Depends(require_admin)) -> dict[str, Any]:
    """Create a new local user account."""
    store = get_store()
    if await store.get_user_by_username(payload.username) is not None:
        raise HTTPException(status_code=409, detail="Username already taken")
    password_hash = await asyncio.to_thread(hash_password, payload.password)
    user = await store.create_user(
        user_id=generate_id("user"),
        username=payload.username,
        password_hash=password_hash,
        is_admin=payload.is_admin,
        display_name=payload.display_name,
        email=payload.email,
    )
    await store.record_audit(
        action="admin.create_user",
        actor=admin,
        target_type="user",
        target_id=user.id,
        detail={"created_username": user.username},
    )
    return {"user": user.to_dict()}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str, payload: UserUpdate, admin: User = Depends(require_admin)
) -> dict[str, Any]:
    """Patch a user's username/display name/admin flag/active flag."""
    store = get_store()
    target = await store.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.username and payload.username != target.username:
        existing = await store.get_user_by_username(payload.username)
        if existing is not None and existing.id != user_id:
            raise HTTPException(status_code=409, detail="Username already taken")
    if payload.is_admin is False and target.is_admin and target.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot remove your own admin privileges")
    updated = await store.update_user(
        user_id,
        username=payload.username,
        display_name=payload.display_name,
        is_admin=payload.is_admin,
        is_active=payload.is_active,
    )
    await store.record_audit(
        action="admin.update_user",
        actor=admin,
        target_type="user",
        target_id=user_id,
        detail=payload.model_dump(exclude_none=True),
    )
    return {"user": updated.to_dict()}


@router.post("/users/{user_id}/password")
async def rotate_password(
    user_id: str, payload: PasswordRotate, admin: User = Depends(require_admin)
) -> dict[str, Any]:
    """Rotate a user's password (admin-initiated). Revokes their existing sessions."""
    store = get_store()
    target = await store.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.auth_provider != "local":
        raise HTTPException(
            status_code=409,
            detail="Cannot set a local password for an OIDC-linked account",
        )
    new_hash = await asyncio.to_thread(hash_password, payload.new_password)
    await store.set_password(user_id, new_hash, must_change_password=payload.force_change)
    await store.revoke_user_sessions(user_id)
    await store.record_audit(
        action="admin.rotate_password",
        actor=admin,
        target_type="user",
        target_id=user_id,
    )
    return {"rotated": True}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    reassign_to: str | None = Query(default=None),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Delete a user account.

    Blocks with 409 if the user owns personal cases and ``reassign_to`` was
    not supplied — deleting a user must never silently orphan their cases.
    """
    store = get_store()
    target = await store.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    owned = await store.owned_case_count(user_id)
    if owned > 0 and not reassign_to:
        raise HTTPException(
            status_code=409,
            detail=f"User owns {owned} case(s). Pass reassign_to=<user_id> (e.g. yourself) to proceed.",
        )
    if reassign_to and await store.get_user(reassign_to) is None:
        raise HTTPException(status_code=404, detail="reassign_to user not found")

    await store.delete_user(user_id, reassign_cases_to=reassign_to)
    await store.record_audit(
        action="admin.delete_user",
        actor=admin,
        target_type="user",
        target_id=user_id,
        detail={"deleted_username": target.username, "reassigned_to": reassign_to},
    )
    return {"deleted": True}


# ═════════════════════════════════════════════════════════════════════════════
# Teams
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/teams")
async def list_teams() -> dict[str, Any]:
    """List all investigation teams."""
    store = get_store()
    return {"teams": [t.to_dict() for t in await store.list_teams()]}


@router.post("/teams")
async def create_team(payload: TeamCreate, admin: User = Depends(require_admin)) -> dict[str, Any]:
    """Create a new investigation team."""
    store = get_store()
    if await store.get_team_by_name(payload.name) is not None:
        raise HTTPException(status_code=409, detail="Team name already taken")
    team = await store.create_team(
        team_id=generate_id(payload.name), name=payload.name, description=payload.description
    )
    await store.record_audit(
        action="admin.create_team",
        actor=admin,
        target_type="team",
        target_id=team.id,
    )
    return {"team": team.to_dict()}


@router.delete("/teams/{team_id}")
async def delete_team(team_id: str, admin: User = Depends(require_admin)) -> dict[str, Any]:
    """Delete a team. Its cases become personal (owner retained), memberships are removed."""
    store = get_store()
    deleted = await store.delete_team(team_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Team not found")
    await store.record_audit(
        action="admin.delete_team",
        actor=admin,
        target_type="team",
        target_id=team_id,
    )
    return {"deleted": True}


@router.get("/teams/{team_id}/members")
async def list_team_members(team_id: str) -> dict[str, Any]:
    """List a team's members with their roles."""
    store = get_store()
    if await store.get_team(team_id) is None:
        raise HTTPException(status_code=404, detail="Team not found")
    members = [
        {**user.to_dict(), "role": role}
        for user, role in await store.list_members_with_users(team_id)
    ]
    return {"members": members}


@router.post("/teams/{team_id}/members")
async def add_team_member(
    team_id: str, payload: MembershipCreate, admin: User = Depends(require_admin)
) -> dict[str, Any]:
    """Add a user to a team with the given role."""
    store = get_store()
    if await store.get_team(team_id) is None:
        raise HTTPException(status_code=404, detail="Team not found")
    if await store.get_user(payload.user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    if await store.get_membership(team_id, payload.user_id) is not None:
        raise HTTPException(status_code=409, detail="User is already a member of this team")
    membership = await store.add_membership(team_id, payload.user_id, role=payload.role)
    await store.record_audit(
        action="admin.add_team_member",
        actor=admin,
        target_type="team",
        target_id=team_id,
        detail={"member_user_id": payload.user_id, "role": payload.role},
    )
    return {"membership": membership.to_dict()}


@router.patch("/teams/{team_id}/members/{member_user_id}")
async def set_team_member_role(
    team_id: str,
    member_user_id: str,
    payload: MembershipRoleUpdate,
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Change a team member's role."""
    store = get_store()
    updated = await store.set_membership_role(team_id, member_user_id, payload.role)
    if not updated:
        raise HTTPException(status_code=404, detail="Membership not found")
    await store.record_audit(
        action="admin.update_team_member",
        actor=admin,
        target_type="team",
        target_id=team_id,
        detail={"member_user_id": member_user_id, "role": payload.role},
    )
    return {"updated": True}


@router.delete("/teams/{team_id}/members/{member_user_id}")
async def remove_team_member(
    team_id: str, member_user_id: str, admin: User = Depends(require_admin)
) -> dict[str, Any]:
    """Remove a user from a team."""
    store = get_store()
    removed = await store.remove_membership(team_id, member_user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Membership not found")
    await store.record_audit(
        action="admin.remove_team_member",
        actor=admin,
        target_type="team",
        target_id=team_id,
        detail={"member_user_id": member_user_id},
    )
    return {"removed": True}


# ═════════════════════════════════════════════════════════════════════════════
# Global audit
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/audit")
async def query_audit(
    user_id: str | None = Query(default=None),
    case_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=10000),
) -> dict[str, Any]:
    """Query the global audit trail, filterable by user/case/action."""
    store = get_store()
    rows = await store.query_audit(user_id=user_id, case_id=case_id, action=action, limit=limit)
    return {"audit": [r.to_dict() for r in rows]}


# ═════════════════════════════════════════════════════════════════════════════
# Enricher assets (GeoIP database upload)
# ═════════════════════════════════════════════════════════════════════════════


class EnricherGlobalConfigUpdate(BaseModel):
    """Payload to set an enricher's instance-wide defaults."""

    auto_run_default: bool


@router.get("/enrichers/config")
async def list_enricher_global_configs(admin: User = Depends(require_admin)) -> dict[str, Any]:
    """Return every registered enricher with its instance-wide config and asset state.

    Asset status is folded into this response (instead of a per-enricher GET)
    because the admin page is the sole consumer and already fetches this list
    — one payload keeps the frontend fully generic with no N+1.
    """
    from vestigo.enrichers.registry import all_enrichers, get_cached_availability

    store = get_store()
    configs = {c.enricher_key: c for c in await store.list_enricher_global_configs()}
    result = []
    for enricher in all_enrichers():
        availability = get_cached_availability(enricher.key)
        config = configs.get(enricher.key)
        asset: dict[str, Any] | None = None
        if enricher.asset_spec is not None:
            # asset_status() stats the filesystem — keep it off the event loop.
            status = await asyncio.to_thread(enricher.asset_status)
            asset = {
                "name": enricher.asset_spec.name,
                "description": enricher.asset_spec.description,
                "accepted_extensions": list(enricher.asset_spec.file_extensions),
                "uploaded": bool(status and status["uploaded"]),
                "size_bytes": status["size_bytes"] if status else None,
                "detail": status["detail"] if status else {},
            }
        result.append(
            {
                "key": enricher.key,
                "display_name": enricher.display_name,
                "description": enricher.description,
                "available": availability.available if availability else False,
                "reason": availability.reason if availability else None,
                "auto_run_default": config.auto_run_default if config else False,
                "asset": asset,
            }
        )
    return {"enrichers": result}


@router.put("/enrichers/{enricher_key}/config")
async def set_enricher_global_config(
    enricher_key: str,
    body: EnricherGlobalConfigUpdate,
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Set instance-wide defaults for one enricher (currently: auto-run on ingest)."""
    from vestigo.enrichers.registry import get_enricher

    if get_enricher(enricher_key) is None:
        raise HTTPException(status_code=404, detail="Unknown enricher")

    store = get_store()
    config = await store.upsert_enricher_global_config(
        enricher_key=enricher_key,
        auto_run_default=body.auto_run_default,
        updated_by=admin.id,
    )
    await store.record_audit(
        action="admin.enricher_global_config",
        actor=admin,
        target_type="enricher",
        target_id=enricher_key,
        detail={"auto_run_default": body.auto_run_default},
    )
    return {"config": config.to_dict()}


@router.post("/enrichers/{enricher_key}/asset")
async def upload_enricher_asset(
    enricher_key: str,
    file: UploadFile = File(...),  # noqa: B008
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Upload (or replace) the data asset an enricher declares via ``asset_spec``.

    Content validation and atomic installation are the enricher's job
    (``Enricher.install_asset``, e.g. GeoIP's City-flavor check + metadata
    sidecar); this endpoint only streams the upload, maps
    ``AssetValidationError`` to 400, refreshes availability, and audits.

    Replacing an asset under a running system needs no invalidation: each
    job run works on a fresh enricher instance (``Enricher.spawn()``) with
    its own file handles, so new runs see the new file. A job already in
    flight keeps its old open handle (safe on Linux — the inode lives until
    the handle closes) and its results carry the *old* asset's config hash
    captured at job start, so provenance stays accurate either way.
    """
    from vestigo.enrichers.base import AssetValidationError
    from vestigo.enrichers.registry import get_enricher, refresh_availability

    enricher = get_enricher(enricher_key)
    if enricher is None:
        raise HTTPException(status_code=404, detail="Unknown enricher")
    if enricher.asset_spec is None:
        raise HTTPException(status_code=400, detail="Enricher requires no uploaded asset")

    store = get_store()
    max_bytes = get_settings().max_upload_bytes or None
    suffix = enricher.asset_spec.file_extensions[0] if enricher.asset_spec.file_extensions else ""
    tmp_path, sha256, _size = await receive_upload_to_tmp(file, max_bytes=max_bytes, suffix=suffix)

    try:
        install_detail = await asyncio.to_thread(enricher.install_asset, tmp_path, sha256)
    except AssetValidationError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    availability = (await asyncio.to_thread(refresh_availability, enricher_key)).get(enricher_key)
    await store.record_audit(
        action="admin.enricher_asset_upload",
        actor=admin,
        target_type="enricher",
        target_id=enricher_key,
        detail={"filename": file.filename, "sha256": sha256, **install_detail},
    )
    return {
        "available": availability.available if availability else False,
        "reason": availability.reason if availability else None,
        "detail": install_detail,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Instance settings (every VESTIGO_* tunable, DB-backed)
# ═════════════════════════════════════════════════════════════════════════════


class SettingsUpdate(BaseModel):
    """Payload to change instance settings.

    ``values`` maps field name to new value; ``null`` clears that field's
    stored override so it falls back to the environment, then the built-in
    default. A field absent from the mapping is left untouched — the console
    only ever sends what the admin edited.
    """

    values: dict[str, Any]


def _settings_payload() -> dict[str, Any]:
    """Describe every setting: metadata, effective value, and where it came from.

    ``source`` is ``"env"`` when the operator pinned the field (it then ignores
    anything stored), ``"db"`` when an admin override is applied, ``"default"``
    otherwise — the same vocabulary the agent settings endpoint uses. Secret
    values are replaced by a ``value_set`` boolean and never leave the process.
    """
    settings = get_settings()
    overrides = runtime_overrides()
    secrets = secret_fields()
    fields: list[dict[str, Any]] = []
    for spec in all_specs():
        pinned = env_pinned(spec.field)
        if pinned:
            source = "env"
        elif spec.field in overrides:
            source = "db"
        else:
            source = "default"
        value = getattr(settings, spec.field, None)
        entry: dict[str, Any] = {
            "field": spec.field,
            "group": spec.group,
            "label": spec.label,
            "help": spec.help,
            "kind": field_kind(spec.field),
            "nullable": is_nullable(spec.field),
            "constraints": field_constraints(spec.field),
            "choices": list(spec.choices) if spec.choices else None,
            "default": default_value(spec.field),
            "source": source,
            "env_var": env_var_name(spec.field),
            "env_only": spec.env_only,
            "restart_required": spec.restart_required,
            "subsystem": spec.subsystem,
            "editable": not spec.env_only and not pinned,
        }
        if spec.field in secrets:
            entry["value"] = None
            entry["value_set"] = bool(value)
        else:
            entry["value"] = value
        fields.append(entry)
    config = resolve_agent_config(settings)
    return {
        "groups": [{"key": g.key, "label": g.label, "description": g.description} for g in GROUPS],
        "settings": fields,
        # env-only refuses secret storage instance-wide; the console disables
        # those inputs instead of letting the PUT fail.
        "secrets_mode": settings.secrets_mode,
        # The two things the agent group needs that no single field carries:
        # the tool catalog `agent_disabled_tools` toggles against, and advisory
        # guard-rails on the *combination* of resolved values (full fidelity
        # against an underpowered window). The warnings change no behaviour —
        # every override stands — but the operator should see them before an
        # investigation dies of the combination.
        "agent": {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "embeddings_gated": t.embeddings_gated,
                    "requires_conversation": t.requires_conversation,
                }
                for t in TOOL_REGISTRY
            ],
            "warnings": [
                w
                for w in (fidelity_config_warning(config.tool_fidelity, config.context_window),)
                if w is not None
            ],
        },
    }


#: Agent string fields where a pasted value routinely carries stray whitespace
#: (a trailing space on a URL, a newline after an API key) that silently breaks
#: the endpoint probe. Whitespace-only degrades to an explicit clear.
_AGENT_TRIMMED_FIELDS: frozenset[str] = frozenset(
    {"agent_model", "agent_api_base_url", "agent_api_key", "agent_user_agent"}
)


def _normalize_agent_values(values: dict[str, Any]) -> dict[str, Any]:
    """Trim agent strings and reject unknown tool names before validation.

    Two checks the generic ``Settings`` model cannot make: whitespace around a
    pasted credential is a formatting concern rather than a validation one, and
    the tool catalogue lives in ``agent/tools.py``, which ``core/config.py``
    must not import. They ran on the retired agent-settings PUT; they run here
    now, on the only surface that writes these fields.
    """
    normalized = dict(values)
    for field_name in _AGENT_TRIMMED_FIELDS & set(normalized):
        value = normalized[field_name]
        if isinstance(value, str):
            normalized[field_name] = value.strip() or None
    tools = normalized.get("agent_disabled_tools")
    if isinstance(tools, list):
        unknown = sorted({t for t in tools if t not in TOOL_NAMES})
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"unknown tool name(s): {', '.join(unknown)}"
            )
        normalized["agent_disabled_tools"] = sorted(set(tools)) or None
    return normalized


@router.get("/settings")
async def get_instance_settings(admin: User = Depends(require_admin)) -> dict[str, Any]:
    """Return every configurable setting with its effective value and source."""
    return _settings_payload()


@router.put("/settings")
async def set_instance_settings(
    payload: SettingsUpdate, admin: User = Depends(require_admin)
) -> dict[str, Any]:
    """Persist settings overrides and apply them to the running process.

    Validation runs against the whole merged ``Settings`` model before
    anything is written, so a rejected change leaves both the database and the
    process untouched. Audited with field *names* only — values may include
    secrets, which never enter the audit trail.
    """
    if not payload.values:
        return _settings_payload()
    values = _normalize_agent_values(payload.values)
    try:
        applied = await save_runtime_settings(values, admin.id)
    except SettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # A changed endpoint changes what the availability probe would answer;
    # drop its cache so /api/health reflects the edit at once instead of
    # after the TTL.
    reset_probe_cache()
    await get_store().record_audit(
        action="admin.settings_update",
        actor=admin,
        target_type="app_settings",
        target_id="global",
        detail={"fields": sorted(payload.values)},
    )
    return {**_settings_payload(), "applied": sorted(applied)}


# ═════════════════════════════════════════════════════════════════════════════
# AI agent: the endpoint's model listing
# ═════════════════════════════════════════════════════════════════════════════
#
# The agent's *configuration* is not here: every knob is a ``VESTIGO_AGENT_*``
# field persisted through the settings endpoints above (migration 0033 retired
# the separate ``agent_settings`` row and its PUT). What remains is a probe —
# asking the operator's endpoint which models it serves — which persists
# nothing and therefore is not a settings route.


class AgentModelsRequest(BaseModel):
    """Credentials to list models against, before they have been saved.

    Each field overrides the corresponding resolved-config value for this
    request only; omitting one falls back to what is already configured. That
    fallback is what makes the listing work when the key is env-pinned or
    already stored — the admin UI never holds those, so it cannot send them.
    """

    api_base_url: str | None = None
    api_key: str | None = None
    provider: str | None = None


@router.post("/agent/models")
async def list_agent_models(
    payload: AgentModelsRequest, admin: User = Depends(require_admin)
) -> dict[str, Any]:
    """List the model ids the configured LLM endpoint advertises.

    Populates the model picker on the settings page, which is why it takes the
    *unsaved* form values: an admin typing a new endpoint and key should see
    its models before committing them. Nothing is persisted and the probe
    cache is untouched — this is a read against the operator's own endpoint.

    Reaching the network here is deliberate and admin-triggered, consistent
    with the availability probe (see `TECH_STACK.md` §6): it only ever talks
    to the endpoint the operator configured, never to a third party.

    Always 200. An unreachable endpoint, a rejected key, or one that serves
    no listing all return an empty list — the UI falls back to free-text
    model entry, so a failure here is not an error condition.
    """
    config = resolve_agent_config()
    # An env-pinned field is not overridable here, matching the settings PUT
    # and the disabled inputs in the UI. It also closes an exfiltration path
    # the pin would otherwise not cover: overriding `api_base_url` while the
    # key stays env-pinned would send the operator's key — which this API
    # never discloses — to a host of the caller's choosing.
    overrides = {
        f: v
        for f, v in payload.model_dump(exclude_unset=True).items()
        if v not in (None, "") and config.sources.get(f) != "env"
    }
    if overrides:
        config = replace(config, **overrides)
    return {"models": await list_models(config)}


@router.post("/agent/probe")
async def probe_agent_endpoint(admin: User = Depends(require_admin)) -> dict[str, Any]:
    """Re-probe the configured LLM endpoint now, ignoring the cached result.

    "Test connection" on the settings page. The availability cache exists so
    ``/api/health`` never waits on a hung endpoint, but an admin who just saved
    a URL is asking precisely for a fresh answer — and a stale one up to
    ``agent_probe_ttl_seconds`` old would read as their edit having failed.
    Persists nothing.
    """
    reset_probe_cache()
    return {"available": await agent_available(force=True)}
