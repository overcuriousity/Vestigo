/**
 * The generated-converters panel: rows with status, download link, regenerate
 * only with the capability, attempt detail on expand, and nothing at all when
 * the feature is off and the case has no scripts.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { GeneratedConvertersPanel } from "@/components/sources/GeneratedConvertersPanel";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { useJobsStore } from "@/stores/jobs";

const listMock = vi.fn();
const getMock = vi.fn();
const capsMock = vi.fn();
const regenMock = vi.fn();

vi.mock("@/api/converters", () => ({
  convertersApi: {
    listForCase: (...a: unknown[]) => listMock(...a),
    getForCase: (...a: unknown[]) => getMock(...a),
    caseDownloadUrl: (c: string, id: string) => `/api/cases/${c}/converters/${id}/download`,
    regenerate: (...a: unknown[]) => regenMock(...a),
  },
}));
vi.mock("@/api/agent", () => ({
  agentApi: { getInfo: async () => ({ model: "m", api_base_url: "http://x/v1" }) },
}));
vi.mock("@/api/health", () => ({ useCapabilities: () => capsMock() }));

const SCRIPTS = [
  {
    id: "s1",
    name: "syslog2vestigo",
    version: 2,
    status: "working",
    model: "gpt-x",
    sources_produced: 3,
    created_at: "2026-08-17T10:00:00Z",
    raw_filename: "auth.log",
    attempts: [],
  },
  {
    id: "s2",
    name: "weird2vestigo",
    version: 1,
    status: "failed",
    model: "gpt-x",
    sources_produced: 0,
    created_at: "2026-08-17T11:00:00Z",
    raw_filename: "weird.log",
    attempts: [],
  },
];

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter>
          <GeneratedConvertersPanel caseId="c1" />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  listMock.mockReset();
  getMock.mockReset();
  capsMock.mockReset();
  regenMock.mockReset();
  useJobsStore.setState({ jobs: {} });
});

describe("GeneratedConvertersPanel", () => {
  it("renders nothing when the feature is off and the case has no scripts", async () => {
    capsMock.mockReturnValue({ converter_generation: false });
    listMock.mockResolvedValue({ scripts: [], sample_bytes: 65536 });
    renderPanel();
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText(/Generated converters/)).toBeNull();
  });

  it("lists scripts with status, download link, and regenerate only when enabled", async () => {
    capsMock.mockReturnValue({ converter_generation: false });
    listMock.mockResolvedValue({ scripts: SCRIPTS, sample_bytes: 65536 });
    renderPanel();
    expect(await screen.findByText("syslog2vestigo v2")).toBeTruthy();
    expect(screen.getByText("working")).toBeTruthy();
    expect(screen.getByText("failed")).toBeTruthy();
    const links = screen.getAllByTitle("Download script") as HTMLAnchorElement[];
    expect(links[0].getAttribute("href")).toBe("/api/cases/c1/converters/s1/download");
    expect(screen.queryByLabelText(/Regenerate syslog2vestigo/)).toBeNull();
  });

  it("offers regenerate with the capability and shows attempts on expand", async () => {
    capsMock.mockReturnValue({ converter_generation: true });
    listMock.mockResolvedValue({ scripts: SCRIPTS, sample_bytes: 65536 });
    getMock.mockResolvedValue({
      ...SCRIPTS[1],
      hint: "try harder",
      sample_excerpt: "line one\nline two",
      attempts: [
        {
          n: 1,
          phase: "sample",
          model: "gpt-x",
          elapsed_ms: 1200,
          exit_code: 1,
          stderr_tail: "Traceback: boom",
          validation: {
            ok: false,
            rows: 0,
            checks: [{ name: "run", ok: false, detail: "exit code 1", enforced: true }],
          },
        },
      ],
    });
    renderPanel();
    expect(await screen.findByLabelText(/Regenerate syslog2vestigo/)).toBeTruthy();
    fireEvent.click(screen.getByText("weird2vestigo v1"));
    expect(await screen.findByText(/Attempts \(1\)/)).toBeTruthy();
    expect(screen.getByText("run")).toBeTruthy();
    expect(screen.getByText(/exit code 1/)).toBeTruthy();
    expect(screen.getByText(/Traceback: boom/)).toBeTruthy();
    expect(screen.getByText(/line two/)).toBeTruthy();
    expect(getMock).toHaveBeenCalledWith("c1", "s2");
  });

  it("disables Regenerate for a script whose job is in flight and polls the list", async () => {
    capsMock.mockReturnValue({ converter_generation: true });
    listMock.mockResolvedValue({ scripts: SCRIPTS, sample_bytes: 65536 });
    regenMock.mockResolvedValue({ job_id: "job-7" });
    renderPanel();
    const btn = (await screen.findByLabelText(/Regenerate syslog2vestigo/)) as HTMLButtonElement;
    fireEvent.click(btn);
    fireEvent.click(await screen.findByRole("button", { name: "Regenerate" }));
    await waitFor(() => expect(regenMock).toHaveBeenCalledWith("c1", "s1", undefined));
    // The row does not exist until the model's first reply, so the panel keeps
    // asking for the list instead of trusting a one-off invalidation…
    await waitFor(() => expect(useJobsStore.getState().jobs["job-7"]).toBeTruthy());
    expect((screen.getByLabelText(/Regenerate syslog2vestigo/) as HTMLButtonElement).disabled).toBe(
      true,
    );
    // …and a second click cannot start a second job.
    fireEvent.click(screen.getByLabelText(/Regenerate syslog2vestigo/));
    expect(regenMock).toHaveBeenCalledTimes(1);
    // The other script's button is untouched.
    expect((screen.getByLabelText(/Regenerate weird2vestigo/) as HTMLButtonElement).disabled).toBe(
      false,
    );
    // Once the job is terminal the button comes back.
    useJobsStore.getState().updateJob({
      id: "job-7",
      kind: "convert_ingest",
      status: "completed",
      progress: null,
      result: null,
      error: null,
    });
    await waitFor(() =>
      expect(
        (screen.getByLabelText(/Regenerate syslog2vestigo/) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
  });

  it("keeps an expanded row's attempts under the list's query key so a refresh reaches them", async () => {
    capsMock.mockReturnValue({ converter_generation: true });
    listMock.mockResolvedValue({ scripts: SCRIPTS, sample_bytes: 65536 });
    getMock.mockResolvedValue({ ...SCRIPTS[0], attempts: [], sample_excerpt: "x" });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter>
            <GeneratedConvertersPanel caseId="c1" />
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByText("syslog2vestigo v2"));
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(1));
    // What the job tray does on a terminal convert job.
    await qc.invalidateQueries({ queryKey: ["converters", "c1"] });
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2));
  });
});
