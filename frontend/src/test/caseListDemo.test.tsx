/**
 * An empty case list is where a user lands after deleting their demo case.
 * The restore offer only renders when the backend says the archive is actually
 * packaged — a button that can only answer 503 is worse than no button.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CaseList } from "@/components/cases/CaseList";

const listMock = vi.fn();
const seedMock = vi.fn();
const capabilitiesMock = vi.fn();

vi.mock("@/api/cases", () => ({
  casesApi: { list: () => listMock() },
}));

vi.mock("@/api/demo", () => ({
  demoApi: { seed: () => seedMock() },
}));

vi.mock("@/api/health", () => ({
  useCapabilities: () => capabilitiesMock(),
}));

function renderList() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CaseList />
    </QueryClientProvider>,
  );
}

describe("CaseList empty state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listMock.mockResolvedValue([]);
    seedMock.mockResolvedValue({ job_id: "job123" });
  });

  it("offers the demo case when the capability is on", async () => {
    capabilitiesMock.mockReturnValue({ demo_case: true });
    renderList();
    const button = await screen.findByRole("button", { name: /demo case/i });
    fireEvent.click(button);
    await waitFor(() => expect(seedMock).toHaveBeenCalledTimes(1));
  });

  it("hides the offer when the capability is off", async () => {
    capabilitiesMock.mockReturnValue({ demo_case: false });
    renderList();
    await screen.findByText(/no investigation cases yet/i);
    expect(screen.queryByRole("button", { name: /demo case/i })).toBeNull();
  });
});
