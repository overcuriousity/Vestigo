"""Block-content contracts for Stories (W7).

The DB stores ``StoryBlock.content`` opaquely; this module is the single
validation gate at the API boundary (the HTTP router and the agent's
``propose_story_block`` both call ``validate_block_content``). Snapshot
hashing follows the project's canonical-JSON convention (``sort_keys``,
no whitespace) so a snapshot hash is reproducible from the stored JSON.
"""

import hashlib
import json

from pydantic import BaseModel, Field, ValidationError, field_validator

from vestigo.core.config import get_settings

BLOCK_KINDS = ("markdown", "view_ref", "chart_ref", "event_ref")

#: Hard cap on rows a view block may freeze into a snapshot.
VIEW_BLOCK_ROW_CAP = 1000

#: Ceiling on a story title, mirroring ``Story.title``'s ``String(255)``.
#: Enforced by both write paths (the HTTP router and the agent's
#: ``propose_story``) because the column is where an over-long one would
#: otherwise surface — as a driver error, i.e. a 500 on a typo.
STORY_TITLE_MAX_CHARS = 255

#: Ceiling on an ``event_ref`` caption. A caption is a one-line label under a
#: frozen event, not a second narrative block — that's what markdown is for.
MAX_CAPTION_CHARS = 1000


class MarkdownContent(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _bounded(cls, value: str) -> str:
        """Bound a block's prose.

        A markdown block is copied verbatim into every subsequent export
        snapshot, so an unbounded one multiplies across the case's exports
        rather than costing its size once.
        """
        limit = get_settings().story_max_markdown_bytes
        if len(value.encode("utf-8")) > limit:
            raise ValueError(f"markdown text exceeds the {limit}-byte cap")
        return value


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
    caption: str | None = Field(default=None, max_length=MAX_CAPTION_CHARS)


_CONTENT_MODELS: dict[str, type[BaseModel]] = {
    "markdown": MarkdownContent,
    "view_ref": ViewRefContent,
    "chart_ref": ChartRefContent,
    "event_ref": EventRefContent,
}


#: One example payload per block kind, written the way a model should emit it.
#: Advertised in ``propose_story_block``'s docstring and echoed back on every
#: content error, because the JSON schema for an opaque ``content`` object says
#: nothing about which keys a kind needs — a model that guesses ``{"markdown":
#: ...}`` for a markdown block gets a bare "field required" and no way to learn
#: the answer. ``tests/test_stories_store.py`` pins each hint against its
#: model's required fields so the two cannot drift.
CONTENT_SHAPES: dict[str, str] = {
    "markdown": '{"text": "..."}',
    "view_ref": '{"view_id": "...", "timeline_id": "...", "display": {"limit": 200}}',
    "chart_ref": '{"chart_id": "...", "timeline_id": "..."}',
    "event_ref": '{"event_id": "...", "source_id": "...", "caption": "..."}',
}

#: Fallback for an unknown kind — names the kinds rather than a shape.
CONTENT_SHAPE_HINT: str = "one of " + ", ".join(f"{k} {v}" for k, v in CONTENT_SHAPES.items())


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


def canonical_json(payload: dict) -> str:
    """Serialize a snapshot the way its hash is computed.

    ``allow_nan=False`` on purpose: Python happily emits bare ``NaN`` and
    ``Infinity``, which are not JSON. Hashing bytes that no conforming parser
    accepts would leave an export whose hash cannot be independently verified,
    so a non-finite float has to fail loudly here rather than be frozen. The
    resolver coerces them upstream (``vestigo.stories.export._json_safe``).
    """
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def canonical_hash(payload: dict) -> str:
    """SHA-256 over canonical JSON — same convention as config hashes.

    Verifying a downloaded snapshot means re-serializing it with
    ``canonical_json`` first; the bytes a JSON encoder happens to produce are
    not the hashed bytes. ``GET .../exports/{id}/snapshot`` serves exactly
    these bytes so a third party can hash the response body directly.
    """
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
