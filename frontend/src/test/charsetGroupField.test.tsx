/**
 * D14 frontend: the charset view's group-by picker sends `group_field`, and a
 * grouped finding names the group it was scored in — including which reference
 * scored it, since a group absent from the baseline window is scored against
 * events outside the suspect windows.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { CharsetNoveltyView } from "@/components/analysis/CharsetNoveltyView";
import { installFakeResizeObserver } from "./helpers/resizeObserver";
import { installRadixJsdomStubs } from "./helpers/radix";

function finding(details: Record<string, unknown>) {
  return {
    type: "charset" as const,
    field: "attr:user",
    value: "adminа",
    novel_chars: ["а"],
    count: 2,
    score: 4.2,
    first_seen: "2026-01-01T00:00:00Z",
    event_id: "evt-1",
    event: null,
    details: { detector: "charset", field: "attr:user", value: "adminа", ...details },
  };
}

const listMock = vi.fn();
const fieldsMock = vi.fn().mockResolvedValue({ fields: [] });

vi.mock("@/api/anomalies", async () => {
  const actual = await vi.importActual<typeof import("@/api/anomalies")>("@/api/anomalies");
  return {
    ...actual,
    anomaliesApi: {
      ...actual.anomaliesApi,
      list: (...args: unknown[]) => listMock(...args),
      fields: (...args: unknown[]) => fieldsMock(...args),
    },
  };
});

beforeAll(() => {
  installFakeResizeObserver();
  installRadixJsdomStubs();
});

function result(results: ReturnType<typeof finding>[] = []) {
  return {
    status: "ok",
    detector: "charset",
    method: "rare-chars",
    results,
    total_findings: results.length,
    dismissed_count: 0,
    baseline_size: 100,
  };
}

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <CharsetNoveltyView caseId="c1" timelineId="t1" onSelectEvent={vi.fn()} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listMock.mockResolvedValue(result());
});

describe("CharsetNoveltyView group-by (D14)", () => {
  it("scopes to the whole scope by default — group_field is not sent", async () => {
    renderView();
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    expect(listMock.mock.calls.at(-1)?.[2]).not.toHaveProperty("group_field");
  });

  it("sends group_field when a grouping field is picked", async () => {
    renderView();
    fireEvent.change(await screen.findByTestId("charset-group-select"), {
      target: { value: "display_name" },
    });
    await waitFor(() =>
      expect(listMock.mock.calls.at(-1)?.[2]).toMatchObject({ group_field: "display_name" }),
    );
  });

  it("names the group a finding was scored in, and flags the fallback reference", async () => {
    listMock.mockResolvedValue(
      result([
        finding({
          group_field: "attr:host",
          group_value: "jump-02",
          group_basis: "outside-suspect-windows",
        }),
      ]),
    );
    renderView();
    expect(await screen.findByText(/jump-02/)).toBeInTheDocument();
    // A group with no baseline window is scored against a different reference,
    // which the row has to say rather than reading like a baseline comparison.
    expect(await screen.findByText(/no baseline/)).toBeInTheDocument();
  });

  it("names the group of events that simply lack the grouping field", async () => {
    listMock.mockResolvedValue(
      result([finding({ group_field: "attr:host", group_value: "", group_basis: "scope" })]),
    );
    renderView();
    expect(await screen.findByText(/\(no value\)/)).toBeInTheDocument();
  });
});
