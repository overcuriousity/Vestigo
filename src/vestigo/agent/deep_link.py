"""The Visualize deep link — a Python mirror of the page's URL codec.

External MCP clients get no chart card, so ``propose_chart`` hands them the
exact URL the in-app card's "Open in Visualize" opens. Mirrors
``frontend/src/lib/queryParams.ts::filtersToParams`` (the Explorer filter
namespace) followed by ``viz/lib/chartConfig.ts::chartConfigToParams`` (the
``c_*`` namespace), in that order and with those encodings; the fixture
``frontend/src/test/fixtures/viz-deep-link.json`` is asserted from both sides.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from vestigo.agent.chart_meta import CHART_META


def _json(value: Any) -> str:
    # JSON.stringify: no whitespace.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _filter_params(payload: dict[str, Any] | None) -> list[tuple[str, str]]:
    p = payload or {}
    out: list[tuple[str, str]] = []
    if p.get("q"):
        out.append(("q", str(p["q"])))
    if p.get("qMode"):
        out.append(("qMode", str(p["qMode"])))
    if p.get("qRegex"):
        out.append(("qRegex", "1"))
    if p.get("artifact"):
        out.append(("artifact", str(p["artifact"])))
    if p.get("artifacts"):
        out.append(("artifacts", ",".join(p["artifacts"])))
    if p.get("sourceId"):
        out.append(("sourceId", str(p["sourceId"])))
    if p.get("tag"):
        out.append(("tag", str(p["tag"])))
    if p.get("excludeTag"):
        out.append(("excludeTag", str(p["excludeTag"])))
    if p.get("tagsInclude"):
        out.append(("tagsInclude", ",".join(p["tagsInclude"])))
    if p.get("tagsExclude"):
        out.append(("tagsExclude", ",".join(p["tagsExclude"])))
    if p.get("start"):
        out.append(("start", str(p["start"])))
    if p.get("end"):
        out.append(("end", str(p["end"])))
    for key in ("filters", "exclusions", "filterModes", "exclusionModes"):
        if p.get(key):
            out.append((key, _json(p[key])))
    if p.get("annotated"):
        out.append(("annotated", ",".join(p["annotated"])))
    if p.get("annotationTagValue"):
        out.append(("annotationTagValue", str(p["annotationTagValue"])))
    return out


def visualize_url(
    case_id: str,
    timeline_id: str,
    stored_config: dict[str, Any],
    filters_payload: dict[str, Any] | None,
) -> str:
    """Return the Visualize page URL that opens *stored_config* under *filters_payload*."""
    c = stored_config
    params = _filter_params(filters_payload)
    chart_type = str(c["chartType"])
    params.append(("c_type", chart_type))
    # A spec that left `scale` unset stores no key; the page would resolve the
    # registry default, so the link says it explicitly.
    params.append(("c_scale", str(c.get("scale") or CHART_META[chart_type].default_scale)))
    if c.get("field"):
        params.append(("c_field", str(c["field"])))
    if c.get("fieldY"):
        params.append(("c_field_y", str(c["fieldY"])))
    if c.get("fields"):
        params.append(("c_fields", _json(c["fields"])))
    if c.get("metric") and c["metric"] != "count":
        params.append(("c_metric", str(c["metric"])))
    compare = c.get("compare") or {}
    if compare.get("mode") and compare["mode"] != "off":
        params.append(("c_compare", str(compare["mode"])))
        if compare["mode"] == "custom":
            params.append(("c_compare_filters", _json(compare.get("filters") or {})))
    if c.get("options"):
        params.append(("c_opts", _json(c["options"])))
    if c.get("derive"):
        params.append(("c_derive", _json(c["derive"])))
    if c.get("inputs"):
        params.append(("c_inputs", _json(c["inputs"])))
    if c.get("marks"):
        params.append(("c_marks", _json(c["marks"])))
    return f"/cases/{case_id}/timelines/{timeline_id}/visualize?{urlencode(params)}"
