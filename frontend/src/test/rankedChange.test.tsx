import { beforeAll, describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { RankedChange } from "@/components/viz/charts/RankedChange";
import type { ChangeResponse } from "@/api/types";
import { installFakeResizeObserver } from "./helpers/resizeObserver";

beforeAll(() => installFakeResizeObserver());

const data: ChangeResponse = {
  kind: "change",
  field: "attr:user",
  derive: null,
  top_n: 3,
  primary_total: 20,
  comparison_total: 10,
  rows: [
    { value: "alice", primary: 4, comparison: 6, primary_share: 0.2, comparison_share: 0.6, delta_share: -0.4, status: "fell" },
    { value: "bob", primary: 12, comparison: 3, primary_share: 0.6, comparison_share: 0.3, delta_share: 0.3, status: "rose" },
    { value: "dave", primary: 3, comparison: 0, primary_share: 0.15, comparison_share: 0, delta_share: 0.15, status: "new" },
    { value: "carol", primary: 0, comparison: 1, primary_share: 0, comparison_share: 0.1, delta_share: -0.1, status: "vanished" },
  ],
  union_size: 4,
  rows_shown: 4,
  union_cap: 200,
  truncated: false,
  omitted: 0,
};

describe("RankedChange — dumbbell", () => {
  it("draws one row per value with a grey comparison dot, an accent primary dot and a link", () => {
    const { container } = render(<RankedChange data={data} />);
    expect(container.querySelectorAll('circle[data-change-dot="comparison"]')).toHaveLength(4);
    expect(container.querySelectorAll('circle[data-change-dot="primary"]')).toHaveLength(4);
    expect(container.querySelectorAll("line[data-change-link]")).toHaveLength(4);
    const primary = container.querySelector('circle[data-change-dot="primary"]')!;
    const comparison = container.querySelector('circle[data-change-dot="comparison"]')!;
    expect(primary.getAttribute("fill")).not.toBe(comparison.getAttribute("fill"));
  });

  it("encodes share, not count: alice's primary dot sits left of her comparison dot", () => {
    const { container } = render(<RankedChange data={data} />);
    const row = container.querySelector('[data-change-row="alice"]')!;
    const p = Number(row.querySelector('circle[data-change-dot="primary"]')!.getAttribute("cx"));
    const c = Number(row.querySelector('circle[data-change-dot="comparison"]')!.getAttribute("cx"));
    expect(p).toBeLessThan(c);
  });

  it("labels new and vanished in words and the rest in percentage points", () => {
    const { container } = render(<RankedChange data={data} />);
    const statuses = [...container.querySelectorAll("text[data-change-status]")].map(
      (t) => t.textContent,
    );
    expect(statuses).toEqual(["−40.0 pp", "+30.0 pp", "new", "vanished"]);
  });

  it("renders the empty state when neither window has a value", () => {
    const { getByText } = render(
      <RankedChange
        data={{ ...data, rows: [], union_size: 0, rows_shown: 0, primary_total: 0, comparison_total: 0 }}
      />,
    );
    getByText(/no values in either window/i);
  });
});

describe("RankedChange — slope", () => {
  it("draws one slope per value between two columns and labels both ends", () => {
    const { container } = render(<RankedChange data={data} layout="slope" />);
    const slopes = container.querySelectorAll("line[data-change-slope]");
    expect(slopes).toHaveLength(4);
    const bob = container.querySelector('line[data-change-slope][data-value="bob"]')!;
    // Rose: the right end (primary) is higher on the page than the left end (comparison).
    expect(Number(bob.getAttribute("y2"))).toBeLessThan(Number(bob.getAttribute("y1")));
    expect(container.querySelectorAll('text[data-change-label="bob"]')).toHaveLength(2);
    expect(container.querySelectorAll("line[data-change-link]")).toHaveLength(0);
  });
});
