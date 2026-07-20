"""Tests for the virtual ``time:`` fields (time parts as ordinary field tokens).

Covers the spec table itself, resolution through ``_field_column_expr`` (so
every aggregation and the filter builder inherit one definition), the
bounded-domain pivot axis, and the reservation of the ``time:`` namespace
against field mappings.

The three properties these tests exist to protect — all silent-corruption
failure modes rather than crashes — are documented in
``vestigo.db._time_fields``: UTC-explicit extraction, zero-padded values, and
sentinel rows collapsing to ``''``.
"""

from __future__ import annotations

import pytest

from tests.test_queries import FakeQueryResult, _viz_service
from vestigo.db._dt import VESTIGO_NOT_SENTINEL_SQL
from vestigo.db._offsets import OFFSET_SRC_PARAM, effective_ts_sql
from vestigo.db._time_fields import (
    TIME_FIELD_PREFIX,
    TIME_FIELD_SPECS,
    is_time_field,
    resolve_time_field,
)
from vestigo.db.field_mappings import validate_field_mappings
from vestigo.db.queries import EventQuery, _field_column_expr

BOUNDED = [t for t, s in TIME_FIELD_SPECS.items() if s.domain is not None]
UNBOUNDED = [t for t, s in TIME_FIELD_SPECS.items() if s.domain is None]

#: The ClickHouse function each unbounded token groups by, for dispatching
#: canned results in the pivot tests below where two terms scans coexist.
_UNBOUNDED_SQL_MARKER = {"time:date": "toDate(", "time:year_month": "formatDateTime("}


# ── the spec table ───────────────────────────────────────────────────────────


def test_every_token_lives_under_the_reserved_prefix() -> None:
    assert TIME_FIELD_SPECS
    assert all(t.startswith(TIME_FIELD_PREFIX) for t in TIME_FIELD_SPECS)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("time:hour_of_day", tuple(f"{h:02d}" for h in range(24))),
        ("time:day_of_week", ("1", "2", "3", "4", "5", "6", "7")),
        ("time:month", tuple(f"{m:02d}" for m in range(1, 13))),
        ("time:day_of_month", tuple(f"{d:02d}" for d in range(1, 32))),
        ("time:week_of_year", tuple(f"{w:02d}" for w in range(1, 54))),
    ],
)
def test_bounded_domains_are_complete_and_in_natural_order(
    token: str, expected: tuple[str, ...]
) -> None:
    assert TIME_FIELD_SPECS[token].domain == expected


@pytest.mark.parametrize("token", BOUNDED)
def test_domain_values_sort_lexically_into_chronological_order(token: str) -> None:
    """Zero-padding is what makes ``sort: "value"`` order an hour axis right.

    Without it ``'9'`` sorts after ``'10'`` and every temporal chart's axis
    scrambles — silently, since the values are all still present.
    """
    domain = TIME_FIELD_SPECS[token].domain
    assert domain is not None
    assert sorted(domain) == list(domain)


@pytest.mark.parametrize("token", BOUNDED)
def test_display_labels_when_present_cover_the_whole_domain(token: str) -> None:
    spec = TIME_FIELD_SPECS[token]
    if spec.display is None:
        return
    assert spec.domain is not None
    assert set(spec.display) == set(spec.domain)


def test_opaque_domains_carry_display_labels() -> None:
    """``'1'`` and ``'01'`` mean nothing on an axis; hours read fine alone."""
    assert TIME_FIELD_SPECS["time:day_of_week"].display["1"] == "Mon"
    assert TIME_FIELD_SPECS["time:day_of_week"].display["7"] == "Sun"
    assert TIME_FIELD_SPECS["time:month"].display["01"] == "Jan"
    assert TIME_FIELD_SPECS["time:month"].display["12"] == "Dec"


# ── resolution ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("token", ["time:hour_of_day", " TIME:Hour_Of_Day ", "Time:hour_of_day"])
def test_resolution_is_whitespace_and_case_insensitive(token: str) -> None:
    assert resolve_time_field(token) is TIME_FIELD_SPECS["time:hour_of_day"]


def test_attr_prefix_never_resolves_as_a_time_field() -> None:
    """``attr:`` is the escape hatch to a raw key — it outranks every alias."""
    assert resolve_time_field("attr:time:hour_of_day") is None


