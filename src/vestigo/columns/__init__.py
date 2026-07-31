"""Recommended event-grid columns for a timeline (issue #213).

An analyst opening a timeline for the first time used to land on
``timestamp / artifact / message`` regardless of what the corpus actually
contained — one useful column, one usually redundant with the filters they
already set, and one long string that truncates to nothing. This package
derives the opening columns from the data instead.

Three modules, deliberately layered so the expensive and non-deterministic
parts stay optional:

* :mod:`~vestigo.columns.recommend` — pure scoring over the per-source field
  statistics cache (``db/field_stats.py``). No I/O, no model, fully
  deterministic and unit-testable. This is the floor: it always runs, and its
  answer stands whenever anything above it is unavailable or unusable.
* :mod:`~vestigo.columns.advisor` — one typed LLM call that *reorders and
  selects from* the candidates the scorer already produced. It cannot
  introduce a field the scorer did not surface, and any failure falls back to
  the heuristic ranking.
* :mod:`~vestigo.columns.jobs` — the background job that loads the stats,
  runs the two above, validates, persists to ``Timeline.recommended_columns``
  and records the audit row.

The recommendation is *display metadata*, shared by everyone with access to
the timeline. It never rewrites events, and a per-user column choice in the
browser always outranks it — so the worst case for a bad recommendation is
one click on "Reset to defaults".
"""

from __future__ import annotations

from vestigo.columns.recommend import (
    ColumnCandidate,
    finalize_columns,
    pick_columns,
    score_columns,
)

__all__ = [
    "ColumnCandidate",
    "finalize_columns",
    "pick_columns",
    "score_columns",
]
