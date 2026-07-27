/**
 * The settings page is fully generic — it renders whatever the registry sends
 * and posts back only what the admin touched. The two things worth pinning are
 * therefore the *policy* rendering (an env-pinned field must be read-only) and
 * the patch shape (typed values, `null` to clear, untouched fields absent).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AdminSettingsPage } from "@/pages/admin/AdminSettingsPage";
import { TooltipProvider } from "@/components/ui/Tooltip";
import type { InstanceSetting } from "@/api/settings";

const getMock = vi.fn();
const updateMock = vi.fn();

vi.mock("@/api/settings", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  settingsApi: {
    get: () => getMock(),
    update: (values: Record<string, unknown>) => updateMock(values),
  },
}));

function setting(over: Partial<InstanceSetting> & { field: string }): InstanceSetting {
  return {
    group: "detectors",
    label: over.field,
    help: "",
    kind: "int",
    constraints: {},
    choices: null,
    default: 3,
    source: "default",
    env_var: `VESTIGO_${over.field.toUpperCase()}`,
    env_only: false,
    restart_required: false,
    subsystem: null,
    managed_by: null,
    editable: true,
    value: 3,
    ...over,
  } as InstanceSetting;
}

const PAYLOAD = {
  groups: [{ key: "detectors", label: "Anomaly detectors", description: "Thresholds." }],
  settings: [
    setting({ field: "stat_rarity_floor", label: "Rarity floor" }),
    setting({
      field: "audit_enabled",
      label: "Audit trail",
      kind: "bool",
      value: true,
      default: true,
    }),
    setting({
      field: "postgres_url",
      label: "PostgreSQL DSN",
      kind: "secret",
      editable: false,
      env_only: true,
      source: "env",
      value: null,
      value_set: true,
    }),
  ],
  secrets_mode: "db" as const,
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <AdminSettingsPage />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset().mockResolvedValue(PAYLOAD);
  updateMock.mockReset().mockResolvedValue({ ...PAYLOAD, applied: ["stat_rarity_floor"] });
});

describe("AdminSettingsPage", () => {
  it("renders an env-pinned field read-only with its variable name", async () => {
    renderPage();
    expect(await screen.findByText("PostgreSQL DSN")).toBeInTheDocument();
    expect(screen.getByText("VESTIGO_POSTGRES_URL")).toBeInTheDocument();
    // The pinned DSN's editor exists but cannot be typed into…
    expect(document.querySelector('input[type="password"]')).toBeDisabled();
    // …while an ordinary field on the same page stays editable.
    expect(screen.getByDisplayValue("3")).toBeEnabled();
  });

  it("sends only edited fields, typed, and nothing before Save", async () => {
    renderPage();
    const input = (await screen.findByDisplayValue("3")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "12" } });
    expect(updateMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Save changes/ }));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith({ stat_rarity_floor: 12 }));
  });

  it("clears an override with null when the field is emptied", async () => {
    getMock.mockResolvedValue({
      ...PAYLOAD,
      settings: [setting({ field: "stat_rarity_floor", value: 9, source: "db" })],
    });
    renderPage();
    const input = (await screen.findByDisplayValue("9")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /Save changes/ }));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith({ stat_rarity_floor: null }));
  });

  it("refuses a malformed value locally, naming the field", async () => {
    renderPage();
    const input = (await screen.findByDisplayValue("3")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "4.5" } });
    fireEvent.click(screen.getByRole("button", { name: /Save changes/ }));
    expect(await screen.findByText(/expected a whole number/)).toBeInTheDocument();
    expect(updateMock).not.toHaveBeenCalled();
  });
});
