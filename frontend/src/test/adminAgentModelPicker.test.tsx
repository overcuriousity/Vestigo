/**
 * The agent's model field, now a control on the settings page rather than a
 * page of its own (migration 0033 retired the separate agent settings row and
 * its endpoint).
 *
 * Free text was the only option, which meant typing a model id exactly right
 * from memory. It is a dropdown fed by the endpoint's own /models listing —
 * but only when that listing actually returns something. No credentials, an
 * unreachable endpoint, or an endpoint that serves no listing all fall back to
 * free text, which is also reachable on demand for models a listing omits.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AdminSettingsPage } from "@/pages/admin/AdminSettingsPage";
import { TooltipProvider } from "@/components/ui/Tooltip";
import type { InstanceSetting } from "@/api/settings";

const getMock = vi.fn();
const updateMock = vi.fn();
const listAgentModelsMock = vi.fn();

vi.mock("@/api/settings", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  settingsApi: {
    get: () => getMock(),
    update: (values: Record<string, unknown>) => updateMock(values),
  },
}));

vi.mock("@/api/admin", () => ({
  adminApi: {
    listAgentModels: (...a: unknown[]) => listAgentModelsMock(...a),
    probeAgent: vi.fn().mockResolvedValue({ available: true }),
  },
}));

function setting(
  over: Partial<InstanceSetting> & { field: string },
): InstanceSetting {
  return {
    group: "agent",
    label: over.field,
    help: "",
    kind: "str",
    nullable: true,
    constraints: {},
    choices: null,
    default: null,
    source: "default",
    env_var: `VESTIGO_${over.field.toUpperCase()}`,
    env_only: false,
    restart_required: false,
    subsystem: "agent",
    editable: true,
    value: null,
    ...over,
  } as InstanceSetting;
}

function payload(over: Partial<InstanceSetting>[] = []) {
  const base = [
    setting({ field: "agent_model", label: "Model" }),
    setting({
      field: "agent_api_base_url",
      label: "Endpoint URL",
      value: "http://llm.example/v1",
    }),
    setting({
      field: "agent_api_key",
      label: "API key",
      kind: "secret",
      value: null,
      value_set: true,
    }),
    setting({
      field: "agent_provider",
      label: "Wire protocol",
      kind: "choice",
      choices: ["openai", "anthropic"],
      value: "openai",
      nullable: false,
    }),
  ];
  const settings = base.map((s) => {
    const patch = over.find((o) => o.field === s.field);
    return patch ? { ...s, ...patch } : s;
  });
  return {
    groups: [
      {
        key: "agent",
        label: "AI agent",
        description: "LLM endpoint and tool policy.",
      },
    ],
    settings,
    secrets_mode: "db" as const,
    agent: { tools: [], warnings: [] },
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <AdminSettingsPage />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers({ shouldAdvanceTime: true });
  getMock.mockResolvedValue(payload());
  updateMock.mockResolvedValue(payload());
  listAgentModelsMock.mockResolvedValue({ models: [] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("admin agent model picker", () => {
  it("lists models from the endpoint once credentials are present", async () => {
    listAgentModelsMock.mockResolvedValue({
      models: ["gpt-4o", "gpt-4o-mini"],
    });
    renderPage();

    await screen.findByText("AI agent");
    vi.advanceTimersByTime(700); // debounce
    await waitFor(() => expect(listAgentModelsMock).toHaveBeenCalled());

    // The stored key is never in the browser, so only the base URL is sent —
    // the backend falls back to what is already configured.
    expect(listAgentModelsMock).toHaveBeenCalledWith({
      api_base_url: "http://llm.example/v1",
      provider: "openai",
    });
    await screen.findByText(/2 models from the endpoint/);
    // Dropdown, not the free-text input.
    expect(screen.queryByPlaceholderText("gpt-4o-mini")).toBeNull();
    expect(screen.getByText("Select model")).toBeTruthy();
  });

  it("falls back to free text when the endpoint lists nothing", async () => {
    listAgentModelsMock.mockResolvedValue({ models: [] });
    renderPage();

    await screen.findByText("AI agent");
    vi.advanceTimersByTime(700);
    await waitFor(() => expect(listAgentModelsMock).toHaveBeenCalled());

    await screen.findByText(/endpoint listed no models/);
    expect(screen.getByPlaceholderText("gpt-4o-mini")).toBeTruthy();
  });

  it("does not call the endpoint without a base URL", async () => {
    getMock.mockResolvedValue(
      payload([
        { field: "agent_api_base_url", value: null },
        { field: "agent_api_key", value_set: false },
      ]),
    );
    renderPage();

    await screen.findByText("AI agent");
    vi.advanceTimersByTime(700);

    await screen.findByText(/Set the endpoint URL and key/);
    expect(listAgentModelsMock).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText("gpt-4o-mini")).toBeTruthy();
  });

  it("keeps an env-pinned model read-only and never lists", async () => {
    listAgentModelsMock.mockResolvedValue({ models: ["gpt-4o"] });
    getMock.mockResolvedValue(
      payload([
        {
          field: "agent_model",
          value: "pinned-model",
          source: "env",
          editable: false,
        },
      ]),
    );
    renderPage();

    await screen.findByText("VESTIGO_AGENT_MODEL");
    const input = screen.getByPlaceholderText(
      "gpt-4o-mini",
    ) as HTMLInputElement;
    expect(input.disabled).toBe(true);
    expect(input.value).toBe("pinned-model");

    // Nothing to pick, so the endpoint is left alone.
    vi.advanceTimersByTime(700);
    expect(listAgentModelsMock).not.toHaveBeenCalled();
  });
});

describe("admin agent settings warnings", () => {
  it("renders the backend's guard-rail warnings above the agent group", async () => {
    getMock.mockResolvedValue({
      ...payload(),
      agent: {
        tools: [],
        warnings: [
          "tool_fidelity is 'full' but context_window is 65536 tokens (< 100000); " +
            "a single tool sweep can fill the window before history and an answer fit.",
        ],
      },
    });
    renderPage();

    expect(await screen.findByText(/tool_fidelity is 'full'/)).toBeTruthy();
  });

  it("renders nothing extra when there are no warnings", async () => {
    renderPage();

    await screen.findByText("AI agent");
    expect(screen.queryByText(/tool_fidelity is 'full'/)).toBeNull();
  });
});
