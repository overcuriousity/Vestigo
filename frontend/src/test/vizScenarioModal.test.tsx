/**
 * The scenario modal: bind the roles, decide about the suggested filter, and
 * the page renders the chart. The assertions here pin what the analyst is
 * promised — nothing is applied until every required role is bound, and the
 * suggested filter is theirs to drop.
 */
import { describe, it, expect, vi, beforeAll } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { ScenarioModal } from "@/components/viz/ScenarioModal";
import { SCENARIOS } from "@/components/viz/lib/scenarios";
import { installFakeResizeObserver } from "./helpers/resizeObserver";
import { installRadixJsdomStubs } from "./helpers/radix";
import type { VizFieldInfo } from "@/api/types";

beforeAll(() => {
  installFakeResizeObserver();
  installRadixJsdomStubs();
});

const FIELDS: VizFieldInfo[] = [
  { token: "artifact", distinct: 12, coverage: 0.99 },
  { token: "attr:username", distinct: 90, coverage: 0.9 },
  { token: "attr:event_id", distinct: 60, coverage: 0.95 },
  { token: "attr:url_path", distinct: 3000, coverage: 0.5 },
];

function byId(id: string) {
  const s = SCENARIOS.find((x) => x.id === id)!;
  return s;
}

function open(scenarioId: string, fields = FIELDS) {
  const onApply = vi.fn();
  render(
    <TooltipProvider>
      <ScenarioModal
        scenario={byId(scenarioId)}
        fields={fields}
        open
        onOpenChange={vi.fn()}
        onApply={onApply}
      />
    </TooltipProvider>,
  );
  return { onApply };
}

describe("ScenarioModal", () => {
  it("names the scenario's question so the analyst knows what they are about to see", () => {
    open("sql-injection");
    expect(screen.getByText(byId("sql-injection").question)).toBeTruthy();
  });

  it("pre-fills a role it can match and applies it on confirm", () => {
    const { onApply } = open("sql-injection");
    const combo = screen.getByLabelText("Request or message field") as HTMLInputElement;
    expect(combo.value).toBe("attr:url_path");
    fireEvent.click(screen.getByRole("button", { name: /render/i }));
    expect(onApply).toHaveBeenCalledTimes(1);
    const [patch] = onApply.mock.calls[0];
    expect(patch.field).toBe("attr:url_path");
    expect(patch.chartType).toBe("bar");
  });

  it("refuses to render while a required role is unbound, and says which", () => {
    const { onApply } = open("data-exfiltration", [
      { token: "artifact", distinct: 12, coverage: 0.99 },
    ]);
    const button = screen.getByRole("button", { name: /render/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByTestId("scenario-unbound").textContent).toContain(
      "Transferred volume",
    );
    fireEvent.click(button);
    expect(onApply).not.toHaveBeenCalled();
  });

  it("passes the suggested filter when its row stays checked", () => {
    const { onApply } = open("rdp-interaction");
    fireEvent.click(screen.getByRole("button", { name: /render/i }));
    const [, filters] = onApply.mock.calls[0];
    expect(filters.filters["attr:event_id"]).toContain("4624");
  });

  it("passes no filter once the analyst unchecks it", () => {
    const { onApply } = open("rdp-interaction");
    fireEvent.click(screen.getByTestId("scenario-filter-toggle"));
    fireEvent.click(screen.getByRole("button", { name: /render/i }));
    const [, filters] = onApply.mock.calls[0];
    expect(filters).toBeNull();
  });

  it("offers no filter row for a scenario that suggests none", () => {
    open("off-hours-activity");
    expect(screen.queryByTestId("scenario-filter-toggle")).toBeNull();
  });

  it("renders straight away for a scenario that needs no field", () => {
    const { onApply } = open("off-hours-activity");
    fireEvent.click(screen.getByRole("button", { name: /render/i }));
    const [patch] = onApply.mock.calls[0];
    expect(patch.chartType).toBe("punchcard");
    expect(patch.field).toBeNull();
  });
});
