import { beforeAll, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MarksEditor } from "@/components/viz/MarksEditor";
import type { MarkSource } from "@/components/viz/lib/chartConfig";
import { installRadixJsdomStubs } from "./helpers/radix";

vi.mock("@/api/baselines", () => ({
  baselinesApi: {
    list: vi.fn().mockResolvedValue({
      baselines: [{ id: "bd1", name: "Quiet week", baseline: { start: "", end: "" }, suspect_windows: [] }],
    }),
  },
}));
vi.mock("@/api/views", () => ({
  viewsApi: { list: vi.fn().mockResolvedValue([{ id: "v1", name: "Beacons", query: "", filter: {} }]) },
}));
vi.mock("@/api/dispositions", () => ({
  dispositionsApi: {
    list: vi.fn().mockResolvedValue({
      dispositions: [
        { id: "d1", kind: "confirmed", event_id: "e1", source_id: "s1" },
        { id: "d2", kind: "confirmed", event_id: "e2", source_id: "s1" },
      ],
    }),
  },
}));

beforeAll(() => installRadixJsdomStubs());

function renderEditor(marks: MarkSource[] = []) {
  const onChange = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MarksEditor caseId="c1" timelineId="t1" marks={marks} onChange={onChange} fields={[]} />
    </QueryClientProvider>,
  );
  return { onChange };
}

async function pick(entry: string) {
  fireEvent.click(screen.getByRole("combobox", { name: "Add mark" }));
  fireEvent.click(await screen.findByRole("option", { name: entry }));
}

describe("MarksEditor", () => {
  it("adds a typed instant with its label", async () => {
    const { onChange } = renderEditor();
    await pick("Instant");
    fireEvent.change(screen.getByLabelText("At"), { target: { value: "2026-07-20T09:41" } });
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "first beacon" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(onChange).toHaveBeenCalledWith([{ kind: "instant", at: "2026-07-20T09:41:00.000Z", label: "first beacon" }]);
  });

  it("adds a tag as an events mark and an event id as an events mark with ids", async () => {
    const { onChange } = renderEditor();
    await pick("Tag");
    fireEvent.change(screen.getByLabelText("Tag"), { target: { value: "exfil" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(onChange).toHaveBeenLastCalledWith([{ kind: "events", filters: { tagsInclude: ["exfil"] }, label: "tag exfil" }]);
    await pick("Event id");
    fireEvent.change(screen.getByLabelText("Event id"), { target: { value: "e42" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(onChange).toHaveBeenLastCalledWith([{ kind: "events", filters: { ids: ["e42"] }, label: "event e42" }]);
  });

  it("refuses a custom-filter mark whose filter narrows nothing", async () => {
    // A mark is a source resolved at render time, so an empty filter is not
    // an empty mark: it matches every event in the timeline and draws one
    // rule per event, up to the cap (#332).
    const { onChange } = renderEditor();
    await pick("Custom filter");
    const add = screen.getByRole("button", { name: "Add" });
    expect(add).toBeDisabled();
    expect(screen.getByText(/an empty one marks every event/i)).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Search text…"), {
      target: { value: "beacon" },
    });
    expect(screen.getByRole("button", { name: "Add" })).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(onChange).toHaveBeenLastCalledWith([{ kind: "events", filters: { q: "beacon" } }]);
  });

  it("turns confirmed findings into one events mark over their ids", async () => {
    const { onChange } = renderEditor();
    await pick("Confirmed findings");
    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith([{ kind: "events", filters: { ids: ["e1", "e2"] }, label: "confirmed findings" }]),
    );
  });

  it("offers baseline definitions and saved views by name", async () => {
    const { onChange } = renderEditor();
    await pick("Baseline definition");
    fireEvent.click(await screen.findByRole("combobox", { name: "Baseline definition" }));
    fireEvent.click(await screen.findByRole("option", { name: "Quiet week" }));
    expect(onChange).toHaveBeenLastCalledWith([{ kind: "baseline", definitionId: "bd1" }]);
  });

  it("lists marks with their resolved status and removes one", () => {
    const marks: MarkSource[] = [
      { kind: "events", filters: { q: "beacon" }, label: "beacons" },
      { kind: "instant", at: "2026-07-20T09:41:00Z", label: "first" },
    ];
    const onChange = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MarksEditor
          caseId="c1"
          timelineId="t1"
          marks={marks}
          onChange={onChange}
          fields={[]}
          resolved={{
            cap: 50,
            marks: [],
            sources: [
              { index: 0, kind: "events", label: "beacons", count: 80, shown: 50, overflow: true, undated: 2 },
              { index: 1, kind: "instant", label: "first", count: 1, shown: 1, overflow: false, undated: 0 },
            ],
          }}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByText(/50 of 80 drawn/)).toBeTruthy();
    expect(screen.getByText(/2 undated/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove mark beacons" }));
    expect(onChange).toHaveBeenCalledWith([marks[1]]);
  });
});
