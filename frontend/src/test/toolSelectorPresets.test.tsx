/**
 * ToolSelectorPopover presets (A13). Tool schemas are resent with every model
 * request, so "Core" is a context-reclaiming control, not cosmetics — these
 * tests pin what each preset denies, since getting the deny list inverted
 * would silently hand a small model the full catalog (or nothing).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ToolSelectorPopover } from "@/components/agent/ToolSelector";
import { CARD_TOOLS } from "@/components/agent/proposalTools";
import type { AgentToolInfo } from "@/api/agent";

const getInfoMock = vi.fn();

vi.mock("@/api/agent", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/agent")>("@/api/agent");
  return {
    ...actual,
    agentApi: {
      getInfo: (...a: unknown[]) => getInfoMock(...a),
      updatePreferences: vi.fn().mockResolvedValue({}),
    },
  };
});

const tool = (
  name: string,
  extra: Partial<AgentToolInfo> = {},
): AgentToolInfo => ({
  name,
  description: `${name} does a thing`,
  embeddings_gated: false,
  requires_conversation: false,
  admin_disabled: false,
  ...extra,
});

const TOOLS: AgentToolInfo[] = [
  tool("search_events", { tier: "core" }),
  tool("field_terms", { tier: "core" }),
  tool("field_scatter", { tier: "extended" }),
  tool("get_sigma_run", { tier: "extended" }),
  tool("locked_tool", { tier: "extended", admin_disabled: true }),
];

function renderSelector(onChange = vi.fn(), disabled: string[] = []) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ToolSelectorPopover
        disabledTools={disabled}
        onChange={onChange}
        seedFromDefaults={false}
      />
    </QueryClientProvider>,
  );
  return onChange;
}

async function openPopover() {
  fireEvent.click(screen.getByRole("button", { name: /Tools/ }));
  await waitFor(() => expect(screen.getByText("Core")).toBeInTheDocument());
}

describe("ToolSelectorPopover presets", () => {
  beforeEach(() => {
    getInfoMock.mockReset();
    getInfoMock.mockResolvedValue({
      model: "m",
      provider: "openai",
      api_base_url: "http://x",
      context_window: 32000,
      tools: TOOLS,
      user_disabled_tools: [],
    });
  });

  it("Core denies exactly the extended tier", async () => {
    const onChange = renderSelector();
    await openPopover();
    fireEvent.click(screen.getByText("Core"));
    expect(onChange).toHaveBeenCalledWith(["field_scatter", "get_sigma_run"]);
  });

  it("Core does not list admin-disabled tools, which are already denied server-side", async () => {
    const onChange = renderSelector();
    await openPopover();
    fireEvent.click(screen.getByText("Core"));
    expect(onChange.mock.calls[0][0]).not.toContain("locked_tool");
  });

  it("All clears the deny list", async () => {
    const onChange = renderSelector(vi.fn(), ["field_scatter"]);
    await openPopover();
    fireEvent.click(screen.getByText("All"));
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("offers no preset that disables everything", async () => {
    // An agent with no tools cannot measure anything, so every claim it made
    // would breach the evidence rule. Individual toggles still allow it.
    renderSelector();
    await openPopover();
    expect(screen.queryByText("None")).not.toBeInTheDocument();
  });

  // Disabling a card tool removes its proposal cards from the chat, which is
  // a workflow change and not just narrower coverage. The warning used to be
  // its own hand-maintained allowlist and covered only two of the four tools,
  // so propose_story_block and propose_chart could be switched off silently.
  // It now derives from CARD_TOOLS — parameterized here so a tool added there
  // has to carry its warning.
  describe.each(Object.entries(CARD_TOOLS))("card tool %s", (name, cardName) => {
    beforeEach(() => {
      getInfoMock.mockResolvedValue({
        model: "m",
        provider: "openai",
        api_base_url: "http://x",
        context_window: 32000,
        tools: [...TOOLS, tool(name, { tier: "core" })],
        user_disabled_tools: [],
      });
    });

    it("warns that disabling it removes its cards", async () => {
      renderSelector(vi.fn(), [name]);
      await openPopover();
      expect(
        screen.getByText(new RegExp(`removes the ${cardName} proposal cards`)),
      ).toBeInTheDocument();
    });

    it("does not warn while it is enabled", async () => {
      renderSelector();
      await openPopover();
      expect(screen.queryByText(/proposal cards/)).not.toBeInTheDocument();
    });
  });

  it("does not warn for a tool that renders no card", async () => {
    renderSelector(vi.fn(), ["field_scatter"]);
    await openPopover();
    expect(screen.queryByText(/proposal cards/)).not.toBeInTheDocument();
  });

  it("hides Core when the backend does not tier its catalog", async () => {
    getInfoMock.mockResolvedValue({
      model: "m",
      provider: "openai",
      api_base_url: "http://x",
      context_window: 32000,
      tools: TOOLS.map(({ tier: _tier, ...t }) => t),
      user_disabled_tools: [],
    });
    renderSelector();
    fireEvent.click(screen.getByRole("button", { name: /Tools/ }));
    await waitFor(() => expect(screen.getByText("All")).toBeInTheDocument());
    expect(screen.queryByText("Core")).not.toBeInTheDocument();
  });
});
