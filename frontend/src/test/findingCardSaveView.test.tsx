/**
 * FindingCard: "Apply to Explorer" only writes the URL, so a finding worth
 * keeping dies with the conversation. Saving the same filter set as a View
 * puts it in the left-hand Views panel — same dialog and same payload
 * normalization the Explorer's own Save View uses.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
// AddToStoryButton navigates client-side on its success toast, so this
// subtree needs a router the same way the real app always provides one.
import { MemoryRouter } from "react-router-dom";
import { FindingCard } from "@/components/agent/FindingCard";
import { TooltipProvider } from "@/components/ui/Tooltip";
import type { AgentFilterSpec } from "@/api/agent";

const createViewMock = vi.fn();

vi.mock("@/api/views", () => ({
  viewsApi: {
    create: (...a: unknown[]) => createViewMock(...a),
  },
}));

const CASE = "c1";
const SPEC: AgentFilterSpec = {
  q: "powershell",
  filters: { artifact: ["windows:evtx"] },
};

function renderCard(onApply = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <TooltipProvider>
        <FindingCard
          caseId={CASE}
          timelineId="t1"
          title="Suspicious PowerShell"
          description="looks odd"
          spec={SPEC}
          total={42}
          onApply={onApply}
        />
      </TooltipProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return onApply;
}

beforeEach(() => {
  vi.clearAllMocks();
  createViewMock.mockResolvedValue({ id: "v1", name: "Odd PowerShell" });
});

describe("FindingCard save as View", () => {
  it("saves the finding's filters as a named View", async () => {
    renderCard();
    fireEvent.click(screen.getByLabelText("Save as a View"));

    const nameInput = await screen.findByPlaceholderText(/Suspicious PowerShell events/);
    fireEvent.change(nameInput, { target: { value: "Odd PowerShell" } });
    fireEvent.click(screen.getByRole("button", { name: /Save View/ }));

    await waitFor(() => expect(createViewMock).toHaveBeenCalled());
    const [caseId, name, query] = createViewMock.mock.calls[0];
    expect(caseId).toBe(CASE);
    expect(name).toBe("Odd PowerShell");
    // The view's query column carries the finding's free-text term, matching
    // how the Explorer's own Save View builds it.
    expect(query).toBe("powershell");
  });

  it("still applies to the Explorer without saving", () => {
    const onApply = renderCard();
    fireEvent.click(screen.getByRole("button", { name: /Apply to Explorer/ }));
    expect(onApply).toHaveBeenCalledTimes(1);
    expect(createViewMock).not.toHaveBeenCalled();
  });
});
