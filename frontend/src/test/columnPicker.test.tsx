/**
 * ColumnPicker derived-key grouping (PR #54 finding #34): enrichment-derived
 * keys collapse under their parent attribute, search auto-expands them, and
 * selection always uses the full raw key.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ColumnPicker } from "@/components/explorer/ColumnPicker";
import { useUiStore } from "@/stores/ui";

vi.mock("@/api/events", () => ({
  eventsApi: {
    fields: vi.fn().mockResolvedValue({
      top_level: ["timestamp", "message"],
      attributes: [
        "dst_ip",
        "src_ip",
        "src_ip:geo_city",
        "src_ip:geo_country",
        "zulu:geo_country",
      ],
      derived_suffixes: ["geo_city", "geo_country"],
      mapped: [],
    }),
  },
}));

async function renderOpenPicker() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ColumnPicker caseId="c1" timelineId="t1" />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: /columns/i }));
  await waitFor(() => expect(screen.getByText("src_ip")).toBeInTheDocument());
}

beforeEach(() => {
  useUiStore.setState({ visibleColumnsByTimeline: {} });
});

describe("ColumnPicker derived-key grouping", () => {
  it("collapses derived keys under their parent attribute", async () => {
    await renderOpenPicker();

    expect(screen.getByText("dst_ip")).toBeInTheDocument();
    expect(screen.getByText("Derived (2)")).toBeInTheDocument();
    // Collapsed by default: children hidden.
    expect(screen.queryByText("geo_city")).not.toBeInTheDocument();
    expect(screen.queryByText("src_ip:geo_country")).not.toBeInTheDocument();
  });

  it("expands on click and labels children by output field", async () => {
    await renderOpenPicker();

    fireEvent.click(screen.getByText("Derived (2)"));
    expect(screen.getByText("geo_city")).toBeInTheDocument();
    expect(screen.getByText("geo_country")).toBeInTheDocument();
  });

  it("puts derived keys without a listed parent into a trailing group", async () => {
    await renderOpenPicker();

    expect(screen.getByText("Derived fields")).toBeInTheDocument();
    expect(screen.getByText("zulu:geo_country")).toBeInTheDocument();
  });

  it("search auto-expands matching derived children", async () => {
    await renderOpenPicker();

    fireEvent.change(screen.getByPlaceholderText("Search fields…"), {
      target: { value: "geo_city" },
    });
    // Child visible without manual expansion; parent row kept visible too.
    expect(screen.getByText("geo_city")).toBeInTheDocument();
    expect(screen.getByText("src_ip")).toBeInTheDocument();
    // Unrelated base attribute filtered out.
    expect(screen.queryByText("dst_ip")).not.toBeInTheDocument();
  });

  it("selecting a derived child stores the full raw key", async () => {
    await renderOpenPicker();

    fireEvent.click(screen.getByText("Derived (2)"));
    const row = screen.getByText("geo_country").closest("label")!;
    fireEvent.click(row.querySelector("input")!);

    expect(useUiStore.getState().visibleColumnsByTimeline["c1/t1"]).toContain(
      "src_ip:geo_country",
    );
  });

  it("does not misclassify a raw vendor key whose colon suffix isn't a registered enricher output", async () => {
    // Regression: splitDerivedKey used to split on any colon, so a raw key
    // like "event_data:AccountName" (Windows Event Log/Sysmon-style) was
    // wrongly treated as enrichment-derived. It must only group under a
    // parent when the suffix is a known enricher output field.
    const { eventsApi } = await import("@/api/events");
    vi.mocked(eventsApi.fields).mockResolvedValueOnce({
      top_level: ["timestamp", "message"],
      attributes: ["event_data", "event_data:AccountName"],
      derived_suffixes: ["geo_city", "geo_country"],
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ColumnPicker caseId="c1" timelineId="t1" />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: /columns/i }));
    await waitFor(() => expect(screen.getByText("event_data")).toBeInTheDocument());

    expect(screen.getByText("event_data:AccountName")).toBeInTheDocument();
    expect(screen.queryByText(/^Derived \(/)).not.toBeInTheDocument();
  });
});
