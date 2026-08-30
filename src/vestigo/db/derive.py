"""Derivations — a change of scale applied to a field before aggregation.

Exactly two kinds exist, and both yield the ordinal scale:

* ``bins``: a number becomes ordered ranges. ``width`` and ``log`` compute
  ``count`` bins over the slice's actual range (a one-scan pre-flight in
  :meth:`EventQueryService._resolve_derive`); ``custom`` takes the analyst's
  own edges and is open-ended at both ends. ``log`` cannot place ``<= 0``, so
  those values get their own, disclosed bin.
* ``time_part``: a timestamp-valued field becomes an hour / weekday / day /
  week / month, reusing the ``time:`` field specs so a derived hour and a
  ``time:hour_of_day`` chart can never disagree (UTC, zero-padded).

Nothing here knows what an IP address, a URL or a port *is* — that is what
the enrichers are for. Domain-specific parsing is deliberately not a
derivation. ``docs/VISUALIZE.md`` §"Derivations".
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vestigo.db._time_fields import TIME_FIELD_SPECS

#: ``DeriveSpec.part`` → the ``time:`` token whose expression it reuses.
TIME_PART_TOKENS: dict[str, str] = {
    "hour": "time:hour_of_day",
    "weekday": "time:day_of_week",
    "day": "time:day_of_month",
    "week": "time:week_of_year",
    "month": "time:month",
}

BINS_MIN, BINS_MAX = 2, 50


class DeriveSpec(BaseModel):
    """One derivation, as it travels on the wire (snake_case)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["bins", "time_part"]
    mode: Literal["width", "log", "custom"] | None = None
    count: int | None = Field(default=None, ge=BINS_MIN, le=BINS_MAX)
    edges: list[float] | None = None
    part: Literal["hour", "weekday", "day", "week", "month"] | None = None

    @model_validator(mode="after")
    def _shape(self) -> DeriveSpec:
        if self.kind == "bins":
            if self.part is not None:
                raise ValueError("bins takes no `part`")
            if self.mode is None:
                raise ValueError('bins needs `mode`: "width", "log" or "custom"')
            if self.mode == "custom":
                if not self.edges:
                    raise ValueError("custom bins need at least one edge")
                if len(self.edges) >= BINS_MAX:
                    raise ValueError(f"at most {BINS_MAX - 1} custom edges")
                if any(not math.isfinite(e) for e in self.edges):
                    raise ValueError("edges must be finite numbers")
                if any(b <= a for a, b in zip(self.edges, self.edges[1:], strict=False)):
                    raise ValueError("edges must be strictly increasing")
                if self.count is not None:
                    raise ValueError("custom bins take `edges`, not `count`")
            else:
                if self.count is None:
                    raise ValueError(f"{self.mode} bins need `count` ({BINS_MIN}-{BINS_MAX})")
                if self.edges is not None:
                    raise ValueError(f"{self.mode} bins take `count`, not `edges`")
        else:
            if self.part is None:
                raise ValueError("time_part needs `part`: hour, weekday, day, week or month")
            if self.mode is not None or self.count is not None or self.edges is not None:
                raise ValueError("time_part takes only `part`")
        return self


def parse_derive(raw: str | None) -> DeriveSpec | None:
    """Parse the ``derive`` query parameter (a JSON object) or return None."""
    if raw is None or raw == "":
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"derive is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("derive must be a JSON object")
    return DeriveSpec.model_validate(data)


def bin_edges(mode: Literal["width", "log"], count: int, lo: float, hi: float) -> list[float]:
    """The ``count - 1`` interior edges over ``[lo, hi]`` — none when the range is a point."""
    if not (hi > lo):
        return []
    if mode == "width":
        step = (hi - lo) / count
        return [lo + k * step for k in range(1, count)]
    llo, lhi = math.log10(lo), math.log10(hi)
    step = (lhi - llo) / count
    return [10 ** (llo + k * step) for k in range(1, count)]


def _fmt(x: float) -> str:
    """``1024.0`` → ``1,024``; ``0.123456`` → ``0.123`` (three significant digits)."""
    if float(x).is_integer():
        return f"{int(x):,}"
    digits = 3 - int(math.floor(math.log10(abs(x)))) - 1
    rounded = round(x, max(digits, 0))
    if float(rounded).is_integer():
        return f"{int(rounded):,}"
    return f"{rounded:,}"


