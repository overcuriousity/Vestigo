"""Stories (W7): per-case block documents. See docs/STORIES.md.

Case-scoped routes so the shared ``require_case_read``/``require_case_contribute``
dependencies apply unchanged. Block writes carry an optimistic ``version``;
a stale write returns 409 with the current block so the editor can present
the conflict instead of clobbering a collaborator's edit.
"""

import hashlib
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from vestigo.api.deps import (
    get_current_user,
    get_store,
    require_admin,
    require_case_contribute,
    require_case_read,
)
from vestigo.db.postgres import Case, StaleBlockError, StoryBlock, User
from vestigo.stories.export import resolve_story_snapshot
from vestigo.stories.schemas import canonical_hash, validate_block_content

#: Upper bound on an uploaded export artifact (standalone HTML), bytes.
MAX_ARTIFACT_BYTES = 20_000_000

router = APIRouter(prefix="/api/cases", tags=["stories"])


class StoryBody(BaseModel):
    title: str | None = None
    description: str | None = None


class BlockCreateBody(BaseModel):
    kind: str
    content: dict[str, Any]
    after_block_id: str | None = None


class BlockUpdateBody(BaseModel):
    content: dict[str, Any]
    version: int


class BlockMoveBody(BaseModel):
    after_block_id: str | None = None
    version: int


class ArtifactBody(BaseModel):
    html: str


async def _get_story_or_404(case_id: str, story_id: str):
    story = await get_store().get_story(case_id, story_id)
    if story is None:
        raise HTTPException(status_code=404, detail="Story not found")
    return story


async def _get_block_or_404(story_id: str, block_id: str) -> StoryBlock:
    block = await get_store().get_story_block(block_id)
    if block is None or block.story_id != story_id:
        raise HTTPException(status_code=404, detail="Block not found")
    return block


