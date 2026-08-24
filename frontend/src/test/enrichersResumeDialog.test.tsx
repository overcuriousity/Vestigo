/**
 * Resume is the analyst's only exit from an enrichment run that died before its
 * results were applied (session-56: ClickHouse OOM-killed mid-apply while the
 * app stayed up, so startup reconciliation never saw the orphan). The POST that
 * starts it returns long before the partition rewrite it kicks off finishes, so
 * both things this file covers are about that gap: the button must not re-arm
 * while the rewrite is in flight (a second click 409s against the analyst's own
 * resume), and the banner must clear itself when the job completes rather than
 * waiting for someone to close and reopen the dialog.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { EnrichersDialog } from "@/components/timelines/EnrichersDialog";
import { useJobsStore } from "@/stores/jobs";
import type { Timeline } from "@/api/types";

const listMock = vi.fn();
const resumeMock = vi.fn();

vi.mock("@/api/enrichers", () => ({
  enrichersApi: {
    listForTimeline: (...a: unknown[]) => listMock(...a),
    resume: (...a: unknown[]) => resumeMock(...a),
    run: vi.fn(),
    setConfig: vi.fn(),
  },
}));

const timeline = { id: "t1", case_id: "c1", name: "Timeline One" } as unknown as Timeline;

function enricherRow(withUnfinished: boolean) {
  return [
    {
      key: "geoip",
      display_name: "GeoIP",
      description: "",
      eligible: true,
      sample_checked: 1,
      sample_matched: 1,
      eligibility_error: null,
      mode: "manual",
      enabled: true,
      unfinished_run: withUnfinished
        ? {
            job_id: "dead-job",
            started_at: "2026-08-07T10:00:00+00:00",
            age_seconds: 600,
            staged_rows: 12,
            staged_sources: 1,
            completed_sources: 1,
          }
        : null,
    },
  ];
}

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <EnrichersDialog caseId="c1" timeline={timeline} />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Enrichers" }));
  return qc;
}

beforeEach(() => {
  listMock.mockReset();
  resumeMock.mockReset();
  useJobsStore.setState({ jobs: {} });
});

describe("EnrichersDialog resume", () => {
  it("keeps Resume disabled until the apply job reports terminal", async () => {
    listMock.mockResolvedValue(enricherRow(true));
    resumeMock.mockResolvedValue({
      job_id: "resume-job",
      resumed_job_id: "dead-job",
      status: "queued",
      staged_rows: 12,
      staged_sources: 1,
    });
    renderDialog();

    const resume = await screen.findByRole("button", { name: "Resume" });
    fireEvent.click(resume);

    // The POST has returned but the rewrite runs on in a BackgroundTasks
    // callback — the tracked job is what says when it is actually over.
    await waitFor(() => expect(resumeMock).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Resum/ })).toBeDisabled(),
    );

    // A failed resume must hand the button back rather than stranding the
    // analyst with a banner they can no longer act on.
    useJobsStore.getState().updateJob({
      id: "resume-job",
      kind: "enrich",
      status: "failed",
      progress: null,
      result: null,
      error: "clickhouse unreachable",
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Resum/ })).not.toBeDisabled(),
    );
  });

  it("refetches the enrichers list when the resume job completes", async () => {
    listMock.mockResolvedValue(enricherRow(true));
    resumeMock.mockResolvedValue({
      job_id: "resume-job",
      resumed_job_id: "dead-job",
      status: "queued",
      staged_rows: 12,
      staged_sources: 1,
    });
    const qc = renderDialog();

    fireEvent.click(await screen.findByRole("button", { name: "Resume" }));
    await waitFor(() => expect(resumeMock).toHaveBeenCalledTimes(1));

    // The job is registered with the dialog's own query key, so JobTray's
    // completion invalidation reaches the banner. Without it the marker stays
    // on screen — and Run now stays disabled — until the dialog is reopened,
    // because the mutation's own onSettled fires while the apply is still in
    // flight and the query never becomes stale again on its own.
    const tracked = useJobsStore.getState().jobs["resume-job"];
    expect(tracked.invalidate).toContainEqual(["timeline-enrichers", "c1", "t1"]);

    // Replay what JobTray does on completion: the marker is gone by then, so
    // the banner clears without anyone reopening the dialog.
    listMock.mockResolvedValue(enricherRow(false));
    for (const key of tracked.invalidate ?? []) qc.invalidateQueries({ queryKey: key });
    await waitFor(() =>
      expect(screen.queryByText(/An earlier run was interrupted/)).not.toBeInTheDocument(),
    );
  });
});
