/**
 * BarChart `sort="value"` orders by the *canonical* value, not the display
 * label.
 *
 * This is what makes the zero-padding in `src/vestigo/db/_time_fields.py`
 * pay off: time parts are emitted as "02", not "2", precisely so lexical
 * order equals chronological order. Once a value carries a display label
 * ("1" renders as "Mon"), sorting on the label reorders the axis into
 * alphabetical nonsense while every key, filter and colour stays on the
 * canonical value — a silent, plausible-looking wrong answer.
 */
import { beforeAll, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { BarChart } from "@/components/viz/charts/BarChart";
import { installFakeResizeObserver } from "./helpers/resizeObserver";
import type { FieldTermsResponse } from "@/api/types";

beforeAll(() => installFakeResizeObserver());

const terms = (
  field: string,
  values: [string, number][],
  otherCount = 0,
): FieldTermsResponse => ({
  field,
  values: values.map(([value, count]) => ({ value, count })),
  total: values.reduce((s, [, c]) => s + c, 0) + otherCount,
  distinct: values.length + (otherCount > 0 ? 1 : 0),
  other_count: otherCount,
});

/** Rendered text nodes, in document order. */
const textsInOrder = (container: HTMLElement): string[] =>
  Array.from(container.querySelectorAll("text")).map((t) => t.textContent ?? "");

describe("BarChart sort='value' with a labelled time field", () => {
  it("orders weekdays chronologically, not alphabetically by label", () => {
    // Server order is count-descending and chronologically shuffled. Weekdays
    // are the deliberate fixture: sorting on the label yields
    // Mon, Sun, Tue, Wed — visibly different from the correct chronological
    // Mon, Tue, Wed, Sun. A fixture where both orders agree proves nothing.
    const { container } = render(
      <BarChart
        terms={terms("time:day_of_week", [
          ["3", 90],
          ["1", 50],
          ["7", 10],
          ["2", 30],
        ])}
        sort="value"
      />,
    );
    const days = textsInOrder(container).filter((t) =>
      ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].includes(t),
    );
    expect(days).toEqual(["Mon", "Tue", "Wed", "Sun"]);
  });

  it("orders zero-padded hours by their canonical value", () => {
    const { container } = render(
      <BarChart
        terms={terms("time:hour_of_day", [
          ["09", 5],
          ["10", 99],
          ["02", 40],
        ])}
        sort="value"
      />,
    );
    const hours = textsInOrder(container).filter((t) => /^\d{2}:00$/.test(t));
    expect(hours).toEqual(["02:00", "09:00", "10:00"]);
  });

  it("keeps the Other bucket last regardless of how its label sorts", () => {
    const { container } = render(
      <BarChart
        terms={terms(
          "time:day_of_week",
          [
            ["7", 10],
            ["1", 50],
          ],
          25,
        )}
        sort="value"
      />,
    );
    const labels = textsInOrder(container).filter((t) =>
      ["Mon", "Sun", "Other"].includes(t),
    );
    expect(labels).toEqual(["Mon", "Sun", "Other"]);
  });

  it("reports the canonical value on click, not the label it renders", () => {
    // The whole reason sorting must not move to labels: the click payload
    // feeds a filter, and only the canonical value round-trips.
    const onValueClick = vi.fn();
    render(
      <BarChart
        terms={terms("time:day_of_week", [
          ["3", 90],
          ["1", 50],
        ])}
        sort="value"
        onValueClick={onValueClick}
      />,
    );
    fireEvent.click(screen.getByText("Mon"));
    expect(onValueClick).toHaveBeenCalledTimes(1);
    expect(onValueClick.mock.calls[0][0].entries).toEqual([["time:day_of_week", "1"]]);
  });
});

describe("BarChart sort='value' with a derived value order", () => {
  it("with valueOrder, sort=value follows that order rather than the alphabet", () => {
    const order = ["< 1,024", "1,024 – 10,240", "≥ 10,240"];
    const { container } = render(
      <BarChart
        terms={terms("attr:bytes", [
          ["≥ 10,240", 1],
          ["< 1,024", 3],
          ["1,024 – 10,240", 2],
        ])}
        sort="value"
        valueOrder={order}
      />,
    );
    expect(textsInOrder(container).filter((t) => order.includes(t))).toEqual(order);
  });

  it("keeps Other last and unknown labels after the known order", () => {
    const order = ["< 1,024", "≥ 1,024"];
    const { container } = render(
      <BarChart
        terms={terms("attr:bytes", [
          ["stray", 9],
          ["≥ 1,024", 1],
          ["< 1,024", 3],
        ], 4)}
        sort="value"
        valueOrder={order}
      />,
    );
    const shown = textsInOrder(container).filter((t) =>
      [...order, "stray", "Other"].includes(t),
    );
    expect(shown).toEqual(["< 1,024", "≥ 1,024", "stray", "Other"]);
  });
});
