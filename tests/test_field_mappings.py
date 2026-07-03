"""Unit tests for timeline field mappings (issue #10): expression building,
validation, discovery rewriting, and SQL integration via the query builder."""

from __future__ import annotations

from tracesignal.db.anomaly_stats import _col_expr
from tracesignal.db.field_mappings import (
    apply_mappings_to_attribute_keys,
    mapping_coalesce_expr,
    resolve_mapping,
    validate_field_mappings,
)

MAPPINGS = {"ip_address": ["src_ip", "ip_addr"], "user_name": ["user", "username"]}


# ── mapping_coalesce_expr ─────────────────────────────────────────────────────


def test_coalesce_expr_binds_keys_in_precedence_order():
    params: dict = {}
    expr = mapping_coalesce_expr(["src_ip", "ip_addr"], params, "fk")
    assert expr == (
        "coalesce(nullif(attributes[{fk_m0:String}], ''), "
        "nullif(attributes[{fk_m1:String}], ''), '')"
    )
    assert params == {"fk_m0": "src_ip", "fk_m1": "ip_addr"}


def test_coalesce_expr_with_callable_param_minting():
    params: dict = {}
    counter = iter(range(10))
    expr = mapping_coalesce_expr(["a", "b"], params, lambda: f"p{next(counter)}")
    assert "attributes[{p0:String}]" in expr
    assert "attributes[{p1:String}]" in expr
    assert params == {"p0": "a", "p1": "b"}


# ── resolve_mapping ───────────────────────────────────────────────────────────


def test_resolve_mapping_hits_canonical_and_misses_others():
    assert resolve_mapping("ip_address", MAPPINGS) == ["src_ip", "ip_addr"]
    assert resolve_mapping("message", MAPPINGS) is None
    assert resolve_mapping("ip_address", None) is None


def test_attr_prefix_bypasses_mapping():
    # attr: always addresses the raw key — the analyst's escape hatch.
    assert resolve_mapping("attr:ip_address", MAPPINGS) is None


# ── validate_field_mappings ───────────────────────────────────────────────────

AVAILABLE = {"src_ip", "ip_addr", "user", "username", "status"}


def test_valid_mapping_passes():
    assert validate_field_mappings(MAPPINGS, AVAILABLE) == []


def test_core_column_collision_rejected():
    problems = validate_field_mappings({"message": ["src_ip"]}, AVAILABLE)
    assert any("core event column" in p for p in problems)


def test_existing_raw_key_collision_rejected():
    problems = validate_field_mappings({"status": ["src_ip"]}, AVAILABLE)
    assert any("existing raw attribute key" in p for p in problems)


def test_duplicate_raw_key_across_mappings_rejected():
    problems = validate_field_mappings(
        {"ip_a": ["src_ip"], "ip_b": ["src_ip"]}, AVAILABLE
    )
    assert any("mapped only once" in p for p in problems)


def test_nonexistent_raw_key_rejected():
    problems = validate_field_mappings({"ip": ["no_such_field"]}, AVAILABLE)
    assert any("does not exist" in p for p in problems)


def test_empty_raw_list_and_empty_name_rejected():
    assert validate_field_mappings({"ip": []}, AVAILABLE)
    assert validate_field_mappings({" ": ["src_ip"]}, AVAILABLE)
    assert validate_field_mappings({"attr:ip": ["src_ip"]}, AVAILABLE)


# ── apply_mappings_to_attribute_keys ──────────────────────────────────────────


def test_discovery_hides_raws_and_surfaces_canonical():
    keys, provenance = apply_mappings_to_attribute_keys(
        ["ip_addr", "src_ip", "status", "user", "username"], MAPPINGS
    )
    assert keys == ["ip_address", "status", "user_name"]
    assert {p["name"] for p in provenance} == {"ip_address", "user_name"}
    prov = {p["name"]: p["raw_fields"] for p in provenance}
    assert prov["ip_address"] == ["src_ip", "ip_addr"]


def test_discovery_ignores_mappings_with_no_present_raws():
    keys, provenance = apply_mappings_to_attribute_keys(["status"], MAPPINGS)
    assert keys == ["status"]
    assert provenance == []


def test_discovery_noop_without_mappings():
    keys, provenance = apply_mappings_to_attribute_keys(["a", "b"], None)
    assert keys == ["a", "b"]
    assert provenance == []


# ── anomaly_stats._col_expr integration ──────────────────────────────────────


def test_anomaly_col_expr_resolves_canonical_to_coalesce():
    params: dict = {}
    expr = _col_expr("ip_address", params, MAPPINGS)
    assert expr.startswith("coalesce(")
    assert params == {"fk_m0": "src_ip", "fk_m1": "ip_addr"}


def test_anomaly_col_expr_without_mappings_unchanged():
    params: dict = {}
    assert _col_expr("artifact", params, None) == "artifact"
    assert _col_expr("attr:src_ip", params, MAPPINGS) == "attributes[{fk:String}]"
    assert params == {"fk": "src_ip"}


# ── Query-builder SQL integration ────────────────────────────────────────────


def _service_with_fake_store():
    from tests.test_queries import FakeClickHouseStore
    from tracesignal.db.queries import EventQueryService

    store = FakeClickHouseStore()
    return EventQueryService(store=store), store


def test_field_filter_on_canonical_field_generates_coalesce_sql():
    from tracesignal.db.queries import EventQuery

    service, store = _service_with_fake_store()
    service.query(
        EventQuery(
            case_id="c1",
            source_ids=["s1", "s2"],
            field_filters={"ip_address": "10.0.0.1"},
            field_mappings=MAPPINGS,
        )
    )
    sql, params = store.client.queries[0]
    assert "coalesce(nullif(attributes[" in sql
    assert "10.0.0.1" in params.values()
    assert "src_ip" in params.values() and "ip_addr" in params.values()


def test_field_exclusion_and_raw_attr_filter_bypass():
    from tracesignal.db.queries import EventQuery

    service, store = _service_with_fake_store()
    service.query(
        EventQuery(
            case_id="c1",
            source_ids=["s1"],
            field_filters={"attr:src_ip": "10.0.0.1"},
            field_exclusions={"ip_address": ["10.9.9.9"]},
            field_mappings=MAPPINGS,
        )
    )
    sql, params = store.client.queries[0]
    # Exclusion on the canonical field coalesces; attr: filter stays raw.
    assert "coalesce(nullif(attributes[" in sql
    assert "src_ip" in params.values()


def test_field_terms_groups_by_coalesced_canonical():
    from tracesignal.db.queries import EventQuery

    service, store = _service_with_fake_store()
    service.field_terms(
        EventQuery(case_id="c1", source_ids=["s1"], field_mappings=MAPPINGS),
        "ip_address",
    )
    sql, params = store.client.queries[0]
    assert "coalesce(nullif(attributes[{field_key_m0:String}]" in sql
    assert params["field_key_m0"] == "src_ip"
    assert params["field_key_m1"] == "ip_addr"


def test_list_fields_rewrites_inventory_with_provenance():
    from tests.test_queries import FakeQueryResult

    service, store = _service_with_fake_store()
    store.client.event_rows = []

    def fake_query(query, parameters=None, **_kw):
        return FakeQueryResult(result_rows=[[["src_ip", "ip_addr", "status"]]])

    store.client.query = fake_query
    result = service.list_fields("c1", ["s1"], MAPPINGS)
    assert result["attributes"] == ["ip_address", "status"]
    assert result["mapped"] == [{"name": "ip_address", "raw_fields": ["src_ip", "ip_addr"]}]
