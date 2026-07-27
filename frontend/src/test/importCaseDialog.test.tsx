/**
 * ImportCaseDialog: a restore that produced warnings must not navigate away.
 * The job store is in-memory, so the moment the dialog closes is the last
 * chance an analyst ever has to read "no blob for X" or "user Y not found,
 * attributed to importer" — facts that change what they are looking at.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ImportCaseDialog } from "@/components/cases/ImportCaseDialog";

const startImportMock = vi.fn();
const getJobMock = vi.fn();
const navigateMock = vi.fn();

vi.mock("@/api/transfer", () => ({
  transferApi: {
    startImport: (...a: unknown[]) => startImportMock(...a),
  },
}));

// The restore job is polled under the job tray's own key/queryFn, so the two
// share one request stream instead of asking about the same job twice.
vi.mock("@/api/jobs", () => ({
  jobsApi: { get: (...a: unknown[]) => getJobMock(...a) },
}));

vi.mock("react-router-dom", async () => ({
  ...(await vi.importActual<typeof import("react-router-dom")>("react-router-dom")),
  useNavigate: () => navigateMock,
}));

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <ImportCaseDialog />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

function pickFile() {
  fireEvent.click(screen.getByRole("button", { name: /Import/ }));
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File([new Uint8Array([1, 2, 3])], "backup.vestigo");
  fireEvent.change(input, { target: { files: [file] } });
}

function pickFileAndImport() {
  pickFile();
  fireEvent.click(screen.getByRole("button", { name: "Import" }));
}

beforeEach(() => {
  vi.clearAllMocks();
  startImportMock.mockResolvedValue({ job_id: "j1" });
});

describe("ImportCaseDialog", () => {
  it("navigates straight to the case when the restore was clean", async () => {
    getJobMock.mockResolvedValue({
      id: "j1",
      kind: "case_import",
      status: "completed",
      result: { case_id: "c9", warnings: [] },
    });
    renderDialog();
    pickFileAndImport();

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/cases/c9"));
  });

  it("holds the dialog open and shows the importer's warnings", async () => {
    getJobMock.mockResolvedValue({
      id: "j1",
      kind: "case_import",
      status: "completed",
      result: {
        case_id: "c9",
        warnings: ["user bob not found on this instance — attributed to importer"],
      },
    });
    renderDialog();
    pickFileAndImport();

    const go = await screen.findByRole("button", { name: "Go to case" });
    expect(screen.getByText(/user bob not found/)).toBeTruthy();
    expect(navigateMock).not.toHaveBeenCalled();

    fireEvent.click(go);
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/cases/c9"));
  });

  it("cannot be submitted twice while the archive is still uploading (#184)", async () => {
    // The original defect: `jobId` was only set in the upload promise's
    // `.then()`, so nothing disabled the button for the whole multi-GB upload
    // and a second click started a second import of the same archive. The
    // clicks below all land in one task, before React can re-render the
    // button as disabled — only the synchronous ref guard stops them.
    startImportMock.mockReturnValue(new Promise(() => {}));
    renderDialog();
    pickFile();
    const submit = screen.getByRole("button", { name: "Import" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    fireEvent.click(submit);
    fireEvent.click(submit);

    await waitFor(() => expect(startImportMock).toHaveBeenCalledTimes(1));
    expect(submit).toBeDisabled();
  });

  it("reports upload bytes while the archive is in flight", async () => {
    startImportMock.mockImplementation(
      (_file: File, opts: { onProgress: (p: unknown) => void }) =>
        new Promise(() => opts.onProgress({ loaded: 5_000_000, total: 10_000_000 })),
    );
    renderDialog();
    pickFileAndImport();

    expect(await screen.findByText(/Uploading backup\.vestigo/)).toBeTruthy();
    // Bytes, not a bare percentage: "how much of my 4 GB archive has gone" is
    // the question, and the bar already carries the proportion.
    expect(screen.getByText(/4\.8 MB \/ 9\.5 MB/)).toBeTruthy();
  });

  it("aborts the upload when the analyst cancels, without reporting a failure", async () => {
    let abortSignal: AbortSignal | undefined;
    startImportMock.mockImplementation(
      (_file: File, opts: { signal: AbortSignal; onProgress: (p: unknown) => void }) => {
        abortSignal = opts.signal;
        opts.onProgress({ loaded: 1_000_000, total: 10_000_000 });
        return new Promise((_resolve, reject) => {
          opts.signal.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        });
      },
    );
    renderDialog();
    pickFileAndImport();

    fireEvent.click(await screen.findByText("Cancel upload"));
    expect(abortSignal?.aborted).toBe(true);
    // A cancel is the analyst's own decision, not an error to report back.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Import" })).not.toBeDisabled(),
    );
    expect(screen.queryByText(/Aborted/)).toBeNull();
  });

  it("names the server-side phase once the archive has landed", async () => {
    getJobMock.mockResolvedValue({
      id: "j1",
      kind: "case_import",
      status: "running",
      progress: { phase: "events", processed: 1, total: 4 },
      result: null,
    });
    renderDialog();
    pickFileAndImport();

    // Not the raw "events" token, and not the exporter's "Packing events".
    expect(await screen.findByText(/Restoring events/)).toBeTruthy();
  });

  it("lets the analyst cancel an upload, leaving no error behind", async () => {
    startImportMock.mockImplementation((_file: File, opts: { signal: AbortSignal }) => {
      return new Promise((_resolve, reject) => {
        opts.signal.addEventListener("abort", () =>
          reject(new DOMException("Aborted", "AbortError")),
        );
      });
    });
    renderDialog();
    pickFileAndImport();

    fireEvent.click(await screen.findByRole("button", { name: "Cancel upload" }));

    // Back to a re-submittable dialog: a cancel is not a failure.
    const submit = await screen.findByRole("button", { name: "Import" });
    expect(submit).not.toBeDisabled();
    expect(screen.queryByText(/Aborted/)).toBeNull();
  });

  it("surfaces a failed import as an error", async () => {
    getJobMock.mockResolvedValue({
      id: "j1",
      kind: "case_import",
      status: "failed",
      error: "manifest.json missing",
    });
    renderDialog();
    pickFileAndImport();

    expect(await screen.findByText("manifest.json missing")).toBeTruthy();
    expect(navigateMock).not.toHaveBeenCalled();
  });
});