@router.get("/{case_id}/stories")
async def list_stories(case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """List the case's stories, newest first."""
    stories = await get_store().list_stories(case.id)
    return {"stories": [s.to_dict() for s in stories]}


@router.post("/{case_id}/stories")
async def create_story(
    body: StoryBody,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a story."""
    if not (body.title or "").strip():
        raise HTTPException(status_code=422, detail="title is required")
    store = get_store()
    story = await store.create_story(
        case.id, uuid.uuid4().hex, body.title.strip(), body.description, user=user.username
    )
    await store.record_audit(
        action="story.create",
        actor=user,
        case_id=case.id,
        target_type="story",
        target_id=story.id,
    )
    return {"story": story.to_dict()}


@router.get("/{case_id}/stories/{story_id}")
async def get_story(
    story_id: str, case: Case = Depends(require_case_read)
) -> dict[str, Any]:
    """Return a story with its blocks in document order."""
    story = await _get_story_or_404(case.id, story_id)
    blocks = await get_store().list_story_blocks(story.id)
    return {"story": story.to_dict(), "blocks": [b.to_dict() for b in blocks]}


@router.patch("/{case_id}/stories/{story_id}")
async def update_story(
    story_id: str,
    body: StoryBody,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Update a story's title/description."""
    await _get_story_or_404(case.id, story_id)
    story = await get_store().update_story(
        case.id, story_id, title=body.title, description=body.description, user=user.username
    )
    return {"story": story.to_dict()}


@router.delete("/{case_id}/stories/{story_id}")
async def delete_story(
    story_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a story with its blocks and exports (audited)."""
    store = get_store()
    deleted = await store.delete_story(case.id, story_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Story not found")
    await store.record_audit(
        action="story.delete",
        actor=user,
        case_id=case.id,
        target_type="story",
        target_id=story_id,
    )
    return {"deleted": True}


@router.post("/{case_id}/stories/{story_id}/blocks")
async def create_block(
    story_id: str,
    body: BlockCreateBody,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Append or insert a block (also the push target for "Add to story")."""
    story = await _get_story_or_404(case.id, story_id)
    try:
        content = validate_block_content(body.kind, body.content)
        block = await get_store().create_story_block(
            story.id,
            uuid.uuid4().hex,
            body.kind,
            content,
            user=user.username,
            after_block_id=body.after_block_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"block": block.to_dict()}


@router.patch("/{case_id}/stories/{story_id}/blocks/{block_id}")
async def update_block(
    story_id: str,
    block_id: str,
    body: BlockUpdateBody,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Update a block's content under optimistic concurrency (409 when stale)."""
    await _get_story_or_404(case.id, story_id)
    existing = await _get_block_or_404(story_id, block_id)
    try:
        content = validate_block_content(existing.kind, body.content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        block = await get_store().update_story_block(
            block_id, content, expected_version=body.version, user=user.username
        )
    except StaleBlockError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "block changed", "block": exc.current.to_dict()},
        ) from exc
    if block is None:
        raise HTTPException(status_code=404, detail="Block not found")
    return {"block": block.to_dict()}


@router.post("/{case_id}/stories/{story_id}/blocks/{block_id}/move")
async def move_block(
    story_id: str,
    block_id: str,
    body: BlockMoveBody,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Reposition a block; ``after_block_id: null`` moves it to the top."""
    await _get_story_or_404(case.id, story_id)
    await _get_block_or_404(story_id, block_id)
    try:
        block = await get_store().move_story_block(
            block_id, body.after_block_id, expected_version=body.version, user=user.username
        )
    except StaleBlockError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "block changed", "block": exc.current.to_dict()},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if block is None:
        raise HTTPException(status_code=404, detail="Block not found")
    return {"block": block.to_dict()}


@router.delete("/{case_id}/stories/{story_id}/blocks/{block_id}")
async def delete_block(
    story_id: str,
    block_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a block."""
    await _get_story_or_404(case.id, story_id)
    await _get_block_or_404(story_id, block_id)
    await get_store().delete_story_block(block_id)
    return {"deleted": True}


async def _get_export_or_404(case_id: str, story_id: str, export_id: str):
    export = await get_store().get_story_export(case_id, export_id)
    if export is None or export.story_id != story_id:
        raise HTTPException(status_code=404, detail="Export not found")
    return export


@router.post("/{case_id}/stories/{story_id}/exports")
async def create_export(
    story_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Freeze the story into an immutable, hashed point-in-time snapshot.

    The server resolves every block itself (view queries, chart execution,
    event fetches) — the snapshot is authoritative; the client-rendered HTML
    artifact uploaded afterwards is presentation only.
    """
    story = await _get_story_or_404(case.id, story_id)
    store = get_store()
    blocks = await store.list_story_blocks(story.id)
    snapshot = await resolve_story_snapshot(story, blocks, user=user)
    export = await store.create_story_export(
        uuid.uuid4().hex,
        story.id,
        case.id,
        snapshot,
        canonical_hash(snapshot),
        user=user.username,
    )
    await store.record_audit(
        action="story.export",
        actor=user,
        case_id=case.id,
        target_type="story_export",
        target_id=export.id,
    )
    return {"export": export.to_dict(include_snapshot=True)}


@router.post("/{case_id}/stories/{story_id}/exports/{export_id}/artifact")
async def seal_export_artifact(
    story_id: str,
    export_id: str,
    body: ArtifactBody,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Attach the client-rendered standalone HTML to an export, exactly once."""
    await _get_export_or_404(case.id, story_id, export_id)
    if len(body.html.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="artifact exceeds the size cap")
    html_hash = hashlib.sha256(body.html.encode("utf-8")).hexdigest()
    export = await get_store().seal_story_export_artifact(export_id, body.html, html_hash)
    if export is None:
        raise HTTPException(status_code=409, detail="export already carries an artifact")
    return {"export": export.to_dict()}


@router.get("/{case_id}/stories/{story_id}/exports")
async def list_exports(
    story_id: str, case: Case = Depends(require_case_read)
) -> dict[str, Any]:
    """List a story's exports, newest first (snapshots omitted)."""
    await _get_story_or_404(case.id, story_id)
    exports = await get_store().list_story_exports(story_id)
    return {"exports": [e.to_dict() for e in exports]}


@router.get("/{case_id}/stories/{story_id}/exports/{export_id}/snapshot")
async def download_export_snapshot(
    story_id: str, export_id: str, case: Case = Depends(require_case_read)
) -> dict[str, Any]:
    """Download the frozen snapshot JSON."""
    export = await _get_export_or_404(case.id, story_id, export_id)
    return export.snapshot


@router.get("/{case_id}/stories/{story_id}/exports/{export_id}/artifact")
async def download_export_artifact(
    story_id: str, export_id: str, case: Case = Depends(require_case_read)
) -> Response:
    """Download the sealed standalone HTML artifact."""
    export = await _get_export_or_404(case.id, story_id, export_id)
    if export.html is None:
        raise HTTPException(status_code=404, detail="export has no artifact yet")
    return Response(
        content=export.html,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="story-{story_id}-{export_id}.html"'
            )
        },
    )


@router.delete("/{case_id}/stories/{story_id}/exports/{export_id}")
async def delete_export(
    story_id: str,
    export_id: str,
    case: Case = Depends(require_case_read),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Delete an export — admin only; exports are otherwise immutable."""
    await _get_export_or_404(case.id, story_id, export_id)
    store = get_store()
    await store.delete_story_export(export_id)
    await store.record_audit(
        action="story.export_delete",
        actor=user,
        case_id=case.id,
        target_type="story_export",
        target_id=export_id,
    )
    return {"deleted": True}
