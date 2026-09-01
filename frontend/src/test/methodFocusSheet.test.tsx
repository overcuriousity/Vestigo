/**
 * The Tools sheet's half of the focus (#341).
 *
 * Two failures the sheet had, both of them the same mistake in different
 * directions: the focus control and the field picker disagreeing about what
 * is actually being scanned.
 *
 * - A selection the Run button refuses could still be *persisted* into every
 *   sweep, so a one-field `value_combo` focus 422'd the rail on every load.
 * - A focused method opened its picker on "auto", describing a sweep that is
 *   not happening — and since the focus controls only appear beside a
 *   selection, that also put "Clear focus" out of reach of the sheet that set
 *   it.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InvestigateSheet } from "@/components/analysis/InvestigateSheet";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { useAuthStore } from "@/stores/auth";
import type { User } from "@/api/types";

vi.mock("@/hooks/useMethodFindings", () => ({
  useMethodFindings: () => ({ data: undefined, isLoading: false, isError: false }),
  METHOD_LIMIT: 50,
}));

vi.mock("@/api/anomalies", () => ({
  anomaliesApi: {
    fields: async () => ({
      fields: [
        { token: "attr:src_ip", distinct: 42, coverage: 0.98, kind: "categorical", recommended: true },
        { token: "attr:user", distinct: 12, coverage: 0.9, kind: "categorical", recommended: true },
      ],
    }),
    numericFields: async () => ({ fields: [] }),
  },
}));

vi.mock("@/api/timelines", () => ({
  timelinesApi: {
    get: async () => ({ id: "t1", case_id: "c1", field_overrides: {} }),
    patchFieldOverrides: async () => ({ id: "t1", case_id: "c1", field_overrides: {} }),
  },
}));

vi.mock("@/api/cases", () => ({
  casesApi: { get: async () => ({ id: "c1", access_level: "contribute" }) },
}));

const updatePreferencesMock = vi.fn();
vi.mock("@/api/auth", async () => {
  const actual = await vi.importActual<typeof import("@/api/auth")>("@/api/auth");
  return {
    ...actual,
    authApi: {
      ...actual.authApi,
      updatePreferences: (...a: unknown[]) => updatePreferencesMock(...a),
    },
  };
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <TooltipProvider>{children}</TooltipProvider>
    </QueryClientProvider>
  );
}

function seedFocus(focus: Record<string, Record<string, string[]>> | null) {
  useAuthStore.getState().setUser({
    id: "u1",
    username: "analyst",
    is_admin: false,
    preferences: focus ? { analysis_method_focus: focus } : {},
  } as unknown as User);
}

function renderMethod(methodId: string) {
  return render(
    <InvestigateSheet
      caseId="c1"
      timelineId="t1"
      railWidth={360}
      mode="method"
      methodId={methodId as never}
      onClose={() => {}}
      onRun={() => {}}
      query={{ data: undefined, isFetching: false, isError: false } as never}
    />,
    { wrapper },
  );
}

const focusButton = () => screen.getByRole("button", { name: /Focus on this selection/ });

beforeEach(() => {
  updatePreferencesMock.mockReset();
  seedFocus(null);
});

describe("focusing a method from the sheet", () => {
  it("refuses to persist a selection the run itself refuses", async () => {
    // `value_combo` combines fields, so one field is not a combination — Run
    // says so and the endpoint 422s. Focus must not be the way around that:
    // stored, the same selection would 422 on every sweep until the rail
    // strip cleared it.
    renderMethod("value_combo");

    fireEvent.click(screen.getByRole("button", { name: /fields/i }));
    await screen.findByRole("button", { name: /^src_ip/ });
    // Auto has to have settled first: toggling a chip subtracts from the
    // effective selection, so a click landing before the inventory loads adds
    // a field instead of removing one.
    await waitFor(() => expect(document.body.textContent).toContain("2 fields selected"));
    fireEvent.click(screen.getByRole("button", { name: /^src_ip/ }));

    await waitFor(() => expect(screen.getByTestId("method-knob-blocker")).toBeTruthy());
    await waitFor(() => expect(screen.getByTestId("method-knob-blocker")).toBeTruthy());
    expect(screen.getByRole("button", { name: /^Run/ }).hasAttribute("disabled")).toBe(true);
    expect(focusButton().hasAttribute("disabled")).toBe(true);

    // Putting the second field back clears the floor, and with it both controls.
    fireEvent.click(screen.getByRole("button", { name: /^src_ip/ }));
    await waitFor(() => expect(focusButton().hasAttribute("disabled")).toBe(false));
  });

  it("opens the picker on the focus already in force, so it can be cleared there", async () => {
    seedFocus({ t1: { value_novelty: ["attr:src_ip"] } });
    renderMethod("value_novelty");

    // The narrowing is stated…
    await waitFor(() => expect(screen.getByTestId("method-focus-note")).toBeTruthy());
    // …and the picker agrees with it rather than reading "auto".
    fireEvent.click(screen.getByRole("button", { name: /fields/i }));
    const chip = await screen.findByRole("button", { name: /^src_ip/ });
    await waitFor(() => expect(chip.className).toContain("--color-accent"));

    // Which is what makes the way out reachable from the sheet that set it.
    const clear = screen.getByRole("button", { name: /Clear focus/ });
    expect(clear.hasAttribute("disabled")).toBe(false);
    fireEvent.click(clear);
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalled());
    expect(updatePreferencesMock.mock.calls[0][0]).toEqual({
      analysis_method_focus: { t1: {} },
    });
  });
});
