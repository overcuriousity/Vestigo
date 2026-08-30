"""`resolve_marks` over fakes: every provenance kind, the per-source cap, the refusals.

The ClickHouse side is `tests/test_marks_clickhouse.py`; here the service is a
fake so the *shape* of the answer — what a caption and a figure will read — is
pinned without a corpus.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from vestigo.agent.marks import resolve_marks
from vestigo.agent.tools import AgentScope, ChartMarkSpec
from vestigo.db.postgres import User


def _scope() -> AgentScope:
    return AgentScope(
        case_id="c1",
        timeline_id="t1",
        user=User(id="u1", username="tester", is_admin=True, is_active=True),
        source_ids=["s1"],
        field_mappings=None,
        source_offsets=None,
    )


class _FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, int]] = []

    def mark_instants(self, query, limit):
        self.calls.append((query, limit))
        n = 3 if (query.q or "") == "beacon" else 1
        return {
            "instants": [
                {"event_id": f"e{i}", "source_id": "s1", "at": f"2026-07-20T0{i}:00:00+00:00"}
                for i in range(1, min(n, limit) + 1)
            ],
            "dated": n,
            "undated": 1 if n == 3 else 0,
            "overflow": n > limit,
        }


class _FakeStore:
    async def get_baseline_definition(self, case_id, timeline_id, baseline_id):
        if baseline_id != "bd1":
            return None
        return SimpleNamespace(
            id="bd1",
            name="Quiet week",
            baseline_start=datetime(2026, 7, 1, tzinfo=UTC),
            baseline_end=datetime(2026, 7, 8, tzinfo=UTC),
            suspect_windows=[
                {
                    "id": "w0",
                    "label": "Exfil day",
                    "start": "2026-07-20T00:00:00+00:00",
                    "end": "2026-07-21T00:00:00+00:00",
                }
            ],
        )

    async def get_view(self, case_id, view_id):
        if view_id != "v1":
            return None
        return SimpleNamespace(id="v1", name="Beacons", query="", view_filter={"q": "beacon"})


async def _run(fn, *args):
    return fn(*args)


def _mark(**kw) -> ChartMarkSpec:
    return ChartMarkSpec.model_validate(kw)


async def test_every_source_kind_resolves_with_its_provenance():
    service = _FakeService()
    result = await resolve_marks(
        _scope(),
        [
            _mark(kind="instant", at="2026-07-20T09:41:00Z", label="first beacon"),
            _mark(
                kind="range",
                start="2026-07-20T09:00:00Z",
                end="2026-07-20T10:00:00Z",
                label="window",
            ),
            _mark(kind="baseline", definition_id="bd1"),
            _mark(kind="view", view_id="v1"),
            _mark(kind="events", filters={"q": "logon"}, label="logons"),
        ],
        service=service,
        store=_FakeStore(),
        cap=50,
        run=_run,
    )
    by_source = {}
    for m in result["marks"]:
        by_source.setdefault(m["source"], []).append(m)
    assert by_source[0] == [
        {
            "kind": "instant",
            "at": "2026-07-20T09:41:00+00:00",
            "label": "first beacon",
            "source": 0,
            "provenance": {"kind": "analyst"},
        }
    ]
    assert by_source[1][0]["kind"] == "range" and by_source[1][0]["provenance"] == {
        "kind": "analyst"
    }
    assert [m["label"] for m in by_source[2]] == ["Quiet week — baseline", "Quiet week — Exfil day"]
    assert by_source[2][0]["provenance"] == {
        "kind": "baseline",
        "definition_id": "bd1",
        "window_id": "baseline",
    }
    assert by_source[2][1]["provenance"] == {
        "kind": "baseline",
        "definition_id": "bd1",
        "window_id": "w0",
    }
    assert by_source[2][0]["start"] == "2026-07-01T00:00:00+00:00"
    assert len(by_source[3]) == 3 and by_source[3][0]["label"] == "Beacons"
    assert by_source[3][0]["provenance"] == {
        "kind": "view",
        "view_id": "v1",
        "event_id": "e1",
        "source_id": "s1",
    }
    assert by_source[4] == [
        {
            "kind": "instant",
            "at": "2026-07-20T01:00:00+00:00",
            "label": "logons",
            "source": 4,
            "provenance": {"kind": "event", "event_id": "e1", "source_id": "s1"},
        }
    ]
    assert result["cap"] == 50
    assert [s["kind"] for s in result["sources"]] == [
        "instant",
        "range",
        "baseline",
        "view",
        "events",
    ]
    assert result["sources"][2] == {
        "index": 2,
        "kind": "baseline",
        "label": "Quiet week",
        "count": 2,
        "shown": 2,
        "overflow": False,
        "undated": 0,
    }
    assert result["sources"][3] == {
        "index": 3,
        "kind": "view",
        "label": "Beacons",
        "count": 3,
        "shown": 3,
        "overflow": False,
        "undated": 1,
    }
    # The view's stored filter reached the query; the events source's label defaults when omitted.
    assert service.calls[0][0].q == "beacon" and service.calls[0][1] == 50


async def test_the_cap_is_per_source_and_disclosed():
    service = _FakeService()
    result = await resolve_marks(
        _scope(),
        [_mark(kind="events", filters={"q": "beacon"})],
        service=service,
        store=_FakeStore(),
        cap=2,
        run=_run,
    )
    assert [m["at"][:13] for m in result["marks"]] == ["2026-07-20T01", "2026-07-20T02"]
    assert result["sources"][0] == {
        "index": 0,
        "kind": "events",
        "label": "matching events",
        "count": 3,
        "shown": 2,
        "overflow": True,
        "undated": 1,
    }
    assert service.calls[0][1] == 2


@pytest.mark.parametrize(
    ("mark", "needle"),
    [
        (
            {"kind": "baseline", "definition_id": "nope"},
            'marks\\[0\\]: baseline definition "nope" not found',
        ),
        ({"kind": "view", "view_id": "nope"}, 'marks\\[0\\]: saved view "nope" not found'),
    ],
)
async def test_unknown_references_are_refused_by_index(mark, needle):
    with pytest.raises(ValueError, match=needle):
        await resolve_marks(
            _scope(), [_mark(**mark)], service=_FakeService(), store=_FakeStore(), cap=5, run=_run
        )


async def test_a_baseline_marks_label_overrides_the_definitions_name():
    """`label` is accepted on every kind and was discarded on this one (#332).

    The schema documents it as "optional otherwise" and the `view` branch
    honours `mark.label or view.name`; the baseline branch hardcoded the
    definition's name, so an analyst naming the definition "W-3" and the mark
    "the exfil window" got "W-3" on the canvas with no word about it.
    """
    result = await resolve_marks(
        _scope(),
        [_mark(kind="baseline", definition_id="bd1", label="the exfil window")],
        service=_FakeService(),
        store=_FakeStore(),
        cap=50,
        run=_run,
    )
    assert [m["label"] for m in result["marks"]] == [
        "the exfil window — baseline",
        "the exfil window — Exfil day",
    ]
    assert [s["label"] for s in result["sources"]] == ["the exfil window"]
    # Provenance still names the definition, so the label never hides the source.
    assert all(m["provenance"]["definition_id"] == "bd1" for m in result["marks"])


async def test_a_baseline_without_a_label_still_uses_the_definitions_name():
    result = await resolve_marks(
        _scope(),
        [_mark(kind="baseline", definition_id="bd1")],
        service=_FakeService(),
        store=_FakeStore(),
        cap=50,
        run=_run,
    )
    assert [m["label"] for m in result["marks"]] == [
        "Quiet week — baseline",
        "Quiet week — Exfil day",
    ]
