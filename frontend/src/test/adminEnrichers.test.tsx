/**
 * Enricher assets are GeoLite-sized — hundreds of MB — and the install is
 * synchronous server-side, so there is no job to poll afterwards. The upload's
 * own progress row is the entire feedback, and the card that renders it had no
 * local state at all before this.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AdminEnrichersPage } from "@/pages/admin/AdminEnrichersPage";

const adminConfigsMock = vi.fn();
const uploadAssetMock = vi.fn();

vi.mock("@/api/enrichers", () => ({
  enrichersApi: {
    adminConfigs: () => adminConfigsMock(),
    uploadAsset: (...a: unknown[]) => uploadAssetMock(...a),
    setAdminConfig: vi.fn(),
  },
}));

const CONFIG = {
  key: "geoip",
  display_name: "GeoIP",
  description: "Resolve IPs to locations",
  available: false,
  reason: "asset not installed",
  auto_run_default: false,
  asset: {
    name: "GeoLite2-City.mmdb",
    description: "MaxMind city database",
    uploaded: false,
    size_bytes: null,
    accepted_extensions: [".mmdb"],
  },
};

interface ProgressOpts {
  onProgress: (p: { loaded: number; total: number | null }) => void;
  signal: AbortSignal;
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <AdminEnrichersPage />
    </QueryClientProvider>,
  );
}

async function pickAsset(name = "GeoLite2-City.mmdb", bytes = 78_000_000) {
  const button = await screen.findByRole("button", { name: /Upload GeoLite2-City\.mmdb/ });
  const input = button.parentElement!.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File([new Uint8Array(1)], name);
  Object.defineProperty(file, "size", { value: bytes });
  fireEvent.change(input, { target: { files: [file] } });
}

beforeEach(() => {
  adminConfigsMock.mockReset();
  uploadAssetMock.mockReset();
  adminConfigsMock.mockResolvedValue([CONFIG]);
});

describe("AdminEnrichersPage asset upload", () => {
  it("reports bytes sent while the asset is in flight", async () => {
    uploadAssetMock.mockImplementation((..._a: unknown[]) => {
      const opts = _a[2] as ProgressOpts;
      opts.onProgress({ loaded: 39_000_000, total: 78_000_000 });
      return new Promise(() => {});
    });
    renderPage();
    await pickAsset();

    expect(await screen.findByText("Uploading GeoLite2-City.mmdb")).toBeTruthy();
    expect(screen.getByText(/37\.2 MB \/ 74\.4 MB/)).toBeTruthy();
  });

  it("uploads the picked file even though it never reaches component state", async () => {
    // The picker fires immediately, so the file travels through `submit` —
    // reading it back from state would still be null on this render.
    uploadAssetMock.mockResolvedValue({ available: true, reason: null });
    renderPage();
    await pickAsset();

    await waitFor(() => expect(uploadAssetMock).toHaveBeenCalledTimes(1));
    expect(uploadAssetMock.mock.calls[0][0]).toBe("geoip");
    expect((uploadAssetMock.mock.calls[0][1] as File).name).toBe("GeoLite2-City.mmdb");
  });

  it("cancels an upload in flight, leaving the previous asset in place", async () => {
    let signal: AbortSignal | undefined;
    uploadAssetMock.mockImplementation((..._a: unknown[]) => {
      const opts = _a[2] as ProgressOpts;
      signal = opts.signal;
      opts.onProgress({ loaded: 1_000, total: 78_000_000 });
      return new Promise((_res, rej) =>
        opts.signal.addEventListener("abort", () =>
          rej(new DOMException("Aborted", "AbortError")),
        ),
      );
    });
    renderPage();
    await pickAsset();

    fireEvent.click(await screen.findByText("Cancel"));
    expect(signal?.aborted).toBe(true);
    await waitFor(() =>
      expect(screen.queryByText("Uploading GeoLite2-City.mmdb")).toBeNull(),
    );
    expect(screen.queryByText(/Aborted/)).toBeNull();
  });

  it("renders a failed install inline", async () => {
    uploadAssetMock.mockRejectedValueOnce(new Error("not a valid mmdb"));
    renderPage();
    await pickAsset();

    expect(await screen.findByText("not a valid mmdb")).toBeTruthy();
  });
});
