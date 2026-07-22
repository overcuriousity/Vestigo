/**
 * Admin agent settings: the model field.
 *
 * Free text was the only option, which meant typing a model id exactly right
 * from memory. It is now a dropdown fed by the endpoint's own /models listing
 * — but only when that listing actually returns something. No credentials, an
 * unreachable endpoint, or an endpoint that serves no listing all fall back to
 * free text, which is also reachable on demand for models a listing omits.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AdminAgentPage } from "@/pages/admin/AdminAgentPage";

const getAgentSettingsMock = vi.fn();
const listAgentModelsMock = vi.fn();

vi.mock("@/api/admin", () => ({
  adminApi: {
    getAgentSettings: (...a: unknown[]) => getAgentSettingsMock(...a),
    listAgentModels: (...a: unknown[]) => listAgentModelsMock(...a),
    putAgentSettings: vi.fn(),
  },
}));

vi.mock("@/api/health", () => ({ healthApi: { check: vi.fn() } }));

function settings(over: Record<string, unknown> = {}) {
  return {
    effective: {
      model: "",
      provider: "openai",
      api_base_url: "http://llm.example/v1",
      api_key_set: true,
      user_agent: null,
      extra_headers: null,
      max_turns: 15,
      reasoning_effort: "off",
      context_window: null,
      disabled_tools: [],
      ...over,
    },
    sources: {},
    env_vars: {},
    secret_mode: "db",
    tools: [],
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AdminAgentPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers({ shouldAdvanceTime: true });
  getAgentSettingsMock.mockResolvedValue(settings());
  listAgentModelsMock.mockResolvedValue({ models: [] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("admin agent model picker", () => {
  it("lists models from the endpoint once credentials are present", async () => {
    listAgentModelsMock.mockResolvedValue({ models: ["gpt-4o", "gpt-4o-mini"] });
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
    getAgentSettingsMock.mockResolvedValue(
      settings({ api_base_url: "", api_key_set: false }),
    );
    renderPage();

    await screen.findByText("AI agent");
    vi.advanceTimersByTime(700);

    await screen.findByText(/Set the API base URL and key/);
    expect(listAgentModelsMock).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText("gpt-4o-mini")).toBeTruthy();
  });

  it("keeps an env-pinned model read-only and never lists", async () => {
    listAgentModelsMock.mockResolvedValue({ models: ["gpt-4o"] });
    getAgentSettingsMock.mockResolvedValue({
      ...settings({ model: "pinned-model" }),
      sources: { model: "env" },
      env_vars: { model: "VESTIGO_AGENT_MODEL" },
    });
    renderPage();

    await screen.findByText(/pinned by VESTIGO_AGENT_MODEL/);
    const input = screen.getByPlaceholderText("gpt-4o-mini") as HTMLInputElement;
    expect(input.disabled).toBe(true);
    expect(input.value).toBe("pinned-model");

    // Nothing to pick, so the endpoint is left alone.
    vi.advanceTimersByTime(700);
    expect(listAgentModelsMock).not.toHaveBeenCalled();
  });
});
