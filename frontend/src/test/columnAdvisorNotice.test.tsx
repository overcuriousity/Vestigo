/**
 * The column-suggestion disclosure (issue #213): shown once per user, names
 * what leaves the machine, and is the only place the LLM path gets enabled.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ColumnAdvisorNotice } from "@/components/explorer/ColumnAdvisorNotice";
import { useAuthStore } from "@/stores/auth";
import type { User } from "@/api/types";

const updatePreferences = vi.fn();
const updateSettings = vi.fn();
const getSettings = vi.fn();

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

vi.mock("@/api/auth", () => ({
  authApi: { updatePreferences: (patch: Record<string, unknown>) => updatePreferences(patch) },
}));

vi.mock("@/api/settings", () => ({
  settingsApi: {
    get: () => getSettings(),
    update: (values: Record<string, unknown>) => updateSettings(values),
  },
}));

function user(over: Partial<User> = {}): User {
  return {
    id: "u1",
    username: "alice",
    display_name: null,
    email: null,
    is_admin: true,
    is_active: true,
    must_change_password: false,
    auth_provider: "local",
    onboarding_completed: true,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    last_login_at: null,
    preferences: null,
    ...over,
  };
}

function settingsPayload(source: "env" | "db" | "default", editable: boolean) {
  return {
    groups: [],
    secrets_mode: "db" as const,
    settings: [
      {
        field: "column_recommend_mode",
        group: "explorer",
        label: "Suggest event-grid columns",
        help: "",
        kind: "choice" as const,
        nullable: false,
        constraints: {},
        choices: ["auto", "heuristic", "off"],
        default: "heuristic",
        source,
        env_var: "VESTIGO_COLUMN_RECOMMEND_MODE",
        env_only: false,
        restart_required: false,
        subsystem: null,
        managed_by: null,
        editable,
        value: "heuristic",
      },
    ],
  };
}

function renderNotice(mode: "auto" | "heuristic" | "off" = "heuristic") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ColumnAdvisorNotice mode={mode} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  updatePreferences.mockResolvedValue(user({ preferences: { column_advisor_notice_ack: true } }));
  updateSettings.mockResolvedValue(settingsPayload("db", true));
  getSettings.mockResolvedValue(settingsPayload("default", true));
  useAuthStore.setState({ user: user() });
});

describe("ColumnAdvisorNotice", () => {
  it("names what is sent, where, and to which model", async () => {
    renderNotice();

    expect(await screen.findByText("AI column suggestions")).toBeInTheDocument();
    expect(screen.getByText(/three real sample values per field/i)).toBeInTheDocument();
    expect(await screen.findByText("http://10.0.0.4:8000/v1")).toBeInTheDocument();
    expect(screen.getByText(/qwen3-coder/)).toBeInTheDocument();
  });

  it("lets an admin enable the LLM path, and records the acknowledgement", async () => {
    renderNotice();

    fireEvent.click(await screen.findByRole("button", { name: /enable/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith({ column_recommend_mode: "auto" }),
    );
    expect(updatePreferences).toHaveBeenCalledWith({ column_advisor_notice_ack: true });
  });

  it("acknowledges without changing the setting when statistics-only is kept", async () => {
    renderNotice();

    fireEvent.click(await screen.findByRole("button", { name: /keep statistics-only/i }));

    await waitFor(() =>
      expect(updatePreferences).toHaveBeenCalledWith({ column_advisor_notice_ack: true }),
    );
    expect(updateSettings).not.toHaveBeenCalled();
  });

  it("offers a non-admin no way to change the instance setting", async () => {
    useAuthStore.setState({ user: user({ is_admin: false }) });
    renderNotice();

    expect(await screen.findByRole("button", { name: /got it/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^enable$/i })).not.toBeInTheDocument();
    expect(screen.getByText(/an administrator controls this/i)).toBeInTheDocument();
    expect(getSettings).not.toHaveBeenCalled();
  });

  it("explains a pinned setting instead of offering to change it", async () => {
    getSettings.mockResolvedValue(settingsPayload("env", false));
    renderNotice();

    expect(await screen.findByText(/VESTIGO_COLUMN_RECOMMEND_MODE/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^enable$/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /got it/i })).toBeInTheDocument();
  });

  it("stays closed for a user who has already seen it", () => {
    useAuthStore.setState({
      user: user({ preferences: { column_advisor_notice_ack: true } }),
    });
    renderNotice();

    expect(screen.queryByText("AI column suggestions")).not.toBeInTheDocument();
  });

  it("says nothing when suggestions are switched off entirely", () => {
    renderNotice("off");

    expect(screen.queryByText("AI column suggestions")).not.toBeInTheDocument();
  });
});
