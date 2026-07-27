/**
 * The event export is the one transfer whose size nobody can know in advance:
 * the server streams it with no row cap and no `Content-Length`, and the
 * browser buffers the whole Blob before it can be saved. So progress here can
 * only ever be bytes-so-far — which is still the difference between "working"
 * and "hung" — and a way out matters more than anywhere else.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ExportDialog } from "@/components/explorer/ExportDialog";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const downloadExportMock = vi.fn();

vi.mock("@/api/export", () => ({
  downloadExport: (...a: unknown[]) => downloadExportMock(...a),
}));

interface ProgressOpts {
  onProgress: (p: { loaded: number; total: number | null }) => void;
  signal: AbortSignal;
}

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ExportDialog caseId="c1" timelineId="t1" filters={{ q: "ssh" }} total={12_000} />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: /Export/ }));
}

beforeEach(() => {
  downloadExportMock.mockReset();
});

describe("ExportDialog", () => {
  it("says the browser buffers the file, not just that the server streams it", () => {
    // The old copy — "Streams directly from the backend — no memory limit" —
    // was true of the server and false of the client doing the reading.
    renderDialog();
    expect(screen.getByText(/holds the whole file in memory/)).toBeTruthy();
  });

  it("downloads once however fast the button is clicked", async () => {
    downloadExportMock.mockReturnValue(new Promise(() => {}));
    renderDialog();
    const submit = screen.getByRole("button", { name: /Download \.csv/ });
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() => expect(downloadExportMock).toHaveBeenCalledTimes(1));
  });

  it("reports bytes received with no percentage — the response has no length", async () => {
    downloadExportMock.mockImplementation((..._a: unknown[]) => {
      const opts = _a[4] as ProgressOpts;
      opts.onProgress({ loaded: 2_500_000, total: null });
      return new Promise(() => {});
    });
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: /Download \.csv/ }));

    expect(await screen.findByText("Downloading .csv")).toBeTruthy();
    // Bytes so far, and no "/ total" half — there is no denominator to show.
    const readout = screen.getByText(/2\.4 MB/);
    expect(readout.textContent).not.toContain("/");
  });

  it("aborts a download in flight", async () => {
    let signal: AbortSignal | undefined;
    downloadExportMock.mockImplementation((..._a: unknown[]) => {
      const opts = _a[4] as ProgressOpts;
      signal = opts.signal;
      opts.onProgress({ loaded: 1_000, total: null });
      return new Promise((_res, rej) =>
        opts.signal.addEventListener("abort", () =>
          rej(new DOMException("Aborted", "AbortError")),
        ),
      );
    });
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: /Download \.csv/ }));

    fireEvent.click(await screen.findByText("Cancel download"));
    expect(signal?.aborted).toBe(true);
    await waitFor(() => expect(screen.queryByText("Downloading .csv")).toBeNull());
  });

  it("passes the chosen format through and closes on success", async () => {
    downloadExportMock.mockResolvedValue(undefined);
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: ".jsonl" }));
    fireEvent.click(screen.getByRole("button", { name: /Download \.jsonl/ }));

    await waitFor(() =>
      expect(downloadExportMock).toHaveBeenCalledWith(
        "c1",
        "t1",
        "jsonl",
        { q: "ssh" },
        expect.anything(),
      ),
    );
    await waitFor(() => expect(screen.queryByText("Export Events")).toBeNull());
  });
});
