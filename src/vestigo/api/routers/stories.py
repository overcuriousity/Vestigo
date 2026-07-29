"""Stories (W7): per-case block documents. See docs/STORIES.md.

Case-scoped routes so the shared ``require_case_read``/``require_case_contribute``
dependencies apply unchanged. Block writes carry an optimistic ``version``; a
stale write returns 409 so the caller presents the conflict instead of
clobbering a collaborator's edit. The winning block travels in the 409 body for
API consumers; the browser client collapses an error body to its message, so the
editor refetches and reads the winner from fresh data instead.
"""

import hashlib
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from vestigo.api.deps import (
    get_store,
    require_admin,
    require_case_contribute,
    require_case_read,
    require_password_current,
)
from vestigo.core.config import get_settings
from vestigo.db.postgres import UNSET, Case, StaleBlockError, StoryBlock, User
from vestigo.stories.export import SnapshotTooLargeError, resolve_story_snapshot
from vestigo.stories.refs import validate_block_scope
from vestigo.stories.schemas import canonical_hash, canonical_json, validate_block_content

router = APIRouter(prefix="/api/cases", tags=["stories"])


class StoryBody(BaseModel):
    title: str | None = None
    description: str | None = None


class BlockCreateBody(BaseModel):
    kind: str
    content: dict[str, Any]
    after_block_id: str | None = None
    #: Place the block above every existing one. Needed because
    #: ``after_block_id: null`` means "append at end" here while it means
    #: "top" on the move endpoint — so on create there is no anchor that can
    #: name the top. Mutually exclusive with ``after_block_id``.
    at_top: bool = False


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
    user: User = Depends(require_password_current),
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
async def get_story(story_id: str, case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """Return a story with its blocks in document order."""
    story = await _get_story_or_404(case.id, story_id)
    blocks = await get_store().list_story_blocks(story.id)
    return {"story": story.to_dict(), "blocks": [b.to_dict() for b in blocks]}


@router.patch("/{case_id}/stories/{story_id}")
async def update_story(
    story_id: str,
    body: StoryBody,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Update a story's title/description.

    Only fields present in the request body are touched, so
    ``{"description": null}`` clears the description while ``{"title": "x"}``
    leaves it alone. A supplied title must be non-blank — the same rule
    ``POST`` applies, rather than letting PATCH blank it out.
    """
    await _get_story_or_404(case.id, story_id)
    sent = body.model_fields_set
    title: Any = UNSET
    if "title" in sent:
        if not (body.title or "").strip():
            raise HTTPException(status_code=422, detail="title cannot be blank")
        title = body.title.strip()
    story = await get_store().update_story(
        case.id,
        story_id,
        title=title,
        description=body.description if "description" in sent else UNSET,
        user=user.username,
    )
    if story is None:
        raise HTTPException(status_code=404, detail="Story not found")
    return {"story": story.to_dict()}


@router.delete("/{case_id}/stories/{story_id}")
async def delete_story(
    story_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Delete a story with its blocks and exports (audited).

    Deleting a single export is admin-only because an export is an immutable
    attestation; the story cascade takes exports too, so a story that carries
    any is admin-only to delete as well — otherwise the cascade would be a way
    around that gate. The deleted exports' hashes go into the audit record so
    the attestation trail outlives the rows.
    """
    store = get_store()
    await _get_story_or_404(case.id, story_id)
    exports = await store.list_story_exports(story_id)
    if exports and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail=(
                f"story has {len(exports)} sealed export(s); deleting it requires an administrator"
            ),
        )
    summary = await store.delete_story(case.id, story_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Story not found")
    await store.record_audit(
        action="story.delete",
        actor=user,
        case_id=case.id,
        target_type="story",
        target_id=story_id,
        detail=summary,
    )
    return {"deleted": True}


@router.post("/{case_id}/stories/{story_id}/blocks")
async def create_block(
    story_id: str,
    body: BlockCreateBody,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Append or insert a block (also the push target for "Add to story").

    Appends at the end by default; ``after_block_id`` inserts after that
    block and ``at_top`` puts it above every existing one. The mutual
    exclusion is enforced here as a 422 rather than left to the store's
    ValueError, so the contract is visible in the OpenAPI schema.
    """
    story = await _get_story_or_404(case.id, story_id)
    if body.at_top and body.after_block_id is not None:
        raise HTTPException(
            status_code=422, detail="at_top and after_block_id are mutually exclusive"
        )
    try:
        content = validate_block_content(body.kind, body.content)
        await validate_block_scope(case.id, body.kind, content)
        block = await get_store().create_story_block(
            story.id,
            uuid.uuid4().hex,
            body.kind,
            content,
            user=user.username,
            after_block_id=body.after_block_id,
            at_top=body.at_top,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if block is None:
        raise HTTPException(status_code=404, detail="Story not found")
    return {"block": block.to_dict()}


@router.patch("/{case_id}/stories/{story_id}/blocks/{block_id}")
async def update_block(
    story_id: str,
    block_id: str,
    body: BlockUpdateBody,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Update a block's content under optimistic concurrency (409 when stale)."""
    await _get_story_or_404(case.id, story_id)
    existing = await _get_block_or_404(story_id, block_id)
    try:
        content = validate_block_content(existing.kind, body.content)
        await validate_block_scope(case.id, existing.kind, content)
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
    user: User = Depends(require_password_current),
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
    version: int,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Delete a block under the same optimistic concurrency as an edit (409 when stale).

    ``version`` rides as a query parameter because DELETE bodies are not
    reliably carried end to end. It is required, not optional: deleting a
    block a collaborator has meanwhile rewritten would destroy their edit
    silently, which is exactly what the version guard on update and move
    exists to prevent.
    """
    await _get_story_or_404(case.id, story_id)
    await _get_block_or_404(story_id, block_id)
    try:
        await get_store().delete_story_block(block_id, expected_version=version)
    except StaleBlockError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "block changed", "block": exc.current.to_dict()},
        ) from exc
    return {"deleted": True}


async def _read_capped_body(request: Request, cap: int) -> bytes:
    """Read the request body, abandoning it the moment it exceeds ``cap``.

    A declared ``Content-Length`` is rejected up front; a chunked upload
    declares none, so the stream is also counted as it arrives. ``cap <= 0``
    disables the limit, matching the other ``VESTIGO_MAX_*_BYTES`` settings.
    """
    if cap <= 0:
        return await request.body()
    too_large = HTTPException(status_code=413, detail="artifact exceeds the size cap")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise too_large
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


async def _get_export_or_404(case_id: str, story_id: str, export_id: str):
    export = await get_store().get_story_export(case_id, export_id)
    if export is None or export.story_id != story_id:
        raise HTTPException(status_code=404, detail="Export not found")
    return export


@router.post("/{case_id}/stories/{story_id}/exports")
async def create_export(
    story_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Freeze the story into an immutable, hashed point-in-time snapshot.

    The server resolves every block itself (view queries, chart execution,
    event fetches) — the snapshot is authoritative; the client-rendered HTML
    artifact uploaded afterwards is presentation only.

    Resolution runs synchronously inside the request and issues one or more
    ClickHouse queries per embed block, so both the block count and the
    resulting snapshot are capped (``VESTIGO_STORY_EXPORT_MAX_BLOCKS`` /
    ``VESTIGO_STORY_EXPORT_MAX_SNAPSHOT_BYTES``). The size ceiling is enforced
    *during* resolution, so a story that would blow past it stops costing
    memory and queries at the block that crosses the line — checking only the
    finished bundle would bound what gets stored while still materializing an
    arbitrarily large one first.
    """
    story = await _get_story_or_404(case.id, story_id)
    settings = get_settings()
    store = get_store()
    blocks = await store.list_story_blocks(story.id)
    if len(blocks) > settings.story_export_max_blocks:
        raise HTTPException(
            status_code=413,
            detail=(
                f"story has {len(blocks)} blocks; the export cap is "
                f"{settings.story_export_max_blocks}"
            ),
        )
    cap = settings.story_export_max_snapshot_bytes
    try:
        snapshot = await resolve_story_snapshot(story, blocks, user=user, max_bytes=cap)
    except SnapshotTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail=(
                f"resolved snapshot exceeds the {exc.cap}-byte cap at block "
                f"{exc.block_id}; shrink or split the story"
            ),
        ) from exc
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
    request: Request,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Attach the client-rendered standalone HTML to an export, exactly once.

    Takes the raw request rather than a parsed body model so the size cap is
    applied to the arriving stream: validating the decoded string would bound
    what gets *stored* while still buffering an unbounded body first.

    The HTML must carry the export's ``snapshot_hash``. Nothing else ties the
    artifact's content to the snapshot it claims to render — without this the
    server would hash and store whatever it was handed, and ``html_hash``
    would be presented with the same authority as ``snapshot_hash`` while
    attesting to nothing. Verification is still always against the snapshot;
    this only stops an artifact being sealed onto the wrong export.
    """
    export = await _get_export_or_404(case.id, story_id, export_id)
    raw = await _read_capped_body(request, get_settings().story_max_artifact_bytes)
    try:
        body = ArtifactBody.model_validate_json(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid artifact body: {exc}") from exc
    if export.snapshot_hash not in body.html:
        raise HTTPException(
            status_code=422,
            detail="artifact must embed the export's snapshot_hash",
        )
    html_hash = hashlib.sha256(body.html.encode("utf-8")).hexdigest()
    export = await get_store().seal_story_export_artifact(export_id, body.html, html_hash)
    if export is None:
        raise HTTPException(status_code=409, detail="export already carries an artifact")
    return {"export": export.to_dict()}


@router.get("/{case_id}/stories/{story_id}/exports")
async def list_exports(story_id: str, case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """List a story's exports, newest first (snapshots omitted)."""
    await _get_story_or_404(case.id, story_id)
    exports = await get_store().list_story_exports(story_id)
    return {"exports": [e.to_dict() for e in exports]}


@router.get("/{case_id}/stories/{story_id}/exports/{export_id}/snapshot")
async def download_export_snapshot(
    story_id: str, export_id: str, case: Case = Depends(require_case_read)
) -> Response:
    """Download the frozen snapshot JSON.

    Serves the *canonical* bytes — the exact serialization ``snapshot_hash``
    was computed over — rather than letting the framework re-encode the dict
    with its own key order and spacing. A third party can then verify the
    attestation by hashing the response body directly, with no knowledge of
    our canonicalization rules.
    """
    export = await _get_export_or_404(case.id, story_id, export_id)
    return Response(
        content=canonical_json(export.snapshot),
        media_type="application/json",
        headers={
            "Content-Disposition": (f'attachment; filename="story-{story_id}-{export_id}.json"'),
            "X-Vestigo-Snapshot-Hash": export.snapshot_hash,
        },
    )


@router.get("/{case_id}/stories/{story_id}/exports/{export_id}/artifact")
async def download_export_artifact(
    story_id: str, export_id: str, case: Case = Depends(require_case_read)
) -> Response:
    """Download the sealed standalone HTML artifact.

    The artifact is authored entirely by the client — the seal endpoint only
    checks that it embeds the export's ``snapshot_hash`` — and it is served
    back from the app's own origin, where the session cookie lives. Every UI
    path treats this as a download, so ``Content-Disposition: attachment`` is
    what stops a browser rendering analyst-supplied markup in that origin.
    ``nosniff`` and a ``sandbox`` CSP are the belt to that suspenders: the
    defense should not be one header deep, and nothing here needs a document
    that can run script or reach the network.
    """
    export = await _get_export_or_404(case.id, story_id, export_id)
    if export.html is None:
        raise HTTPException(status_code=404, detail="export has no artifact yet")
    return Response(
        content=export.html,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": (f'attachment; filename="story-{story_id}-{export_id}.html"'),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
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
    export = await _get_export_or_404(case.id, story_id, export_id)
    store = get_store()
    # Captured before the row goes: the hashes are the attestation, and the
    # audit log is the only place they survive the delete.
    detail = {"snapshot_hash": export.snapshot_hash, "html_hash": export.html_hash}
    await store.delete_story_export(export_id)
    await store.record_audit(
        action="story.export_delete",
        actor=user,
        case_id=case.id,
        target_type="story_export",
        target_id=export_id,
        detail=detail,
    )
    return {"deleted": True}
