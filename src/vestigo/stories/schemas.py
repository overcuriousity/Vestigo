"""Block-content contracts for Stories (W7).

The DB stores ``StoryBlock.content`` opaquely; this module is the single
validation gate at the API boundary (the HTTP router and the agent's
``propose_story_block`` both call ``validate_block_content``). Snapshot
hashing follows the project's canonical-JSON convention (``sort_keys``,
no whitespace) so a snapshot hash is reproducible from the stored JSON.
"""

import hashlib
import json

from pydantic import BaseModel, Field, ValidationError

BLOCK_KINDS = ("markdown", "view_ref", "chart_ref", "event_ref")

#: Hard cap on rows a view block may freeze into a snapshot.
VIEW_BLOCK_ROW_CAP = 1000


class MarkdownContent(BaseModel):
    text: str


class ViewDisplay(BaseModel):
    limit: int = Field(default=200, ge=1, le=VIEW_BLOCK_ROW_CAP)
    columns: list[str] | None = None


class ViewRefContent(BaseModel):
    view_id: str
    timeline_id: str
    display: ViewDisplay = Field(default_factory=ViewDisplay)


class ChartRefContent(BaseModel):
    chart_id: str
    timeline_id: str


class EventRefContent(BaseModel):
    event_id: str
    source_id: str
    caption: str | None = None


_CONTENT_MODELS: dict[str, type[BaseModel]] = {
    "markdown": MarkdownContent,
    "view_ref": ViewRefContent,
    "chart_ref": ChartRefContent,
    "event_ref": EventRefContent,
}


def validate_block_content(kind: str, content: dict) -> dict:
    """Validate + normalize a block's content payload for its kind.

    Raises ValueError (with the pydantic detail) so HTTP and agent callers
    can surface one consistent message.
    """
    model = _CONTENT_MODELS.get(kind)
    if model is None:
        raise ValueError(f"unknown block kind {kind!r}; expected one of {', '.join(BLOCK_KINDS)}")
    try:
        return model.model_validate(content or {}).model_dump()
    except ValidationError as exc:
        raise ValueError(f"invalid {kind} content: {exc}") from exc


def canonical_hash(payload: dict) -> str:
    """SHA-256 over canonical JSON — same convention as config hashes."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
