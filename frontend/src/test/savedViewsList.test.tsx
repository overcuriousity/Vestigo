/**
 * The saved-views list is manageable: substring search once there is more
 * than one view, per-view rename, and per-view deletion that reports whether
 * the view was removed or merely hidden because a story still embeds it.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { FilterRail } from "@/components/explorer/FilterRail";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { viewsApi } from "@/api/views";
import type { View } from "@/api/types";

vi.mock("@/api/views", () => ({
  viewsApi: {
    list: vi.fn().mockResolvedValue([]),
    create: vi.fn(),
    rename: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({ deleted: true, view_id: "v1", hidden: false }),
  },
}));

function view(id: string, name: string): View {
  return {
    id,
    case_id: "c1",
    name,
    query: "",
    filter: {},
    created_at: "2026-07-01T00:00:00Z",
  };
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function rail(views: View[]) {
  return (
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <FilterRail
            filters={{}}
            onChange={vi.fn()}
            views={views}
            onApplyView={vi.fn()}
            onSaveView={vi.fn()}
            onSearchSubmit={vi.fn()}
            caseId="c1"
            timelineId="t1"
          />
        </TooltipProvider>
      </QueryClientProvider>
    </MemoryRouter>
  );
}

function renderRail(views: View[]) {
  return render(rail(views));
}

const many = [
  view("v1", "Failed logons"),
  view("v2", "PowerShell"),
  view("v3", "Outbound DNS"),
  view("v4", "Lateral movement"),
  view("v5", "Persistence"),
  view("v6", "Exfil candidates"),
];

beforeEach(() => vi.clearAllMocks());

describe("saved views list", () => {
  it("hides the search box for a single view", () => {
    renderRail(many.slice(0, 1));
    expect(screen.queryByPlaceholderText("Search views")).not.toBeInTheDocument();
  });

  it("shows the search box once there is more than one view", () => {
    renderRail(many);
    expect(screen.getByPlaceholderText("Search views")).toBeInTheDocument();
  });

  it("filters by case-insensitive substring", () => {
    renderRail(many);
    fireEvent.change(screen.getByPlaceholderText("Search views"), {
      target: { value: "powershell" },
    });
    expect(screen.getByText("PowerShell")).toBeInTheDocument();
    expect(screen.queryByText("Failed logons")).not.toBeInTheDocument();
  });

  it("says so when nothing matches", () => {
    renderRail(many);
    fireEvent.change(screen.getByPlaceholderText("Search views"), {
      target: { value: "zzz" },
    });
    expect(screen.getByText("No views match")).toBeInTheDocument();
  });

  it("stops filtering once the list shrinks below the search threshold", () => {
    // Deleting a view can take the list back under the threshold, unmounting
    // the search box. A needle still applying after that would strand the
    // panel on "No views match" with no control left to clear it.
    const { rerender } = renderRail(many);
    fireEvent.change(screen.getByPlaceholderText("Search views"), {
      target: { value: "zzz" },
    });
    expect(screen.getByText("No views match")).toBeInTheDocument();

    rerender(rail(many.slice(0, 1)));
    expect(screen.queryByPlaceholderText("Search views")).not.toBeInTheDocument();
    expect(screen.queryByText("No views match")).not.toBeInTheDocument();
    expect(screen.getByText("Failed logons")).toBeInTheDocument();
  });

  it("deletes a view after confirmation", async () => {
    renderRail(many.slice(0, 2));
    fireEvent.click(screen.getByRole("button", { name: "Delete view Failed logons" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(viewsApi.delete).toHaveBeenCalledWith("c1", "v1"));
  });

  it("does not delete when the confirmation is dismissed", () => {
    renderRail(many.slice(0, 2));
    fireEvent.click(screen.getByRole("button", { name: "Delete view Failed logons" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(viewsApi.delete).not.toHaveBeenCalled();
  });

  it("asks before deleting rather than deleting on the first click", () => {
    renderRail(many.slice(0, 2));
    fireEvent.click(screen.getByRole("button", { name: "Delete view Failed logons" }));
    expect(viewsApi.delete).not.toHaveBeenCalled();
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
  });

  it("renames a view on Enter", async () => {
    renderRail(many.slice(0, 2));
    fireEvent.click(screen.getByRole("button", { name: "Rename view Failed logons" }));
    const input = screen.getByDisplayValue("Failed logons");
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(viewsApi.rename).toHaveBeenCalledWith("c1", "v1", "Renamed"));
    // Unmounting the still-focused input on success fires a native blur;
    // that must not re-trigger a second, duplicate PATCH.
    fireEvent.blur(input);
    await new Promise((r) => setTimeout(r, 0));
    expect(viewsApi.rename).toHaveBeenCalledTimes(1);
  });

  it("cancels a rename on Escape without saving", () => {
    renderRail(many.slice(0, 2));
    fireEvent.click(screen.getByRole("button", { name: "Rename view Failed logons" }));
    const input = screen.getByDisplayValue("Failed logons");
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(viewsApi.rename).not.toHaveBeenCalled();
    expect(screen.getByText("Failed logons")).toBeInTheDocument();
    // Unmounting the still-focused input on cancel fires a native blur;
    // that must not resurrect the edit as a save.
    fireEvent.blur(input);
    expect(viewsApi.rename).not.toHaveBeenCalled();
  });
});
