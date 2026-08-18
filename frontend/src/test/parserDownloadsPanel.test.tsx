/**
 * The converter downloads panel's copy-paste prompts: a failed load must say
 * so and offer a retry — the query never refetches on its own, so silently
 * disabled buttons would stay disabled for the whole session.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ParserDownloadsPanel } from "@/components/sources/ParserDownloadsPanel";
import { TooltipProvider } from "@/components/ui/Tooltip";

const promptsMock = vi.fn();

vi.mock("@/api/converters", () => ({
  convertersApi: {
    list: async () => ({
      upstream: "u",
      commit: "c",
      version: "v",
      license: "l",
      converters: [],
    }),
    prompts: (...a: unknown[]) => promptsMock(...a),
    downloadUrl: (n: string) => `/api/converters/${n}`,
  },
}));

beforeEach(() => promptsMock.mockReset());

describe("ParserDownloadsPanel — LLM prompts", () => {
  it("explains a failed prompt load and retries on request", async () => {
    promptsMock.mockRejectedValueOnce(new Error("HTTP 500"));
    promptsMock.mockResolvedValueOnce({ parquet: "PARQUET PROMPT", csv: "CSV PROMPT" });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <ParserDownloadsPanel />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/Could not load the prompts: HTTP 500/)).toBeTruthy();
    const copy = screen.getByRole("button", { name: /Copy LLM prompt/ }) as HTMLButtonElement;
    expect(copy.disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(promptsMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(copy.disabled).toBe(false));
    expect(screen.queryByText(/Could not load the prompts/)).toBeNull();
  });
});
