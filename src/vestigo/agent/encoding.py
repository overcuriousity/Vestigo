"""Compact encoding for tabular tool results (roadmap A13).

Tool results are not a one-off cost: they are stored in the conversation
history and resent on *every* subsequent turn. A list of dicts repeats each
key name once per row, so a 100-row `field_terms` result spends ~1.4k chars
just writing `"value"`/`"count"` over and over.

:func:`columnar` states the keys once and returns rows as positional lists.
Every value passes through unchanged — this is a reshaping, not a
summarisation. Truncation and row caps stay where they are (the ``MAX_*``
budgets in ``agent/tools.py``); mixing the two concerns here would make it
impossible to tell a compact result from a lossy one.

Each result carries its own ``columns`` legend rather than relying on a
convention stated once in the system prompt. Persisted history is replayed
verbatim with no migration step, so a single conversation can legitimately
contain both old dict-shaped results and new columnar ones — every result has
to be readable on its own terms.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def columnar(rows: Iterable[Mapping[str, Any]], columns: list[str]) -> dict[str, Any]:
    """Encode uniform dict rows as ``{"columns": [...], "rows": [[...]]}``.

    Args:
        rows: The dict rows to encode.
        columns: Column order. Keys absent from a row encode as ``None``;
            keys present in a row but not listed here are dropped, so the
            caller decides explicitly what the model sees.

    Returns:
        The columnar payload. An empty input still reports its columns, so
        the model can tell "no rows" from "no such shape".
    """
    return {
        "columns": list(columns),
        "rows": [[row.get(column) for column in columns] for row in rows],
    }


def columnar_auto(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """:func:`columnar` with the column order taken from the rows themselves.

    Uses first-seen key order across all rows, so ragged rows are still
    encoded losslessly. Prefer the explicit form when the caller knows the
    shape — this exists for pass-through rows built elsewhere (store
    ``to_dict()`` output, for instance).
    """
    columns: dict[str, None] = {}
    for row in rows:
        columns.update(dict.fromkeys(row))
    return columnar(rows, list(columns))