@pytest.mark.parametrize("token", ["artifact", "attr:src_ip", "time:nope", ""])
def test_non_time_tokens_do_not_resolve(token: str) -> None:
    assert resolve_time_field(token) is None
    assert not is_time_field(token)


# ── SQL generation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("token", list(TIME_FIELD_SPECS))
def test_sql_is_utc_explicit_and_blanks_sentinel_rows(token: str) -> None:
    sql = TIME_FIELD_SPECS[token].sql("timestamp")
    assert "'UTC'" in sql
    # Undated events carry the year-2299 sentinel; without this guard a time
    # expression over one yields a real-looking hour and every temporal chart
    # grows a phantom bucket that no `col != ''` check catches.
    assert sql.startswith(f"if({VESTIGO_NOT_SENTINEL_SQL},")
    assert sql.endswith(", '')")


def test_day_of_week_uses_the_iso_mode_the_punchcard_uses() -> None:
    """Mode 0 = Mon..Sun. Diverging would make the punch card and a
    ``time:day_of_week`` chart disagree about what "day 1" means."""
    assert "toDayOfWeek(timestamp, 0, 'UTC')" in TIME_FIELD_SPECS["time:day_of_week"].sql(
        "timestamp"
    )


def test_field_column_expr_resolves_a_time_token() -> None:
    params: dict[str, object] = {}
    expr = _field_column_expr("time:hour_of_day", params, "field_key")
    assert "toHour(timestamp, 'UTC')" in expr
    assert "leftPad" in expr
    # Purely expression-derived: nothing to bind, unlike an attribute lookup.
    assert params == {}


def test_field_column_expr_buckets_the_offset_corrected_timestamp() -> None:
    """A source with declared clock skew must bucket by its *corrected* hour,
    like every other time-derived predicate (W2)."""
    offsets = {"s1": 3600}
    expr = _field_column_expr("time:hour_of_day", {}, "field_key", source_offsets=offsets)
    assert effective_ts_sql(offsets) in expr
    assert "addSeconds" in expr


def test_field_column_expr_ignores_offsets_for_ordinary_fields() -> None:
    assert (
        _field_column_expr("artifact", {}, "field_key", source_offsets={"s1": 3600}) == "artifact"
    )


# ── the filter builder inherits the same definition ──────────────────────────


def test_time_field_is_filterable_and_binds_the_offset_arrays() -> None:
    """ "Only hours 02-05" is a real forensic filter, and it comes free from
    resolving at ``_field_column_expr`` rather than per-aggregation."""
    svc = _viz_service([("GROUP BY val", FakeQueryResult(result_rows=[]))])
    svc.field_terms(
        EventQuery(
            case_id="c1",
            source_ids=["s1"],
            field_filters={"time:hour_of_day": ["02", "03"]},
            source_offsets={"s1": 3600},
        ),
        "artifact",
    )
    sql, params = svc.store.client.queries[-1]  # type: ignore[union-attr]
    assert "toHour(" in sql
    assert params is not None
    # effective_ts_sql binds nothing itself — the builder must have bound the
    # arrays its expression references, or ClickHouse errors on an unknown param.
    assert OFFSET_SRC_PARAM in params
    assert ["02", "03"] in params.values()


# ── pivot: bounded time axes take their domain verbatim ──────────────────────


def test_pivot_with_a_bounded_time_axis_uses_the_full_ordered_domain() -> None:
    """An hour with no events is a finding, not a value to hide — so the axis
    is the whole domain in natural order, not a count-ranked top-N."""
    svc = _viz_service(
        [
            # Only ONE terms scan runs: the country axis. The hour axis needs
            # no scan because its values are known in advance.
            (
                "GROUP BY val",
                FakeQueryResult(result_rows=[["NL", 90, 100, 12], ["US", 10, 100, 12]]),
            ),
            (
                "GROUP BY xv, yv",
                FakeQueryResult(result_rows=[["NL", "02", 40], ["US", "23", 5]]),
            ),
        ]
    )
    result = svc.field_pivot(
        EventQuery(case_id="c1", source_ids=["s1"]),
        "attr:geo_country",
        "time:hour_of_day",
        limit_x=5,
        limit_y=5,
    )
    assert result["y_values"] == [f"{h:02d}" for h in range(24)]
    # `limit_y=5` must not truncate a complete domain.
    assert len(result["y_values"]) == 24
    # Nothing was cut off, so reporting the domain size as `distinct` is honest.
    assert result["y_distinct"] == 24
    assert result["x_values"] == ["NL", "US"]

    terms_scans = [q for q, _ in svc.store.client.queries if "GROUP BY val" in q]  # type: ignore[union-attr]
    assert len(terms_scans) == 1


