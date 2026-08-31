/**
 * Scenario presets — the definition table (`viz/lib/scenarios.ts`).
 *
 * A scenario is domain knowledge (what a DDoS looks like) expressed entirely
 * as prose plus a list of *roles*; the core still knows nothing about what a
 * field is. These assertions pin the two things that can silently rot: that
 * every scenario emits a config the figure registry calls legal, and that a
 * role's suggestion is only ever a suggestion.
 */
import { describe, it, expect } from "vitest";
import {
  SCENARIOS,
  suggestBindings,
  missingRoles,
  buildScenarioConfig,
  scenarioFilters,
} from "@/components/viz/lib/scenarios";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import { legalQuantity } from "@/components/viz/lib/chartConfig";
import type { VizFieldInfo } from "@/api/types";

function byId(id: string) {
  const s = SCENARIOS.find((x) => x.id === id);
  if (!s) throw new Error(`no scenario ${id}`);
  return s;
}

const FIELDS: VizFieldInfo[] = [
  { token: "artifact", distinct: 12, coverage: 0.99 },
  { token: "attr:src_ip", distinct: 4000, coverage: 0.8 },
  { token: "attr:dst_host", distinct: 40, coverage: 0.7 },
  { token: "attr:bytes_out", distinct: 9000, coverage: 0.6 },
  { token: "attr:url_path", distinct: 3000, coverage: 0.5 },
  { token: "attr:username", distinct: 90, coverage: 0.9 },
  { token: "attr:event_id", distinct: 60, coverage: 0.95 },
];

describe("the scenario table", () => {
  it("ships the six investigations", () => {
    expect(SCENARIOS.map((s) => s.id)).toEqual([
      "ddos-flood",
      "data-exfiltration",
      "sql-injection",
      "rdp-interaction",
      "lateral-movement",
      "off-hours-activity",
    ]);
  });

  it("gives every scenario a question, since that is what the gallery reads", () => {
    for (const s of SCENARIOS) {
      expect(s.question.length, s.id).toBeGreaterThan(20);
      expect(s.label.length, s.id).toBeGreaterThan(0);
    }
  });

  it("emits only configs the figure registry calls legal", () => {
    for (const s of SCENARIOS) {
      const meta = CHART_META[s.chartType];
      expect(meta.scales, `${s.id} scale`).toContain(s.scale);
      for (const key of Object.keys(s.options)) {
        expect(meta.readsOptions, `${s.id} reads ${key}`).toContain(key);
      }
      // Every input the figure requires is covered by a required role.
      const bound = new Set(
        s.roles.filter((r) => r.required).map((r) => r.binds),
      );
      if (meta.inputs.field === "required") expect(bound, s.id).toContain("field");
      if (meta.inputs.secondField === "required")
        expect(bound, s.id).toContain("fieldY");
      // ...and no role binds an input the figure never asks for.
      for (const r of s.roles) {
        if (r.binds === "field") expect(meta.inputs.field, s.id).toBeDefined();
        if (r.binds === "fieldY") expect(meta.inputs.secondField, s.id).toBeDefined();
      }
    }
  });

  it("only carries a filter on scenarios whose roles can key it", () => {
    for (const s of SCENARIOS) {
      if (!s.filter) continue;
      const bindings = Object.fromEntries(s.roles.map((r) => [r.key, `f:${r.key}`]));
      const filters = s.filter.build(bindings);
      const keys = Object.keys(filters.filters ?? {});
      expect(keys.length, `${s.id} filter keys`).toBeGreaterThan(0);
      for (const k of keys) expect(Object.values(bindings), s.id).toContain(k);
    }
  });

  it("keeps exfiltration's running sum legal for its own scale and field", () => {
    const s = byId("data-exfiltration");
    expect(legalQuantity(s.options.quantity, s.scale, "attr:bytes_out")).toBe("sum");
  });
});

describe("suggestBindings", () => {
  it("pre-fills a role from the timeline's own field tokens", () => {
    const b = suggestBindings(byId("lateral-movement"), FIELDS);
    expect(b.account).toBe("attr:username");
    expect(b.target).toBe("attr:dst_host");
  });

  it("leaves a role unbound rather than guessing when nothing matches", () => {
    const b = suggestBindings(byId("data-exfiltration"), [
      { token: "artifact", distinct: 12, coverage: 0.99 },
    ]);
    expect(b.bytes).toBeUndefined();
  });

  it("reports the unbound required roles so the modal can name them", () => {
    const s = byId("data-exfiltration");
    expect(missingRoles(s, {}).map((r) => r.key)).toEqual(["bytes"]);
    expect(missingRoles(s, { bytes: "attr:bytes_out" })).toEqual([]);
  });

  it("needs no binding at all for a field-free scenario", () => {
    const s = byId("off-hours-activity");
    expect(missingRoles(s, {})).toEqual([]);
  });
});

describe("buildScenarioConfig", () => {
  it("puts each bound role on the input its role names", () => {
    const s = byId("lateral-movement");
    const patch = buildScenarioConfig(s, {
      account: "attr:username",
      target: "attr:dst_host",
    });
    expect(patch.field).toBe("attr:username");
    expect(patch.fieldY).toBe("attr:dst_host");
    expect(patch.chartType).toBe("sankey");
    expect(patch.scale).toBe("nominal");
  });

  it("clears the state a previous figure left behind", () => {
    const patch = buildScenarioConfig(byId("off-hours-activity"), {});
    expect(patch.field).toBeNull();
    expect(patch.fieldY).toBeNull();
    expect(patch.fields).toBeNull();
    expect(patch.derive).toBeNull();
    expect(patch.marks).toEqual([]);
    expect(patch.inputs).toEqual({});
    expect(patch.compare).toEqual({ mode: "off" });
  });

  it("does not put a filter-only role on the chart's field", () => {
    const patch = buildScenarioConfig(byId("rdp-interaction"), {
      account: "attr:username",
      eventId: "attr:event_id",
    });
    expect(patch.field).toBe("attr:username");
    expect(patch.fieldY).toBeNull();
  });
});

describe("scenarioFilters", () => {
  it("keys the suggested filter on the field the analyst actually bound", () => {
    const filters = scenarioFilters(byId("sql-injection"), { target: "attr:url_path" });
    expect(Object.keys(filters?.filters ?? {})).toEqual(["attr:url_path"]);
    expect(filters?.filterModes?.["attr:url_path"]).toBe("wildcard");
  });

  it("matches RDP's event IDs exactly, not as wildcards", () => {
    const filters = scenarioFilters(byId("rdp-interaction"), {
      account: "attr:username",
      eventId: "attr:event_id",
    });
    expect(filters?.filters?.["attr:event_id"]).toContain("4624");
    expect(filters?.filterModes).toBeUndefined();
  });

  it("is null for a scenario that suggests no filter", () => {
    expect(scenarioFilters(byId("off-hours-activity"), {})).toBeNull();
  });
});
