import { beforeAll, describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { CalendarHeatmap } from "@/components/viz/charts/CalendarHeatmap";
import type { CalendarResponse } from "@/api/types";
import { ChartStaticWidthContext } from "@/components/viz/primitives/chartStaticWidth";
import { installFakeResizeObserver } from "./helpers/resizeObserver";

beforeAll(() => installFakeResizeObserver());

const data: CalendarResponse = {
  kind: "calendar",
  field: null,
  timezone: "UTC",
  start: "2026-07-13", // Monday
  end: "2026-07-22", // Wednesday of the second week
  days: [
    { date: "2026-07-20", count: 2 },
    { date: "2026-07-21", count: 1 },
  ],
  total: 3,
  max_count: 2,
  weeks: 2,
  weeks_total: 2,
  truncated: false,
  dropped: 0,
};

describe("CalendarHeatmap", () => {
  it("draws every covered day, empty days visibly empty", () => {
    const { container } = render(<CalendarHeatmap data={data} />);
    const cells = container.querySelectorAll("rect[data-cal-day]");
    // 2026-07-13 (Mon) through 2026-07-22 (Wed) inclusive — the covered span,
    // not the 14 cells of the two-column grid it is drawn on.
    expect(cells).toHaveLength(10);
    const busy = container.querySelector('rect[data-date="2026-07-20"]')!;
    const quiet = container.querySelector('rect[data-date="2026-07-15"]')!;
    expect(busy.getAttribute("fill")).not.toBe("none");
    expect(quiet.getAttribute("fill")).toBe("none");
    expect(quiet.getAttribute("stroke")).toBeTruthy();
  });

  it("leaves days past the data's end blank rather than drawing them as zero", () => {
    // The grid runs to the Sunday of `end`'s week, so 07-23…07-26 are days the
    // query never covered. Drawn as outlined cells they were indistinguishable
    // from a genuine zero day — the figure asserting "nothing happened" for
    // days it was never asked about (#332).
    const { container } = render(<CalendarHeatmap data={data} />);
    for (const date of ["2026-07-23", "2026-07-24", "2026-07-25", "2026-07-26"]) {
      expect(container.querySelector(`rect[data-date="${date}"]`)).toBeNull();
    }
    // The last covered day is still drawn.
    expect(container.querySelector('rect[data-date="2026-07-22"]')).not.toBeNull();
  });

  it("places weeks in columns and weekdays in rows", () => {
    const { container } = render(<CalendarHeatmap data={data} />);
    const mon1 = container.querySelector('rect[data-date="2026-07-13"]')!;
    const mon2 = container.querySelector('rect[data-date="2026-07-20"]')!;
    const tue2 = container.querySelector('rect[data-date="2026-07-21"]')!;
    expect(Number(mon2.getAttribute("x"))).toBeGreaterThan(Number(mon1.getAttribute("x")));
    expect(mon2.getAttribute("y")).toBe(mon1.getAttribute("y"));
    expect(tue2.getAttribute("x")).toBe(mon2.getAttribute("x"));
    expect(Number(tue2.getAttribute("y"))).toBeGreaterThan(Number(mon2.getAttribute("y")));
  });

  it("fits 53 weeks inside a narrow panel instead of clipping the newest ones", () => {
    // A thumbnail, a Story snapshot or a narrow panel: the columns used to be
    // sized to a 3px floor and overflow the <svg>, which clips — and weeks run
    // left→right, so what disappeared was the most recent days while the
    // caption still claimed 53 weeks.
    const year: CalendarResponse = {
      ...data,
      start: "2025-07-14", // Monday
      end: "2026-07-19",
      days: [],
      weeks: 53,
      weeks_total: 53,
    };
    const { container } = render(
      <ChartStaticWidthContext.Provider value={220}>
        <CalendarHeatmap data={year} />
      </ChartStaticWidthContext.Provider>,
    );
    const cells = [...container.querySelectorAll("rect[data-cal-day]")];
    expect(cells).toHaveLength(53 * 7);
    const svg = container.querySelector("svg")!;
    const inner =
      Number(svg.getAttribute("width")) -
      36 - // the frame's left margin
      8; // …and its right one
    const right = Math.max(
      ...cells.map((c) => Number(c.getAttribute("x")) + Number(c.getAttribute("width"))),
    );
    expect(right).toBeLessThanOrEqual(inner + 0.001);
  });

  it("renders the empty state without dated events", () => {
    const { getByText } = render(
      <CalendarHeatmap
        data={{
          ...data,
          days: [],
          start: null,
          end: null,
          total: 0,
          max_count: 0,
          weeks: 0,
          weeks_total: 0,
        }}
      />,
    );
    getByText(/no dated events/i);
  });
});
