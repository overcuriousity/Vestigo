"""Mark resolution — the one place a mark *source* becomes drawable marks.

A mark source (`ChartMarkSpec`) names events, a baseline definition, a saved
view, or a typed instant/range; a resolved mark is an ``instant`` or a
``range`` with a ``provenance`` block a caption can print and an auditor can
follow back. Three callers share this function — ``POST …/viz/marks`` (the
Visualize page and the agent's card), ``execute_chart_spec`` (the agent, and
through it the Stories export) — so the page, the card and a frozen report
can never disagree about which instant a mark stands for.

Honesty rules, all disclosed in ``sources`` for the caption:

- an ``events``/``view`` source draws the earliest ``cap`` dated events and
  reports ``count`` (dated matches), ``overflow`` and ``undated`` (matches
  with no timestamp, which cannot be placed on a time axis);
- a ``baseline`` source draws its baseline window and every suspect window,
  labeled as declared; nothing is derived from them;
- a typed mark is ``provenance.kind == "analyst"``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any


def _iso(value: datetime | str | None) -> str | None:
    """A datetime as ``…+00:00`` ISO (UTC); a string (already ISO) as is."""
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()
    return str(value)


async def resolve_marks(
    scope: Any,
    marks: Sequence[Any],
    *,
    service: Any,
    store: Any,
    cap: int,
    run: Callable[..., Awaitable[Any]] | None = None,
    validated: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Resolve *marks* (``ChartMarkSpec`` items) under *scope* into drawable marks.

    ``run`` executes a blocking service call (default: the gated threadpool
    runner every agent chart uses); ``validated`` is the caller's filter
    validator (regex / match-mode checks) applied to an ``events`` source's
    filters. Raises ``ValueError`` naming the offending index for an unknown
    baseline definition or saved view.
    """
    from vestigo.agent.chart_exec import run_gated_scan
    from vestigo.agent.tools import FilterSpec, _build_query
    from vestigo.stories.export import _filter_payload_to_spec

    run = run or run_gated_scan
    validated = validated or (lambda f: f or FilterSpec())
    out: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []

    for index, mark in enumerate(marks):
        if mark.kind == "instant":
            out.append(
                {
                    "kind": "instant",
                    "at": _iso(mark.at),
                    "label": mark.label,
                    "source": index,
                    "provenance": {"kind": "analyst"},
                }
            )
            sources.append(_source(index, "instant", mark.label, 1, 1))
        elif mark.kind == "range":
            out.append(
                {
                    "kind": "range",
                    "start": _iso(mark.start),
                    "end": _iso(mark.end),
                    "label": mark.label,
                    "source": index,
                    "provenance": {"kind": "analyst"},
                }
            )
            sources.append(_source(index, "range", mark.label, 1, 1))
        elif mark.kind == "baseline":
            definition = await store.get_baseline_definition(
                scope.case_id, scope.timeline_id, mark.definition_id
            )
            if definition is None:
                raise ValueError(
                    f'marks[{index}]: baseline definition "{mark.definition_id}" not found in '
                    "this timeline. list_baselines names the ones that exist."
                )
            # `label` is optional on every kind but instant/range, and the view
            # branch below honours `mark.label or view.name`. The baseline
            # branch used to hardcode the definition's name, silently dropping
            # a label the schema accepts — so an analyst naming a definition
            # "W-3" and the mark "the exfil window" got "W-3" on the canvas.
            base_label = mark.label or definition.name
            windows = [
                {
                    "kind": "range",
                    "start": _iso(definition.baseline_start),
                    "end": _iso(definition.baseline_end),
                    "label": f"{base_label} — baseline",
                    "source": index,
                    "provenance": {
                        "kind": "baseline",
                        "definition_id": definition.id,
                        "window_id": "baseline",
                    },
                }
            ]
            for window in definition.suspect_windows or []:
                windows.append(
                    {
                        "kind": "range",
                        "start": _iso(window.get("start")),
                        "end": _iso(window.get("end")),
                        "label": f"{base_label} — {window.get('label', '')}",
                        "source": index,
                        "provenance": {
                            "kind": "baseline",
                            "definition_id": definition.id,
                            "window_id": window.get("id"),
                        },
                    }
                )
            out.extend(windows)
            sources.append(_source(index, "baseline", base_label, len(windows), len(windows)))
        else:  # "events" or "view": a filter becomes instants
            if mark.kind == "view":
                view = await store.get_view(scope.case_id, mark.view_id)
                if view is None:
                    raise ValueError(
                        f'marks[{index}]: saved view "{mark.view_id}" not found in this case. '
                        "list_saved_views names the ones that exist."
                    )
                fspec = validated(_filter_payload_to_spec(view.view_filter or {}))
                label = mark.label or view.name
                extra = {"view_id": view.id}
            else:
                fspec = validated(mark.filters)
                label = mark.label or "matching events"
                extra = {}
            query = await _build_query(scope, fspec)
            found = await run(service.mark_instants, query, cap)
            for instant in found["instants"]:
                out.append(
                    {
                        "kind": "instant",
                        "at": instant["at"],
                        "label": label,
                        "source": index,
                        "provenance": {
                            "kind": mark.kind if mark.kind == "view" else "event",
                            **extra,
                            "event_id": instant["event_id"],
                            "source_id": instant["source_id"],
                        },
                    }
                )
            sources.append(
                _source(
                    index,
                    mark.kind,
                    label,
                    found["dated"],
                    len(found["instants"]),
                    overflow=found["overflow"],
                    undated=found["undated"],
                )
            )
    return {"marks": out, "sources": sources, "cap": cap}


def _source(
    index: int,
    kind: str,
    label: str | None,
    count: int,
    shown: int,
    *,
    overflow: bool = False,
    undated: int = 0,
) -> dict[str, Any]:
    return {
        "index": index,
        "kind": kind,
        "label": label,
        "count": count,
        "shown": shown,
        "overflow": overflow,
        "undated": undated,
    }
