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
    getJob: (...a: unknown[]) => getJobMock(...a),
  },
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

  it("offers a retry when the transfer itself fails", async () => {
    downloadExportMock.mockRejectedValueOnce(new Error("network died"));
    renderDialog();
    fireEvent.click(screen.getByTitle("Export case as .vestigo archive"));
    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    const retry = await screen.findByRole("button", { name: "Retry download" });
    expect(screen.getByText("network died")).toBeTruthy();

    downloadExportMock.mockResolvedValueOnce(undefined);
    fireEvent.click(retry);
    await waitFor(() => expect(downloadExportMock).toHaveBeenCalledTimes(2));
  });
});
