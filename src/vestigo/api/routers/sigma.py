"""API routes for the Sigma rule runner (W5).

Rules: the global set is an admin-managed offline directory
(``VESTIGO_SIGMA_RULES_PATH``, re-read per request so a file drop needs no
restart); case rules are analyst uploads stored in Postgres. Runs are
background jobs (ephemeral ``JobStore`` for progress) paired with a
persistent ``SigmaRun`` record (per-rule compiled SQL, content hash, match
count) for forensic reproducibility.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from vestigo.api.deps import (
    get_current_user,
    get_store,
    require_case_contribute,
    require_case_read,
)
from vestigo.core.config import get_settings
from vestigo.core.jobs import get_job_store
from vestigo.db.postgres import Case, User
from vestigo.sigma.rules import (
    MAX_RULE_BYTES,
    LoadedRule,
    content_hash,
    load_global_rules,
    parse_rule_yaml,
    rule_key_for,
)

router = APIRouter(prefix="/api", tags=["sigma"])


def _global_rule_dict(rule: LoadedRule) -> dict[str, Any]:
    """Serialize a global-directory rule for listings (no YAML body — see GET note)."""
    return {
        "origin": "global",
        "ref": rule.ref,
        "rule_key": rule.rule_key,
        "title": rule.title,
        "rule_uuid": rule.rule_uuid,
        "level": rule.level,
        "logsource": rule.logsource,
        "content_hash": rule.content_hash,
        "error": rule.error,
        "enabled": True,
    }


async def _load_global() -> list[LoadedRule]:
    """Load the global ruleset off the event loop (disk walk + YAML parse)."""
    return await asyncio.to_thread(load_global_rules, get_settings().sigma_rules_path)


@router.get("/sigma/rules")
async def list_global_sigma_rules(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """List the global ruleset directory's rules (metadata only, any authenticated user)."""
    rules = await _load_global()
    return {
        "rules_path_configured": bool(get_settings().sigma_rules_path),
        "rules": [_global_rule_dict(r) for r in rules],
    }