def _fmt_fixed(x: float, decimals: int) -> str:
    """``x`` at exactly *decimals* places, trailing zeros trimmed: ``4000.25, 3`` → ``4,000.25``."""
    text = f"{round(x, decimals):,.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fmt_edges(edges: list[float]) -> list[str]:
    """Format *edges* so no two read the same.

    Three significant digits is the readable default, but it names ``4000.125``
    and ``4000.875`` both ``4,000`` — and a label is also the ``multiIf``
    literal the rows are grouped by, so two edges with one label are one bin
    in the result and two in the caption. The edges are strictly increasing,
    so some finite precision separates them; take the first that does.
    """
    labels = [_fmt(e) for e in edges]
    decimals = 0
    while len(set(labels)) < len(labels) and decimals <= 15:
        decimals += 1
        labels = [_fmt_fixed(e, decimals) for e in edges]
    return labels


def bin_labels(edges: list[float], *, negative_bin: bool) -> list[str]:
    """Human labels for the bins ``edges`` delimit, in value order."""
    if not edges:
        return (["≤ 0"] if negative_bin else []) + ["all values"]
    texts = _fmt_edges(edges)
    labels = ["≤ 0"] if negative_bin else []
    labels.append(f"< {texts[0]}")
    for a, b in zip(texts, texts[1:], strict=False):
        labels.append(f"{a} – {b}")
    labels.append(f"≥ {texts[-1]}")
    return labels


def _lit(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def label_order_expr(value_expr: str, labels: list[str]) -> str:
    """``indexOf`` mapping a derived label onto its position in value order.

    A bin label is not sortable as a string: ``'<'`` (0x3C) and ``'≥'`` sort
    after every digit, so lexical order puts ``< 1,000`` and ``≤ 0`` *after*
    ``1,000 – 2,000``. Sorting a derived figure "by value" therefore orders by
    the label's rank in :func:`bin_labels`' own ordering, not by the string.
    (``time_part`` labels are zero-padded numbers and already sort correctly;
    this expression leaves their order unchanged.) A label the array does not
    contain yields 0 and sorts first ascending — the callers only ever pass the
    labels the same ``ResolvedDerive`` produced the values with.
    """
    array = "[" + ", ".join(_lit(label) for label in labels) + "]"
    return f"indexOf({array}, {value_expr})"


def bins_expr(value_expr: str, edges: list[float], *, negative_bin: bool) -> str:
    """``multiIf`` mapping the float-cast *value_expr* onto :func:`bin_labels`.

    NULL (unparseable / non-finite) rows map to ``''`` so the callers' existing
    ``!= ''`` guard drops them and the caption can count them.
    """
    labels = bin_labels(edges, negative_bin=negative_bin)
    arms: list[str] = [f"isNull({value_expr}), ''"]
    idx = 0
    if negative_bin:
        arms.append(f"{value_expr} <= 0, {_lit(labels[idx])}")
        idx += 1
    for edge in edges:
        arms.append(f"{value_expr} < {float(edge)!r}, {_lit(labels[idx])}")
        idx += 1
    arms.append(_lit(labels[idx]))
    return "multiIf(" + ", ".join(arms) + ")"


def time_part_expr(base_expr: str, part: str) -> str:
    """The ``time:`` spec's expression over a parsed timestamp attribute.

    ``parseDateTimeBestEffortOrNull`` yields NULL for a value that is not a
    timestamp; the part functions propagate NULL and ``ifNull`` folds it to
    ``''`` — the same "unusable rows are empty" contract as ``bins_expr``.
    A value carrying no zone is read as UTC — the parser would otherwise
    assume the *server's* zone, and the caption promises UTC.
    """
    spec = TIME_FIELD_SPECS[TIME_PART_TOKENS[part]]
    parsed = f"parseDateTimeBestEffortOrNull(toString({base_expr}), 'UTC')"
    return f"ifNull({spec.expr(parsed)}, '')"


@dataclass(frozen=True)
class ResolvedDerive:
    """A derivation bound to concrete edges — what the SQL and the caption share."""

    spec: DeriveSpec
    expr: str
    labels: list[str]
    edges: list[float] | None
    negative_bin: bool

    def echo(self) -> dict[str, Any]:
        """The wire-shaped summary every derived response carries as ``derive``."""
        out: dict[str, Any] = {"kind": self.spec.kind, "labels": list(self.labels)}
        if self.spec.kind == "bins":
            out["mode"] = self.spec.mode
            out["edges"] = list(self.edges or [])
            out["negative_bin"] = self.negative_bin
        else:
            out["part"] = self.spec.part
            out["timezone"] = "UTC"
        return out
