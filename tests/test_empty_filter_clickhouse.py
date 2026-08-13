"""Live-ClickHouse tests for the `empty` field match mode.

Proves that "no value" means both "attribute key absent" and "attribute
present but blank", that a whitespace-only value counts as a value (it is what
the source recorded — collapsing it into "absent" would make the filter lie
about the evidence), and that include and exclude partition the rows exactly.
Requires the dev compose stack (skipped when ClickHouse is unreachable), same
pattern as ``test_search_blob_clickhouse.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.queries import EventQuery, EventQueryService
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-empty-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-empty"


def _event(i: int, **overrides) -> Event:
    kwargs: dict = {
        "case_id": CASE_ID,
        "source_id": SOURCE_ID,
        "source_file": Path("evidence.log"),
        "byte_offset": i * 100,
        "content_hash": f"{i:064d}",
        "file_hash": "c" * 64,
        "parser_name": "test-empty",
        "parser_version": "1.0.0",
        "raw_line": f"raw {i}",
        "message": f"event {i}",
        "timestamp": f"2026-01-0{1 + i}T10:00:00+00:00",
        "timestamp_desc": "Test Time",
        "artifact": "test:empty",
    }
    kwargs.update(overrides)
    return Event(**kwargs)


def _fixture_events() -> list[Event]:
    """The four states a field can be in, one event each."""
    return [
        # The key is not in the Map at all — ClickHouse reads it as ''.
        _event(0, message="absent", attributes={"other": "x"}),
        # The key is there and blank.
        _event(1, message="blank", attributes={"user_agent": ""}),
        # Whitespace is a value the source recorded, not an absence.
        _event(2, message="space", attributes={"user_agent": " "}),
        _event(3, message="curl", attributes={"user_agent": "curl/8.4"}),
    ]


@pytest.fixture(scope="module")
def service():
    store = ClickHouseStore()
    store.init_schema()
    store.insert_events(_fixture_events())
    yield EventQueryService(store=store)
    store.delete_source_events(CASE_ID, SOURCE_ID)


def _messages(service: EventQueryService, query: EventQuery) -> set[str]:
    return {e["message"] for e in service.query(query).events}


def test_empty_include_matches_absent_and_blank(service: EventQueryService) -> None:
    found = _messages(
        service,
        EventQuery(
            case_id=CASE_ID,
            field_filters={"user_agent": [""]},
            filter_modes={"user_agent": "empty"},
            limit=100,
        ),
    )
    assert found == {"absent", "blank"}


def test_empty_exclude_keeps_whitespace_and_real_values(service: EventQueryService) -> None:
    found = _messages(
        service,
        EventQuery(
            case_id=CASE_ID,
            field_exclusions={"user_agent": [""]},
            exclusion_modes={"user_agent": "empty"},
            limit=100,
        ),
    )
    assert found == {"space", "curl"}


def test_include_and_exclude_partition_the_rows(service: EventQueryService) -> None:
    """Every event lands on exactly one side. This is the property the ifNull
    exists for: a NULL comparison would drop a row from both."""
    include = _messages(
        service,
        EventQuery(
            case_id=CASE_ID,
            field_filters={"user_agent": [""]},
            filter_modes={"user_agent": "empty"},
            limit=100,
        ),
    )
    exclude = _messages(
        service,
        EventQuery(
            case_id=CASE_ID,
            field_exclusions={"user_agent": [""]},
            exclusion_modes={"user_agent": "empty"},
            limit=100,
        ),
    )
    assert include & exclude == set()
    assert include | exclude == {"absent", "blank", "space", "curl"}