@router.get("/cases/{case_id}/sigma/rules")
async def list_case_sigma_rules(case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """List global + case-uploaded rules for the run picker."""
    global_rules, case_rows = await asyncio.gather(
        _load_global(), get_store().list_sigma_rules(case.id)
    )
    case_dicts = []
    for row in case_rows:
        d = row.to_dict()
        d["ref"] = row.id
        case_dicts.append(d)
    return {
        "rules_path_configured": bool(get_settings().sigma_rules_path),
        "global_rules": [_global_rule_dict(r) for r in global_rules],
        "case_rules": case_dicts,
    }


class SigmaRuleUpload(BaseModel):
    """One Sigma rule YAML document uploaded to a case."""

    yaml_content: str = Field(..., min_length=1)


@router.post("/cases/{case_id}/sigma/rules")
async def upload_case_sigma_rule(
    body: SigmaRuleUpload,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Validate and store one case-scoped Sigma rule."""
    text = body.yaml_content
    if len(text.encode("utf-8")) > MAX_RULE_BYTES:
        raise HTTPException(status_code=413, detail="Rule exceeds the 1 MiB limit.")
    parsed, error = parse_rule_yaml(text)
    if parsed is None:
        raise HTTPException(status_code=422, detail=f"Not a valid Sigma rule: {error}")
    yaml_hash = content_hash(text)
    rule_uuid = str(parsed.id) if parsed.id else None
    store = get_store()
    for existing in await store.list_sigma_rules(case.id):
        if existing.content_hash == yaml_hash:
            raise HTTPException(status_code=409, detail="This exact rule is already uploaded.")
    logsource = {
        k: v
        for k, v in (
            ("product", parsed.logsource.product),
            ("category", parsed.logsource.category),
            ("service", parsed.logsource.service),
        )
        if v
    }
    rule = await store.create_sigma_rule(
        case_id=case.id,
        rule_key=rule_key_for(rule_uuid, yaml_hash),
        title=parsed.title or "(untitled rule)",
        yaml_content=text,
        content_hash=yaml_hash,
        rule_uuid=rule_uuid,
        level=str(parsed.level.name).lower() if parsed.level else None,
        logsource=logsource,
        created_by=user.id,
    )
    await store.record_audit(
        action="sigma.rule_upload",
        actor=user,
        case_id=case.id,
        target_type="sigma_rule",
        target_id=rule.id,
        detail={"title": rule.title, "content_hash": yaml_hash},
    )
    return {"rule": rule.to_dict()}


class SigmaRuleUpdate(BaseModel):
    """Toggle payload for an uploaded rule."""

    enabled: bool


@router.patch("/cases/{case_id}/sigma/rules/{rule_id}")
async def update_case_sigma_rule(
    rule_id: str,
    body: SigmaRuleUpdate,
    case: Case = Depends(require_case_contribute),
) -> dict[str, Any]:
    """Enable/disable an uploaded rule."""
    if not await get_store().set_sigma_rule_enabled(case.id, rule_id, body.enabled):
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"status": "ok", "enabled": body.enabled}


@router.delete("/cases/{case_id}/sigma/rules/{rule_id}")
async def delete_case_sigma_rule(
    rule_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete an uploaded rule (its historical run records and hits stay)."""
    store = get_store()
    rule = await store.get_sigma_rule(case.id, rule_id)
    if rule is None or not await store.delete_sigma_rule(case.id, rule_id):
        raise HTTPException(status_code=404, detail="Rule not found")
    await store.record_audit(
        action="sigma.rule_delete",
        actor=user,
        case_id=case.id,
        target_type="sigma_rule",
        target_id=rule_id,
        detail={"title": rule.title, "content_hash": rule.content_hash},
    )
    return {"status": "deleted"}


class SigmaRuleRef(BaseModel):
    """Reference to one rule in a run selection."""

    origin: str = Field(..., pattern="^(global|case)$")
    ref: str = Field(..., min_length=1)


class SigmaRunRequest(BaseModel):
    """Run request: which rules to evaluate (omit/empty = all enabled rules)."""

    rules: list[SigmaRuleRef] | None = None


@router.post("/cases/{case_id}/timelines/{timeline_id}/sigma/run")
async def start_sigma_run(
    timeline_id: str,
    body: SigmaRunRequest,
    background_tasks: BackgroundTasks,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Launch a Sigma evaluation job over the timeline's sources."""
    from vestigo.sigma.runner import run_sigma_job

    store = get_store()
    case_id = case.id
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    sources = await store.list_timeline_sources(case_id, timeline_id)
    if not sources:
        raise HTTPException(status_code=422, detail="Timeline has no sources.")
    source_ids = [s.id for s in sources]

    selection = [r.model_dump() for r in body.rules] if body.rules else None
    run = await store.create_sigma_run(
        case_id=case_id,
        timeline_id=timeline_id,
        params={"source_ids": source_ids, "selection": selection},
        created_by=user.id,
    )
    job_store = get_job_store()
    job = job_store.create(
        kind="sigma_run",
        progress={"rules_total": 0, "rules_done": 0, "hits": 0},
        created_by=user.id,
        case_id=case_id,
    )
    background_tasks.add_task(
        run_sigma_job,
        job.id,
        run.id,
        case_id,
        timeline_id,
        source_ids,
        selection,
        job_store,
        user.id,
        user.username,
    )
    return {"job_id": job.id, "run_id": run.id, "status": job.status}


@router.get("/cases/{case_id}/sigma/runs")
async def list_sigma_runs(case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """List the case's Sigma runs, newest first."""
    runs = await get_store().list_sigma_runs(case.id)
    return {"runs": [r.to_dict() for r in runs]}


@router.get("/cases/{case_id}/sigma/runs/{run_id}")
async def get_sigma_run(run_id: str, case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """Return one persisted Sigma run (per-rule SQL, counts, statuses)."""
    run = await get_store().get_sigma_run(case.id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"run": run.to_dict()}
