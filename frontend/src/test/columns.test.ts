/**
 * Column precedence (issue #213): the analyst's own choice, then the
 * timeline's server-side suggestion, then the built-in defaults.
 */
import { describe, it, expect } from "vitest";
import type { RecommendedColumns } from "@/api/types";
import {
  hasSuggestion,
  isSuggesting,
  resolveVisibleColumns,
  suggestedColumns,
  STALE_SUGGESTION_MS,
} from "@/lib/columns";
import { DEFAULT_COLUMNS } from "@/stores/ui";

function suggestion(over: Partial<RecommendedColumns> = {}): RecommendedColumns {
  return {
    status: "ok",
    columns: ["timestamp", "user", "src_ip", "status"],
    reasons: { user: "in 2/2 sources" },
    method: "heuristic",
    model: null,
    source_ids: ["s1"],
    // "just now": a `running` claim is only believed for STALE_SUGGESTION_MS.
    generated_at: new Date().toISOString(),
    job_id: null,
    ...over,
  };
}

describe("resolveVisibleColumns", () => {
  it("falls back to the built-in defaults with no stored choice and no suggestion", () => {
    expect(resolveVisibleColumns(undefined, null)).toEqual(DEFAULT_COLUMNS);
    expect(resolveVisibleColumns(undefined, undefined)).toEqual(DEFAULT_COLUMNS);
  });

  it("applies the suggestion when the analyst has not chosen columns", () => {
    expect(resolveVisibleColumns(undefined, suggestion())).toEqual([
      "timestamp",
      "user",
      "src_ip",
      "status",
    ]);
  });

  it("lets the analyst's own choice win over the suggestion", () => {
    expect(resolveVisibleColumns(["message"], suggestion())).toEqual(["message"]);
  });

  it("respects a deliberately empty stored choice rather than re-suggesting", () => {
    expect(resolveVisibleColumns([], suggestion())).toEqual([]);
  });

  it("ignores a first-ever suggestion that is still being computed", () => {
    expect(
      resolveVisibleColumns(undefined, suggestion({ status: "running", columns: [] })),
    ).toEqual(DEFAULT_COLUMNS);
  });

  it("keeps showing the previous answer while a recompute runs", () => {
    // The job carries the old columns into its `running` placeholder so the
    // grid does not fall back to the defaults and re-lay out twice.
    expect(resolveVisibleColumns(undefined, suggestion({ status: "running" }))).toEqual([
      "timestamp",
      "user",
      "src_ip",
      "status",
    ]);
  });

  it("ignores an 'insufficient' verdict", () => {
    expect(
      resolveVisibleColumns(undefined, suggestion({ status: "insufficient", columns: [] })),
    ).toEqual(DEFAULT_COLUMNS);
  });

  it("sanitizes a suggestion the same way a stored selection is sanitized", () => {
    // `source` is a retired id that remaps; `_bogus` is a grid-internal id
    // that isn't a real column and must not reach the grid.
    const resolved = resolveVisibleColumns(
      undefined,
      suggestion({ columns: ["timestamp", "source", "_bogus", "user", "user"] }),
    );
    expect(resolved).toEqual(["timestamp", "artifact", "user"]);
  });

  it("falls back when nothing survives sanitization", () => {
    expect(resolveVisibleColumns(undefined, suggestion({ columns: ["_bogus"] }))).toEqual(
      DEFAULT_COLUMNS,
    );
  });
});

describe("suggestion predicates", () => {
  it("hasSuggestion is true whenever there are columns to show", () => {
    expect(hasSuggestion(suggestion())).toBe(true);
    // A recompute in flight still has last time's answer to render.
    expect(hasSuggestion(suggestion({ status: "running" }))).toBe(true);
    expect(hasSuggestion(suggestion({ columns: [] }))).toBe(false);
    expect(hasSuggestion(suggestion({ status: "insufficient", columns: [] }))).toBe(false);
    expect(hasSuggestion(null)).toBe(false);
  });

  it("isSuggesting tracks the running placeholder", () => {
    expect(isSuggesting(suggestion({ status: "running" }))).toBe(true);
    expect(isSuggesting(suggestion())).toBe(false);
    expect(isSuggesting(undefined)).toBe(false);
  });

  it("stops believing a running claim once it is stale", () => {
    // Server-side jobs are in-memory: one that died leaves `running` behind
    // until the next restart sweeps it. A long-lived tab must stop polling
    // rather than wait for a job that is never coming back.
    const old = new Date(Date.now() - STALE_SUGGESTION_MS - 1000).toISOString();
    expect(isSuggesting(suggestion({ status: "running", generated_at: old }))).toBe(false);
    // ...and it still renders the columns that placeholder carries.
    expect(
      resolveVisibleColumns(undefined, suggestion({ status: "running", generated_at: old })),
    ).toEqual(["timestamp", "user", "src_ip", "status"]);
  });

  it("treats an unparseable timestamp as not running", () => {
    expect(isSuggesting(suggestion({ status: "running", generated_at: "nonsense" }))).toBe(
      false,
    );
  });

  it("suggestedColumns is null when there is nothing to apply", () => {
    expect(suggestedColumns(null)).toBeNull();
    expect(suggestedColumns(suggestion({ status: "insufficient", columns: [] }))).toBeNull();
  });
});
