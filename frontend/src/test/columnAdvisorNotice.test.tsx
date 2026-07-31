/**
 * The column-suggestion disclosure (issue #213): opened by "Suggest with AI",
 * names what leaves the machine, and sends nothing until it is confirmed.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ColumnAdvisorNotice } from "@/components/explorer/ColumnAdvisorNotice";
import { hasColumnAdvisorOptIn } from "@/lib/columns";

vi.mock("@/api/agent", () => ({
  agentApi: {
    getInfo: vi.fn().mockResolvedValue({
      model: "qwen3-coder",
      provider: "openai",
      api_base_url: "http://10.0.0.4:8000/v1",
      context_window: null,
      tools: [],
      user_disabled_tools: [],
    }),
  },
}));

function renderNotice(props: Partial<React.ComponentProps<typeof ColumnAdvisorNotice>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onConfirm = vi.fn();
  const onOpenChange = vi.fn();
  const view = render(
    <QueryClientProvider client={qc}>
      <ColumnAdvisorNotice
        open
        onOpenChange={onOpenChange}
        onConfirm={onConfirm}
        {...props}
      />
    </QueryClientProvider>,
  );
  return { ...view, onConfirm, onOpenChange };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ColumnAdvisorNotice", () => {
  it("names what is sent, where, and to which model", async () => {
    renderNotice();

    expect(await screen.findByText("Suggest columns with AI")).toBeInTheDocument();
    expect(screen.getByText(/three real sample values per field/i)).toBeInTheDocument();
    expect(await screen.findByText("http://10.0.0.4:8000/v1")).toBeInTheDocument();
    expect(screen.getByText(/qwen3-coder/)).toBeInTheDocument();
  });

  it("says the result is shared and audited, and that the opt-in is per timeline", async () => {
    renderNotice();

    expect(await screen.findByText(/shared with everyone who can see this timeline/i))
      .toBeInTheDocument();
    expect(screen.getByText(/asked again on the next one/i)).toBeInTheDocument();
  });

  it("sends nothing when it is cancelled", async () => {
    const { onConfirm, onOpenChange } = renderNotice();

    fireEvent.click(await screen.findByRole("button", { name: /cancel/i }));

    expect(onConfirm).not.toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("confirms only on the explicit send action", async () => {
    const { onConfirm } = renderNotice();

    fireEvent.click(await screen.findByRole("button", { name: /send and suggest/i }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("reports a failed opt-in as nothing having been sent", async () => {
    renderNotice({ error: true });

    expect(await screen.findByText(/nothing was sent/i)).toBeInTheDocument();
  });

  it("is not rendered at all when closed", () => {
    renderNotice({ open: false });

    expect(screen.queryByText("Suggest columns with AI")).not.toBeInTheDocument();
  });
});

describe("hasColumnAdvisorOptIn", () => {
  it("is per timeline, not per user", () => {
    const prefs = { column_advisor_optin: { "tl-1": true } };

    expect(hasColumnAdvisorOptIn(prefs, "tl-1")).toBe(true);
    expect(hasColumnAdvisorOptIn(prefs, "tl-2")).toBe(false);
  });

  it("treats a missing, malformed or falsy entry as not opted in", () => {
    expect(hasColumnAdvisorOptIn(null, "tl-1")).toBe(false);
    expect(hasColumnAdvisorOptIn({}, "tl-1")).toBe(false);
    expect(hasColumnAdvisorOptIn({ column_advisor_optin: "yes" }, "tl-1")).toBe(false);
    expect(hasColumnAdvisorOptIn({ column_advisor_optin: { "tl-1": false } }, "tl-1")).toBe(false);
  });
});
