import { describe, expect, it } from "vitest";
import {
  DEFAULT_FINDINGS_LIMIT,
  FINDINGS_LIMIT_STEPS,
  limitOf,
  pageKeyOf,
  useFindingsLimitStore,
} from "@/stores/findingsLimit";

const scope = { frame: "self", baseline_id: null };

describe("pageKeyOf", () => {
  it("keys a query by its case and timeline, not by method and scope alone", () => {
    const a = pageKeyOf("case-1", "tl-a", "value_novelty", scope, {});
    const b = pageKeyOf("case-1", "tl-b", "value_novelty", scope, {});
    const c = pageKeyOf("case-2", "tl-a", "value_novelty", scope, {});
    expect(a).not.toBe(b);
    expect(a).not.toBe(c);
    expect(pageKeyOf("case-1", "tl-a", "value_novelty", scope, {})).toBe(a);
  });

  it("raising one timeline's page leaves the same method on another at the default", () => {
    const a = pageKeyOf("case-1", "tl-a", "value_novelty", scope, {});
    const b = pageKeyOf("case-1", "tl-b", "value_novelty", scope, {});
    useFindingsLimitStore.getState().raise(a);
    const { byKey } = useFindingsLimitStore.getState();
    expect(limitOf(byKey, a)).toBe(FINDINGS_LIMIT_STEPS[1]);
    expect(limitOf(byKey, b)).toBe(DEFAULT_FINDINGS_LIMIT);
  });
});
