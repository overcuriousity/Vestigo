/**
 * The source upload is the app's largest routine transfer — the server takes
 * up to 10 GiB and the ingest job the tray polls does not exist until the
 * whole body has landed. So everything these tests cover happens in a window
 * where the analyst previously had a disabled button and nothing else: byte
 * progress, a way out, and a guard against starting the whole thing twice.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { UploadDialog } from "@/components/timelines/UploadDialog";
import { useJobsStore } from "@/stores/jobs";

const uploadMock = vi.fn();

vi.mock("@/api/sources", () => ({
  sourcesApi: {
    upload: (...a: unknown[]) => uploadMock(...a),
  },
}));

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <UploadDialog caseId="c1" />
    </QueryClientProvider>,
  );
}

function pickFile(name = "auth.csv", bytes = 9_500_000) {
  fireEvent.click(screen.getByRole("button", { name: /Upload Data/ }));
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [new File([new Uint8Array(1)], name)] } });
  // jsdom sizes a File from its contents; the dialog reads `.size` for the
  // progress denominator, so give it a realistic one.
  const picked = input.files![0];
  Object.defineProperty(picked, "size", { value: bytes });
  return picked;
}

interface ProgressOpts {
  onProgress: (p: { loaded: number; total: number | null }) => void;
  signal: AbortSignal;
}

beforeEach(() => {
  uploadMock.mockReset();
  useJobsStore.setState({ jobs: {} });
});

describe("UploadDialog", () => {
  it("uploads once however fast the button is clicked", async () => {
    // Same defect class as the duplicate case import (#184): the file is
    // content-hashed server-side so a second upload is deduped rather than
    // duplicated, but it still costs a full multi-GB round trip.
    uploadMock.mockReturnValue(new Promise(() => {}));
    renderDialog();
    pickFile();
    const submit = screen.getByRole("button", { name: "Upload" });

    fireEvent.click(submit);
    fireEvent.click(submit);
    fireEvent.click(submit);

    await waitFor(() => expect(uploadMock).toHaveBeenCalledTimes(1));
    expect(submit).toBeDisabled();
  });

  it("reports bytes sent while the file is in flight", async () => {
    uploadMock.mockImplementation((..._a: unknown[]) => {
      const opts = _a[4] as ProgressOpts;
      opts.onProgress({ loaded: 4_750_000, total: 9_500_000 });
      return new Promise(() => {});
    });
    renderDialog();
    pickFile();
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));

    expect(await screen.findByText("Uploading auth.csv")).toBeTruthy();
    expect(screen.getByText(/4\.5 MB \/ 9\.1 MB/)).toBeTruthy();
  });

  it("cancels an upload in flight and leaves the dialog usable", async () => {
    // Safe by construction: the server streams the body to a temp file and
    // only creates the Source row and the ingest job once it has all landed.
    let signal: AbortSignal | undefined;
    uploadMock.mockImplementation((..._a: unknown[]) => {
      const opts = _a[4] as ProgressOpts;
      signal = opts.signal;
      opts.onProgress({ loaded: 1_000, total: 9_500_000 });
      return new Promise((_res, rej) =>
        opts.signal.addEventListener("abort", () =>
          rej(new DOMException("Aborted", "AbortError")),
        ),
      );
    });
    renderDialog();
    pickFile();
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));

    fireEvent.click(await screen.findByText("Cancel upload"));
    expect(signal?.aborted).toBe(true);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Upload" })).not.toBeDisabled(),
    );
    // A cancel is the analyst's decision, not a failure to report.
    expect(screen.queryByText(/Aborted/)).toBeNull();
  });

  it("hands the ingest job to the tray and closes on a clean upload", async () => {
    uploadMock.mockResolvedValue({ duplicate: false, job_id: "job-1", status: "ingesting" });
    renderDialog();
    pickFile();
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));

    await waitFor(() => expect(useJobsStore.getState().jobs["job-1"]).toBeTruthy());
    expect(useJobsStore.getState().jobs["job-1"].label).toBe('Ingesting "auth.csv"');
    await waitFor(() => expect(screen.queryByText("Upload source file")).toBeNull());
  });

  it("holds the dialog open on a duplicate, which never gets a job", async () => {
    // The duplicate message is the only thing that explains why nothing is
    // ingesting; closing the dialog would take it away with it.
    uploadMock.mockResolvedValue({
      duplicate: true,
      job_id: null,
      status: "ready",
      events_parsed: 4210,
    });
    renderDialog();
    pickFile();
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));

    // Separator is locale-dependent (`toLocaleString`), the count is not.
    expect(await screen.findByText(/already been ingested \(4.210 events\)/)).toBeTruthy();
    expect(useJobsStore.getState().jobs).toEqual({});
  });

  it("renders a failure inline and lets the analyst try again", async () => {
    uploadMock.mockRejectedValueOnce(new Error("unsupported format"));
    renderDialog();
    pickFile();
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));

    expect(await screen.findByText("unsupported format")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Upload" })).not.toBeDisabled();
  });
});