def test_pivot_with_a_bounded_time_axis_folds_nothing_to_other() -> None:
    svc = _viz_service(
        [
            ("GROUP BY val", FakeQueryResult(result_rows=[["NL", 90, 100, 12]])),
            ("GROUP BY xv, yv", FakeQueryResult(result_rows=[["NL", "02", 40]])),
        ]
    )
    svc.field_pivot(
        EventQuery(case_id="c1", source_ids=["s1"]), "attr:geo_country", "time:hour_of_day"
    )
    _, params = next(
        (q, p)
        for q, p in svc.store.client.queries
        if "GROUP BY xv, yv" in q  # type: ignore[union-attr]
    )
    assert params is not None
    assert params["pivot_y_values"] == [f"{h:02d}" for h in range(24)]


@pytest.mark.parametrize("token", UNBOUNDED)
def test_pivot_with_an_unbounded_time_axis_keeps_the_top_n_path(token: str) -> None:
    """``time:date`` has no closed domain, so it ranks by count like any other
    high-cardinality field — including the truthful '' Other rollup."""
    # Both terms scans say "GROUP BY val", so dispatch on the ClickHouse
    # function each one groups by — they also run in parallel, so arrival
    # order can't be relied on.
    svc = _viz_service(
        [
            (
                _UNBOUNDED_SQL_MARKER[token],
                FakeQueryResult(result_rows=[["2026-07-20", 50, 100, 30]]),
            ),
            ("GROUP BY val", FakeQueryResult(result_rows=[["NL", 90, 100, 12]])),
            ("GROUP BY xv, yv", FakeQueryResult(result_rows=[["NL", "2026-07-20", 40]])),
        ]
    )
    result = svc.field_pivot(
        EventQuery(case_id="c1", source_ids=["s1"]), "attr:geo_country", token, limit_y=1
    )
    assert result["y_values"] == ["2026-07-20"]
    assert result["y_distinct"] == 30
    terms_scans = [q for q, _ in svc.store.client.queries if "GROUP BY val" in q]  # type: ignore[union-attr]
    assert len(terms_scans) == 2


def test_pivot_of_two_bounded_time_axes_runs_no_terms_scan() -> None:
    """The punch card's day×hour shape, reachable as an ordinary pivot."""
    svc = _viz_service([("GROUP BY xv, yv", FakeQueryResult(result_rows=[["1", "02", 7]]))])
    result = svc.field_pivot(
        EventQuery(case_id="c1", source_ids=["s1"]), "time:day_of_week", "time:hour_of_day"
    )
    assert result["x_values"] == ["1", "2", "3", "4", "5", "6", "7"]
    assert result["y_values"] == [f"{h:02d}" for h in range(24)]
    assert not [q for q, _ in svc.store.client.queries if "GROUP BY val" in q]  # type: ignore[union-attr]


# ── the namespace is reserved ────────────────────────────────────────────────


def test_mapping_may_not_claim_a_time_prefixed_canonical_name() -> None:
    """Time fields resolve ahead of mappings, so such a name would be
    unreachable rather than merely ambiguous — reject it at definition time."""
    problems = validate_field_mappings({"time:hour_of_day": ["hour"]}, {"hour"})
    assert any(TIME_FIELD_PREFIX in p for p in problems)


def test_mapping_reservation_covers_the_whole_namespace() -> None:
    """Not just today's tokens — adding a time field later must not silently
    shadow a mapping someone defined in the meantime."""
    problems = validate_field_mappings({"time:quarter": ["q"]}, {"q"})
    assert any(TIME_FIELD_PREFIX in p for p in problems)


def test_ordinary_canonical_names_still_validate() -> None:
    assert validate_field_mappings({"src_ip": ["source_ip"]}, {"source_ip"}) == []
