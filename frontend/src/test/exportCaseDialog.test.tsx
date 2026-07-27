/**
 * ExportCaseDialog: the server deletes the archive as soon as it has been
 * streamed, so the download must fire exactly once per completed job. The
 * effect keys off the polled job object, which changes identity on every
 * refetch (and runs twice under StrictMode) — a ref, not the effect deps, is
 * what makes it one-shot. A second fetch would 404 and show an error on top
 * of a download that actually worked.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ExportCaseDialog } from "@/components/cases/ExportCaseDialog";
import type { Case } from "@/api/types";

const startExportMock = vi.fn();
const downloadExportMock = vi.fn();
const getJobMock = vi.fn();

vi.mock("@/api/transfer", () => ({
  transferApi: {
    startExport: (...a: unknown[]) => startExportMock(...a),
    downloadExport: (...a: unknown[]) => downloadExportMock(...a),
  },
}));

vi.mock("@/api/jobs", () => ({
  jobsApi: { get: (...a: unknown[]) => getJobMock(...a) },
}));

const CASE = { id: "c1", name: "Roundtrip" } as Case;

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <StrictMode>
      <QueryClientProvider client={qc}>
        <ExportCaseDialog case_={CASE} />
      </QueryClientProvider>
    </StrictMode>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  startExportMock.mockResolvedValue({ job_id: "j1" });
  getJobMock.mockResolvedValue({ id: "j1", kind: "case_export", status: "completed" });
  downloadExportMock.mockResolvedValue(undefined);
});

describe("ExportCaseDialog", () => {
  it("downloads a completed export exactly once", async () => {
    renderDialog();
    fireEvent.click(screen.getByTitle("Export case as .vestigo archive"));
    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    await waitFor(() => expect(downloadExportMock).toHaveBeenCalled());
    expect(startExportMock).toHaveBeenCalledWith("c1", false);
    // Force extra renders; the ref must keep the download from firing again.
    await new Promise((r) => setTimeout(r, 50));
    expect(downloadExportMock).toHaveBeenCalledTimes(1);
  });

  it("starts exactly one export however fast the button is clicked", async () => {
    startExportMock.mockReturnValue(new Promise(() => {}));
    renderDialog();
    fireEvent.click(screen.getByTitle("Export case as .vestigo archive"));
    const submit = screen.getByRole("button", { name: "Export" });

    fireEvent.click(submit);
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() => expect(startExportMock).toHaveBeenCalledTimes(1));
  });

  it("names the phase the server is working on", async () => {
    getJobMock.mockResolvedValue({
      id: "j1",
      kind: "case_export",
      status: "running",
      progress: { phase: "blobs", processed: 2, total: 4 },
    });
    renderDialog();
    fireEvent.click(screen.getByTitle("Export case as .vestigo archive"));
    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    // Export-side copy: packing, not restoring.
    expect(await screen.findByText(/Packing original source files/)).toBeTruthy();
    expect(screen.getByText(/50%/)).toBeTruthy();
  });

  it("reports download bytes while the archive is streaming", async () => {
    downloadExportMock.mockImplementation(
      (
        _caseId: string,
        _jobId: string,
        _name: string,
        opts: { onProgress: (p: unknown) => void },
      ) => new Promise(() => opts.onProgress({ loaded: 3_000_000, total: 4_000_000 })),
    );
    renderDialog();
    fireEvent.click(screen.getByTitle("Export case as .vestigo archive"));
    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    expect(await screen.findByText("Downloading archive")).toBeTruthy();
    expect(screen.getByText(/2\.9 MB \/ 3\.8 MB/)).toBeTruthy();
  });

  it("aborts a download in flight and keeps the retry available", async () => {
    // The server only unlinks the archive after a *completed* stream, so a
    // cancelled download leaves it there and the retry is still good.
    let abortSignal: AbortSignal | undefined;
    downloadExportMock.mockImplementation(
      (
        _caseId: string,
        _jobId: string,
        _name: string,
        opts: { signal: AbortSignal; onProgress: (p: unknown) => void },
      ) => {
        abortSignal = opts.signal;
        opts.onProgress({ loaded: 1_000, total: 4_000_000 });
        return new Promise((_resolve, reject) => {
          opts.signal.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        });
      },
    );
    renderDialog();
    fireEvent.click(screen.getByTitle("Export case as .vestigo archive"));
    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    fireEvent.click(await screen.findByText("Cancel download"));
    expect(abortSignal?.aborted).toBe(true);
    await waitFor(() => expect(screen.queryByText("Downloading archive")).toBeNull());
  });

  it("offers a retry when the transfer itself fails", async () => {
    downloadExportMock.mockRejectedValueOnce(new Error("network died"));
    renderDialog();
    fireEvent.click(screen.getByTitle("Export case as .vestigo archive"));
    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    const retry = await screen.findByRole("button", { name: "Retry download" });
    // The failure is worded with what it means for the analyst: the archive
    // survives a transfer that died, so retrying is cheaper than re-exporting.
    expect(screen.getByText(/network died.*still on the server/)).toBeTruthy();

    downloadExportMock.mockResolvedValueOnce(undefined);
    fireEvent.click(retry);
    await waitFor(() => expect(downloadExportMock).toHaveBeenCalledTimes(2));
  });
});
