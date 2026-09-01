import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

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

import { useMethodFocus } from "@/hooks/useMethodFocus";
import { useAuthStore } from "@/stores/auth";
import type { User } from "@/api/types";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const render = (timelineId: string) =>
  renderHook(() => useMethodFocus(timelineId), { wrapper });

function seedUser(preferences: Record<string, unknown> | null) {
  useAuthStore.getState().setUser({
    id: "u1",
    username: "analyst",
    is_admin: false,
    preferences,
  } as unknown as User);
}

beforeEach(() => {
  updatePreferencesMock.mockReset();
  updatePreferencesMock.mockImplementation(async (prefs: Record<string, unknown>) => ({
    id: "u1",
    username: "analyst",
    is_admin: false,
    preferences: prefs,
  }));
  seedUser(null);
});

/**
 * #341: the Tools sheet let an analyst run proportion shift on `http_uri`
 * alone, but closing the sheet threw that away and the sweep went back to
 * every field. The focus is per-user on purpose — `Timeline.field_overrides`
 * is the team's shared, audited declaration and must not be rewritten to tidy
 * one analyst's feed.
 */
describe("useMethodFocus", () => {
  it("has no focus until one is set", () => {
    const { result } = render("tl-1");

    expect(result.current.fieldsFor("proportion_shift")).toBeUndefined();
  });

  it("stores a focus under the timeline and method", async () => {
    const { result } = render("tl-1");

    await act(async () => {
      await result.current.setFocus("proportion_shift", ["attr:http_uri"]);
    });

    expect(updatePreferencesMock).toHaveBeenCalledWith({
      analysis_method_focus: { "tl-1": { proportion_shift: ["attr:http_uri"] } },
    });
    await waitFor(() =>
      expect(result.current.fieldsFor("proportion_shift")).toEqual(["attr:http_uri"]),
    );
  });

  it("keeps another method's focus on the same timeline", async () => {
    seedUser({
      analysis_method_focus: { "tl-1": { value_novelty: ["attr:ua"] } },
    });
    const { result } = render("tl-1");

    await act(async () => {
      await result.current.setFocus("proportion_shift", ["attr:http_uri"]);
    });

    expect(updatePreferencesMock).toHaveBeenCalledWith({
      analysis_method_focus: {
        "tl-1": { value_novelty: ["attr:ua"], proportion_shift: ["attr:http_uri"] },
      },
    });
  });

  it("clears one method without touching the rest", async () => {
    seedUser({
      analysis_method_focus: {
        "tl-1": { proportion_shift: ["attr:http_uri"], value_novelty: ["attr:ua"] },
      },
    });
    const { result } = render("tl-1");

    await act(async () => {
      await result.current.clearFocus("proportion_shift");
    });

    expect(updatePreferencesMock).toHaveBeenCalledWith({
      analysis_method_focus: { "tl-1": { value_novelty: ["attr:ua"] } },
    });
  });

  it("reads only its own timeline's focus", () => {
    seedUser({
      analysis_method_focus: { "tl-2": { proportion_shift: ["attr:http_uri"] } },
    });
    const { result } = render("tl-1");

    expect(result.current.fieldsFor("proportion_shift")).toBeUndefined();
  });

  it("treats an empty field list as no focus, so it can never hide everything", async () => {
    seedUser({ analysis_method_focus: { "tl-1": { proportion_shift: [] } } });
    const { result } = render("tl-1");

    expect(result.current.fieldsFor("proportion_shift")).toBeUndefined();
  });
});
