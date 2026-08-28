/**
 * CompareFilterEditor — a filter on a bounded `time:` field must carry the
 * canonical value, never the label the analyst clicked.
 *
 * "1" is Monday everywhere in the data; "Mon" exists only on axes. A free-text
 * box invited typing the label and building a filter that matches nothing —
 * the same class of silent-zero-result bug as the Legend click payload.
 */
import { describe, it, expect, vi, beforeAll } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CompareFilterEditor } from "@/components/viz/CompareFilterEditor";
import { installRadixJsdomStubs } from "./helpers/radix";
import type { EventFilters, VizFieldInfo } from "@/api/types";

beforeAll(() => installRadixJsdomStubs());

const FIELDS: VizFieldInfo[] = [
  { token: "artifact", distinct: 4, coverage: 0.9 },
  { token: "time:day_of_week", distinct: 7, coverage: null, label: "Day of week (UTC)" },
];

function setup(filters: EventFilters = {}) {
  const onChange = vi.fn();
  render(<CompareFilterEditor filters={filters} onChange={onChange} fields={FIELDS} />);
  return onChange;
}

/** Pick a field in the shared `FieldCombo` — index 0 among the comboboxes.
 * Its rows are portaled and commit on mousedown, not click. */
const pickField = async (label: string) => {
  const combo = (await screen.findAllByRole("combobox"))[0];
  fireEvent.focus(combo);
  fireEvent.mouseDown(await screen.findByRole("option", { name: label }));
};

/** Open a Radix Select by index among the rendered comboboxes. */
const openSelect = async (index: number) => {
  const triggers = await screen.findAllByRole("combobox");
  triggers[index].focus();
  fireEvent.keyDown(triggers[index], { key: "ArrowDown" });
  await screen.findByRole("listbox");
};

describe("CompareFilterEditor with a bounded time field", () => {
  it("offers the domain as labelled choices instead of a free-text box", async () => {
    setup();
    await pickField("Day of week (UTC)");

    // The value control is now a Select, not an Input.
    expect(screen.queryByPlaceholderText("value")).toBeNull();
    await openSelect(1);
    expect(screen.getByText("Mon")).toBeInTheDocument();
    expect(screen.getByText("Sun")).toBeInTheDocument();
  });

  it("stores the canonical value behind the label", async () => {
    const onChange = setup();
    await pickField("Day of week (UTC)");
    await openSelect(1);
    fireEvent.click(screen.getByText("Mon"));
    fireEvent.click(screen.getByLabelText("Add field filter"));

    expect(onChange).toHaveBeenCalledTimes(1);
    // "1", not "Mon" — only the canonical value round-trips into a query.
    expect(onChange.mock.calls[0][0].filters).toEqual({ "time:day_of_week": ["1"] });
  });

  it("keeps the free-text box for an ordinary field", async () => {
    const onChange = setup();
    await pickField("artifact");

    const input = screen.getByPlaceholderText("value");
    fireEvent.change(input, { target: { value: "FILE" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange.mock.calls[0][0].filters).toEqual({ artifact: ["FILE"] });
  });

  it("drops a pending value when the field changes", async () => {
    // "1" means Monday for day-of-week and nothing for artifact; carrying it
    // across a field change would build a filter from the wrong vocabulary.
    setup();
    await pickField("Day of week (UTC)");
    await openSelect(1);
    fireEvent.click(screen.getByText("Mon"));

    await pickField("artifact");
    expect(screen.getByPlaceholderText("value")).toHaveValue("");
  });

  it("removes an exclusion chip, which the old hand-rolled remover could not", () => {
    // FilterChips renders exclusions/tags/time chips here too; a remover that
    // only knew `q` and `filters` left those chips inert.
    const onChange = setup({ exclusions: { host: ["srv1"] } });
    const chip = [...document.querySelectorAll("span")].find(
      (el) => el.textContent === "!host=srv1",
    )!;
    fireEvent.click(chip.querySelector("button")!);
    expect(onChange.mock.calls[0][0].exclusions).toEqual({});
  });
});
