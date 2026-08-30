import { beforeAll, describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { MarksOverlay } from "@/components/viz/primitives/MarksOverlay";
import { CompareHistogram } from "@/components/viz/charts/CompareHistogram";
import type { CompareTimeResponse, ResolvedMark } from "@/api/types";
import { installFakeResizeObserver } from "./helpers/resizeObserver";

beforeAll(() => installFakeResizeObserver());

const marks: ResolvedMark[] = [
  { kind: "instant", at: "2026-07-20T02:00:00+00:00", label: "first", source: 0, provenance: { kind: "analyst" } },
  { kind: "instant", at: "2026-07-20T01:00:00+00:00", label: "e1", source: 1, provenance: { kind: "event", event_id: "e1", source_id: "s1" } },
  { kind: "range", start: "2026-07-20T01:00:00+00:00", end: "2026-07-20T03:00:00+00:00", label: "window", source: 2, provenance: { kind: "analyst" } },
];

describe("MarksOverlay", () => {
  it("draws one rule per instant with its number, and one band per range", () => {
    const T0 = Date.parse("2026-07-20T00:00:00Z");
    const { container } = render(
      <svg>
        <MarksOverlay marks={marks} x={(d) => (d.getTime() - T0) / 3_600_000} innerWidth={24} innerHeight={100} />
      </svg>,
    );
    expect(container.querySelectorAll("line[data-mark-instant]")).toHaveLength(2);
    expect([...container.querySelectorAll("text[data-mark-n]")].map((t) => t.textContent)).toEqual(["#1", "#2"]);
    const band = container.querySelector("rect[data-mark-range]")!;
    expect(band.getAttribute("x")).toBe("1");
    expect(band.getAttribute("width")).toBe("2");
    expect(container.querySelector("title")!.textContent).toBe("e1"); // #1 is the earlier instant
  });

  it("renders nothing without marks", () => {
    const { container } = render(
      <svg>
        <MarksOverlay marks={[]} x={() => 0} innerWidth={10} innerHeight={10} />
      </svg>,
    );
    expect(container.querySelector("[data-marks]")).toBeNull();
  });
});

describe("CompareHistogram with marks", () => {
  it("overlays the marks on the time axis", () => {
    const data: CompareTimeResponse = {
      kind: "time",
      interval_seconds: 3600,
      min: "2026-07-20T00:00:00+00:00",
      max: "2026-07-20T03:00:00+00:00",
      primary_total: 3,
      comparison_total: 0,
      buckets: [
        { start: "2026-07-20T00:00:00+00:00", primary: 1, comparison: 0 },
        { start: "2026-07-20T01:00:00+00:00", primary: 1, comparison: 0 },
        { start: "2026-07-20T02:00:00+00:00", primary: 1, comparison: 0 },
        { start: "2026-07-20T03:00:00+00:00", primary: 0, comparison: 0 },
      ],
    };
    const { container } = render(<CompareHistogram data={data} metric="count" hasComparison={false} marks={marks} />);
    expect(container.querySelectorAll("line[data-mark-instant]")).toHaveLength(2);
    expect(container.querySelectorAll("rect[data-mark-range]")).toHaveLength(1);
  });
});
