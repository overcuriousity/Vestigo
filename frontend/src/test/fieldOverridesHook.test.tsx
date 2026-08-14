/**
 * The write path behind a field declaration.
 *
 * Two hook instances are mounted at once — the method sheet's picker and the
 * Tools sheet's summary — and they write the same object with a full-replace
 * PATCH. Serializing each against itself only is what lets one drop the
 * other's in-flight edit from both the timeline and the audit row's
 * previous/new pair, so the ordering these pin is cross-instance.
 *
 * The other half is failure: this is shared, audited state that the analyst
 * believes the rest of the case now inherits, and the chip returning to the
 * server's answer on the next render reads as "nothing happened" rather than
 * "this was not stored".
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useFieldOverrides } from "@/hooks/useFieldOverrides";

const server = vi.hoisted(() => ({
  overrides: {} as Record<string, Record<string, boolean>>,
  calls: [] as Record<string, Record<string, boolean>>[],
  gate: null as Promise<void> | null,
  fail: null as string | null,
}));

vi.mock("@/api/timelines", () => ({
  timelinesApi: {
    get: async () => ({ id: "t1", case_id: "c1", field_overrides: server.overrides }),
    patchFieldOverrides: async (
      _c: string,
      _t: string,
      next: Record<string, Record<string, boolean>>,
    ) => {
      if (server.gate) await server.gate;
      if (server.fail) throw new Error(server.fail);
      server.calls.push(next);
      server.overrides = next;
      return { id: "t1", case_id: "c1", field_overrides: next };
    },
  },
}));

vi.mock("@/api/cases", () => ({
  casesApi: { get: async () => ({ id: "c1", access_level: "contribute" }) },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const render = () => renderHook(() => useFieldOverrides("c1", "t1"), { wrapper });

describe("writing a field declaration", () => {
  beforeEach(() => {
    server.overrides = {};
    server.calls = [];
    server.gate = null;
    server.fail = null;
  });

  it("keeps a second hook instance from dropping the first's in-flight edit", async () => {
    // The sheet mounts the picker and the Tools summary at once, and an analyst
    // can declare in one and act in the other before the PATCH lands.
    const picker = render();
    const tools = render();
    await waitFor(() => expect(picker.result.current.canEdit).toBe(true));
    await waitFor(() => expect(tools.result.current.canEdit).toBe(true));

    let release: () => void = () => {};
    server.gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    act(() => picker.result.current.declare("numeric_range", "attr:status_code", false));
    act(() => tools.result.current.declare("value_novelty", "attr:session", true));
    release();

    await waitFor(() => expect(server.calls).toHaveLength(2));
    expect(server.calls[1]).toEqual({
      numeric_range: { "attr:status_code": false },
      value_novelty: { "attr:session": true },
    });
  });

  it("says so when the write did not land", async () => {
    server.fail = "field_overrides: unknown method";
    const { result } = render();
    await waitFor(() => expect(result.current.canEdit).toBe(true));

    act(() => result.current.declare("numeric_range", "attr:status_code", false));

    await waitFor(() => expect(result.current.saveError).toBe("field_overrides: unknown method"));
    expect(server.calls).toHaveLength(0);
  });

  it("reports nothing while writes succeed", async () => {
    const { result } = render();
    await waitFor(() => expect(result.current.canEdit).toBe(true));
    act(() => result.current.declare("numeric_range", "attr:status_code", false));
    await waitFor(() => expect(server.calls).toHaveLength(1));
    expect(result.current.saveError).toBeNull();
  });
});
