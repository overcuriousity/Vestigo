/**
 * `useFileTransfer` is the one place the submit guard, the abort signal and
 * the rate estimate live, so every transfer in the app inherits whatever this
 * gets wrong. The guard in particular cannot be tested through `disabled`:
 * the defect it exists for (#184) is precisely that a second click lands
 * before React re-renders the button.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError, type TransferOptions } from "@/api/client";
import { useFileTransfer } from "@/hooks/useFileTransfer";

interface HarnessProps {
  run: (opts: TransferOptions) => Promise<string>;
  onSuccess?: (data: string) => void;
  onCancel?: () => void;
}

function Harness({ run, onSuccess, onCancel }: HarnessProps) {
  const t = useFileTransfer({ mutationFn: run, onSuccess, onCancel });
  return (
    <div>
      <button onClick={() => t.submit()}>submit</button>
      <button onClick={t.cancel}>cancel</button>
      <button onClick={t.reset}>reset</button>
      <span data-testid="active">{String(t.active)}</span>
      <span data-testid="error">{t.error ?? ""}</span>
      <span data-testid="data">{t.data ?? ""}</span>
      <span data-testid="state">{t.state ? JSON.stringify(t.state) : ""}</span>
    </div>
  );
}

function renderHarness(props: HarnessProps) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <Harness {...props} />
    </QueryClientProvider>,
  );
}

const state = () => JSON.parse(screen.getByTestId("state").textContent || "null");

beforeEach(() => vi.useRealTimers());
afterEach(() => vi.useRealTimers());

describe("useFileTransfer", () => {
  it("runs the transfer once however fast submit is called", async () => {
    const run = vi.fn(() => new Promise<string>(() => {}));
    renderHarness({ run });
    const submit = screen.getByText("submit");

    // All in one task, before any re-render — only the synchronous ref guard
    // can stop the second and third.
    fireEvent.click(submit);
    fireEvent.click(submit);
    fireEvent.click(submit);

    await waitFor(() => expect(run).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("active").textContent).toBe("true");
  });

  it("allows a second transfer once the first has settled", async () => {
    const run = vi.fn(() => Promise.resolve("ok"));
    renderHarness({ run });
    fireEvent.click(screen.getByText("submit"));
    await waitFor(() => expect(screen.getByTestId("active").textContent).toBe("false"));
    fireEvent.click(screen.getByText("submit"));
    await waitFor(() => expect(run).toHaveBeenCalledTimes(2));
  });

  it("reports bytes, and withholds a rate until enough time has passed", async () => {
    let report: ((p: { loaded: number; total: number | null }) => void) | undefined;
    renderHarness({
      run: (o) => {
        report = o.onProgress!;
        return new Promise<string>(() => {});
      },
    });
    fireEvent.click(screen.getByText("submit"));
    await waitFor(() => expect(report).toBeDefined());

    // First event: elapsed is ~0, so any derived rate would be noise.
    act(() => report!({ loaded: 1_000_000, total: 8_000_000 }));
    expect(state()).toMatchObject({ loaded: 1_000_000, total: 8_000_000, rate_bps: null });

    const realNow = Date.now;
    vi.spyOn(Date, "now").mockReturnValue(realNow() + 2_000);
    act(() => report!({ loaded: 4_000_000, total: 8_000_000 }));
    const s = state();
    expect(s.rate_bps).toBeGreaterThan(0);
    // Half of an 8 MB transfer left at ~2 MB/s.
    expect(s.eta_s).toBeGreaterThan(0);
    vi.mocked(Date.now).mockRestore();
  });

  it("treats an abort as a cancellation, not a failure", async () => {
    const onCancel = vi.fn();
    let signal: AbortSignal | undefined;
    renderHarness({
      onCancel,
      run: (o) => {
        signal = o.signal;
        o.onProgress!({ loaded: 10, total: 100 });
        return new Promise<string>((_res, rej) =>
          o.signal!.addEventListener("abort", () =>
            rej(new DOMException("Aborted", "AbortError")),
          ),
        );
      },
    });
    fireEvent.click(screen.getByText("submit"));
    await waitFor(() => expect(signal).toBeDefined());

    fireEvent.click(screen.getByText("cancel"));
    expect(signal!.aborted).toBe(true);
    await waitFor(() => expect(onCancel).toHaveBeenCalledTimes(1));
    // No error surfaced, no stale progress row left behind, ready to retry.
    expect(screen.getByTestId("error").textContent).toBe("");
    expect(screen.getByTestId("state").textContent).toBe("");
    expect(screen.getByTestId("active").textContent).toBe("false");
  });

  it("words a transport failure differently from a server rejection", async () => {
    // ApiError status 0 is the XHR "never reached the server" sentinel — the
    // analyst needs to know the bytes stopped, not that the server said no.
    renderHarness({ run: () => Promise.reject(new ApiError(0, "Network error")) });
    fireEvent.click(screen.getByText("submit"));
    await waitFor(() =>
      expect(screen.getByTestId("error").textContent).toBe(
        "The transfer was interrupted before it finished.",
      ),
    );
  });

  it("surfaces a server error message as-is", async () => {
    renderHarness({ run: () => Promise.reject(new ApiError(413, "Upload exceeds 10 GiB")) });
    fireEvent.click(screen.getByText("submit"));
    await waitFor(() =>
      expect(screen.getByTestId("error").textContent).toBe("Upload exceeds 10 GiB"),
    );
  });

  it("clears result, error and progress on reset", async () => {
    renderHarness({ run: () => Promise.resolve("done") });
    fireEvent.click(screen.getByText("submit"));
    await waitFor(() => expect(screen.getByTestId("data").textContent).toBe("done"));

    fireEvent.click(screen.getByText("reset"));
    await waitFor(() => expect(screen.getByTestId("data").textContent).toBe(""));
    expect(screen.getByTestId("error").textContent).toBe("");
  });
});
