/**
 * An empty case list is where a user lands after deleting their demo case, but
 * a user with real cases can delete theirs too — so the offer has to survive a
 * non-empty list. It renders only when the backend says seeding is on and the
 * user has no demo case, and it stays disabled while a seed job is in flight:
 * the build takes ~10s, and a second click in that window seeds a second case.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { CaseList } from "@/components/cases/CaseList";
import { useJobsStore } from "@/stores/jobs";
import type { Case } from "@/api/types";

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

function aCase(overrides: Partial<Case> = {}): Case {
  return {
    id: "case1",
    name: "Real work",
    description: null,
    owner_id: "u1",
    team_id: null,
    is_demo: false,
    access_level: "manage",
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

function renderList() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // CaseCard links to the case, so a populated list needs a router.
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CaseList />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CaseList demo offer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useJobsStore.setState({ jobs: {} });
    listMock.mockResolvedValue([]);
    seedMock.mockResolvedValue({ job_id: "job123" });
    capabilitiesMock.mockReturnValue({ demo_case: true });
  });

  it("offers the demo case in the empty state when the capability is on", async () => {
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

  it("still offers the demo case under a non-empty list", async () => {
    listMock.mockResolvedValue([aCase()]);
    renderList();
    expect(await screen.findByRole("button", { name: /demo case/i })).toBeTruthy();
  });

  it("hides the offer when the user already has a demo case", async () => {
    listMock.mockResolvedValue([
      aCase(),
      aCase({ id: "case2", name: "Demo — contractor account compromise", is_demo: true }),
    ]);
    renderList();
    await screen.findByText("Real work");
    expect(screen.queryByRole("button", { name: /demo case/i })).toBeNull();
  });

  it("stays disabled while the seed job is still running", async () => {
    renderList();
    const button = await screen.findByRole("button", { name: /demo case/i });
    fireEvent.click(button);
    await waitFor(() => expect(seedMock).toHaveBeenCalledTimes(1));

    // The tray now owns job123 and it has not finished, so a second click
    // must not reach the API.
    await waitFor(() => expect(useJobsStore.getState().jobs["job123"]).toBeTruthy());
    const pending = await screen.findByRole("button", { name: /preparing the demo case/i });
    expect((pending as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(pending);
    expect(seedMock).toHaveBeenCalledTimes(1);
  });

  it("surfaces the server's refusal", async () => {
    seedMock.mockRejectedValue(new Error("You already have a demo case."));
    renderList();
    fireEvent.click(await screen.findByRole("button", { name: /demo case/i }));
    expect(await screen.findByText(/already have a demo case/i)).toBeTruthy();
  });
});
