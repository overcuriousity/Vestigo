/**
 * The event export is the one transfer whose size nobody can know in advance:
 * the server streams it with no row cap and no `Content-Length`, and the
 * browser buffers the whole Blob before it can be saved. So progress here can
 * only ever be bytes-so-far — which is still the difference between "working"
 * and "hung" — and a way out matters more than anywhere else.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ExportDialog } from "@/components/explorer/ExportDialog";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { installRadixJsdomStubs } from "./helpers/radix";
import type { VizFieldsResponse } from "@/api/types";

const downloadExportMock = vi.fn();
const downloadFieldInventoryMock = vi.fn();
const vizFieldsMock = vi.fn();

vi.mock("@/api/export", () => ({
  downloadExport: (...a: unknown[]) => downloadExportMock(...a),
  downloadFieldInventory: (...a: unknown[]) => downloadFieldInventoryMock(...a),
}));

vi.mock("@/api/viz", async () => {
  const actual = await vi.importActual<typeof import("@/api/viz")>("@/api/viz");
  return {
    ...actual,
    vizApi: { ...actual.vizApi, fields: (...a: unknown[]) => vizFieldsMock(...a) },
  };
});

const FIELDS: VizFieldsResponse = {
  fields: [
    { token: "attr:src_ip", distinct: 4210, coverage: 0.91 },
    { token: "artifact", distinct: 12, coverage: 1 },
  ],
};

beforeAll(() => {
  installRadixJsdomStubs();
});

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
  downloadFieldInventoryMock.mockReset();
  vizFieldsMock.mockReset();
  vizFieldsMock.mockResolvedValue(FIELDS);
});

/** Switch to the inventory mode and pick a field — the starting point of every
 * inventory assertion below. */
async function openInventory() {
  renderDialog();
  fireEvent.click(screen.getByRole("button", { name: "Value inventory" }));
  const picker = await screen.findByRole("combobox", { name: "Field" });
  await waitFor(() => expect(picker.querySelectorAll("option").length).toBe(3));
  fireEvent.change(picker, { target: { value: "attr:src_ip" } });
}

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
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("ExportDialog — value inventory (#295)", () => {
  it("cannot be downloaded before a field is chosen", () => {
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Value inventory" }));

    expect(
      screen.getByRole("button", { name: /Download \.csv/ }).hasAttribute("disabled"),
    ).toBe(true);
  });

  it("sends the field, the chosen columns and separator, and the filters", async () => {
    downloadFieldInventoryMock.mockResolvedValue(undefined);
    await openInventory();
    fireEvent.click(screen.getByRole("button", { name: ";" }));
    fireEvent.click(screen.getByRole("button", { name: /Download \.csv/ }));

    await waitFor(() =>
      expect(downloadFieldInventoryMock).toHaveBeenCalledWith(
        "c1",
        "t1",
        { q: "ssh" },
        {
          field: "attr:src_ip",
          // `value` is what the file is of; `count` rides along because the
          // default ordering sorts by it.
          columns: ["value", "first_seen", "last_seen", "count"],
          separator: "semicolon",
          orderBy: "count_desc",
        },
        expect.anything(),
      ),
    );
  });

  it("drops a column the analyst unticks", async () => {
    downloadFieldInventoryMock.mockResolvedValue(undefined);
    await openInventory();
    fireEvent.click(screen.getByRole("checkbox", { name: "First seen" }));
    fireEvent.click(screen.getByRole("button", { name: /Download \.csv/ }));

    await waitFor(() =>
      expect(downloadFieldInventoryMock.mock.calls[0][3]).toMatchObject({
        columns: ["value", "last_seen", "count"],
      }),
    );
  });

  it("offers .tsv when the separator is a tab", async () => {
    await openInventory();
    fireEvent.click(screen.getByRole("button", { name: "tab" }));

    expect(screen.getByRole("button", { name: /Download \.tsv/ })).toBeTruthy();
  });

  it("shows how many rows the file will have before asking for it", async () => {
    await openInventory();

    // Grouping separator is the test runner's locale, not ours.
    expect(screen.getByText(/4[.,]210 rows before filtering/)).toBeTruthy();
  });
});
