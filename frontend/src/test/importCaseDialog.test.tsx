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
    getJob: (...a: unknown[]) => getJobMock(...a),
  },
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

function pickFileAndImport() {
  fireEvent.click(screen.getByRole("button", { name: /Import/ }));
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File([new Uint8Array([1, 2, 3])], "backup.vestigo");
  fireEvent.change(input, { target: { files: [file] } });
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
