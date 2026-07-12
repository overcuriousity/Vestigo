/**
 * DispositionIndicator (X3): the event grid's triage marker for event-scoped
 * disposition rows — kind priority (confirmed > dismissed > normal), tooltip
 * content, and the stable-layout placeholder when no verdict exists.
 */
import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { DispositionIndicator } from "@/components/explorer/EventGrid";
import { TooltipProvider } from "@/components/ui/Tooltip";
import type { Disposition, DispositionKind } from "@/api/types";

// jsdom has no ResizeObserver; Radix's tooltip positioning needs one (same
// stub pattern as vizCharts.smoke.test.tsx).
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
// @ts-expect-error -- jsdom has no native ResizeObserver
global.ResizeObserver = FakeResizeObserver;

function disposition(kind: DispositionKind, over: Partial<Disposition> = {}): Disposition {
  return {
    id: `d-${kind}`,
    case_id: "c1",
    timeline_id: null,
    kind,
    detector: "*",
    field: null,
    value: null,
    source_id: "s1",
    event_id: "evt-1",
    note: null,
    details: null,
    created_by: null,
    created_at: null,
    ...over,
  };
}

function renderIndicator(dispositions: Disposition[]) {
  return render(
    <TooltipProvider>
      <DispositionIndicator dispositions={dispositions} />
    </TooltipProvider>,
  );
}

describe("DispositionIndicator", () => {
  it("renders nothing but a layout placeholder without dispositions", () => {
    const { container } = renderIndicator([]);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("shows an icon for a single verdict", () => {
    const { container } = renderIndicator([disposition("normal")]);
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("prioritizes confirmed over dismissed over normal", async () => {
    const { container } = renderIndicator([
      disposition("normal"),
      disposition("confirmed", { detector: "value_novelty" }),
      disposition("dismissed"),
    ]);
    const icon = container.querySelector("span[style]");
    expect(icon).not.toBeNull();
    fireEvent.focus(icon!);
    // Tooltip lists every verdict, led by the confirmed one's label.
    const tip = await screen.findAllByText(/Confirmed \(value_novelty\)/);
    expect(tip.length).toBeGreaterThan(0);
  });

  it("includes detector and note in the tooltip", async () => {
    const { container } = renderIndicator([
      disposition("dismissed", { detector: "entropy", note: "scanner noise" }),
    ]);
    fireEvent.focus(container.querySelector("span[style]")!);
    const tip = await screen.findAllByText(/Dismissed \(entropy\) — scanner noise/);
    expect(tip.length).toBeGreaterThan(0);
  });
});
