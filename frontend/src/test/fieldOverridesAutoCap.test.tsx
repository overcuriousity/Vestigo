/**
 * The auto preview under the string detectors' scan cap.
 *
 * charset and entropy run one heavy per-field query under a single scan-gate
 * slot, so `_auto_string_fields` re-cuts to _MAX_AUTO_SCAN_FIELDS after the
 * pins are prepended — a stored declaration must not be able to double the
 * scan. The picker mirrors that selection, and a preview that checks more
 * chips than the run scans is a claim about a scan that will not happen.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InvestigateSheet } from "@/components/analysis/InvestigateSheet";
import { AUTO_SCAN_MAX_FIELDS } from "@/components/analysis/detector-hooks";
import { TooltipProvider } from "@/components/ui/Tooltip";

vi.mock("@/hooks/useMethodFindings", () => ({
  useFindingsPage: () => ({ limit: 50, canRaise: true, raise: () => {} }),
  useMethodFindings: () => ({ data: undefined, isLoading: false, isError: false }),
  METHOD_LIMIT: 50,
}));

// Enough recommended categoricals to fill the cap on their own, so the two
// pinned fields have nowhere to go but over it.
vi.mock("@/api/anomalies", () => ({
  anomaliesApi: {
    fields: async () => ({
      fields: [
        ...Array.from({ length: 20 }, (_, i) => ({
          token: `attr:f${i}`,
          distinct: 40 - i,
          coverage: 0.9,
          kind: "categorical",
          recommended: true,
        })),
        // More identifiers than the quota reserves slots for, so the two the
        // analyst pinned are exactly the ones it leaves out.
        ...Array.from({ length: 6 }, (_, i) => ({
          token: `attr:id${i}`,
          distinct: 900 - i,
          coverage: 0.5,
          kind: "identifier",
          recommended: false,
        })),
        { token: "attr:rare_a", distinct: 3, coverage: 0.2, kind: "identifier", recommended: false },
        { token: "attr:rare_b", distinct: 4, coverage: 0.2, kind: "identifier", recommended: false },
      ],
    }),
    numericFields: async () => ({ fields: [] }),
  },
}));

vi.mock("@/api/timelines", () => ({
  timelinesApi: {
    get: async () => ({
      id: "t1",
      case_id: "c1",
      field_overrides: { charset: { "attr:rare_a": true, "attr:rare_b": true } },
    }),
    patchFieldOverrides: async () => ({ id: "t1", case_id: "c1", field_overrides: {} }),
  },
}));

vi.mock("@/api/cases", () => ({
  casesApi: { get: async () => ({ id: "c1", access_level: "contribute" }) },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <TooltipProvider>{children}</TooltipProvider>
    </QueryClientProvider>
  );
}

describe("the charset picker's auto preview", () => {
  it("never previews more fields than the scan cap allows", async () => {
    render(
      <InvestigateSheet
        caseId="c1"
        timelineId="t1"
        railWidth={360}
        mode="method"
        methodId={"charset" as never}
        onClose={() => {}}
        onRun={() => {}}
        query={{ data: undefined, isFetching: false, isError: false } as never}
      />,
      { wrapper },
    );
    fireEvent.click(screen.getByRole("button", { name: /fields/i }));
    await screen.findByTestId("declare-attr:rare_a");

    await waitFor(() =>
      expect(screen.getByText(/fields selected/)).toHaveTextContent(
        `${AUTO_SCAN_MAX_FIELDS} fields selected`,
      ),
    );
  });
});
