import { AlertTriangle, Check } from "lucide-react";

import type { ScanBudget } from "@/api/types";

/** GiB with one decimal — the unit every sizing doc and log line already uses. */
function gib(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

const COPY: Record<ScanBudget["risk"], { title: string; body: (b: ScanBudget) => string }> = {
  ok: {
    title: "Scan budget fits under ClickHouse's ceiling",
    body: (b) =>
      `${gib(b.total_bytes)} of scans (${gib(b.per_query_bytes)} × ${b.concurrency}) plus ` +
      `${gib(b.cache_bytes)} of server caches under a ${gib(b.clickhouse_ceiling_bytes)} ceiling, ` +
      `leaving ${gib(b.headroom_bytes)} for background merges.`,
  },
  over_budget: {
    title: "Scans and caches exceed what ClickHouse may use",
    body: (b) =>
      `${gib(b.total_bytes)} of scans plus ${gib(b.cache_bytes)} of caches against a ` +
      `${gib(b.clickhouse_ceiling_bytes)} ceiling. Admitting a full set of scans can only end in ` +
      `a memory error or an OOM kill. Lower the scan budget, shrink the caches in ` +
      `deploy/clickhouse/memory.xml, or raise max_server_memory_usage.`,
  },
  unbounded: {
    title: "ClickHouse reports no memory ceiling of its own",
    body: () =>
      `Nothing bounds its merges, caches or allocator slack, and the kernel is the only ` +
      `backstop — it kills the server without writing anything to ClickHouse's log. Mount ` +
      `deploy/clickhouse/memory.xml and set a container memory limit. See docs/DEPLOYMENT.md ` +
      `"Resource sizing".`,
  },
};

/**
 * The `risk` verdict, where an operator is already standing when they change
 * the numbers it describes.
 *
 * It has always been on `/api/health` and in a startup log line, and a startup
 * warning is exactly what nobody reads — which is how an airgap install ran
 * unbounded for a release without anyone seeing it.
 */
export function ScanBudgetCard({ budget }: { budget: ScanBudget | undefined }) {
  if (!budget) return null;
  const copy = COPY[budget.risk];
  const bad = budget.risk !== "ok";

  return (
    <div
      role={bad ? "alert" : undefined}
      className={
        bad
          ? "mb-2 flex items-start gap-2 rounded border border-[var(--color-danger)]/40 bg-[var(--color-danger-dim)] p-3 text-xs text-[var(--color-danger)]"
          : "mb-2 flex items-start gap-2 rounded border border-[var(--color-border)] p-3 text-xs text-[var(--color-fg-muted)]"
      }
    >
      {bad ? (
        <AlertTriangle size={14} className="mt-0.5 shrink-0" />
      ) : (
        <Check size={14} className="mt-0.5 shrink-0" />
      )}
      <div className="space-y-1">
        <p className="font-medium">{copy.title}</p>
        <p>{copy.body(budget)}</p>
        <p>
          {budget.max_threads} threads per scan (
          {budget.max_threads_source === "pinned"
            ? "pinned"
            : budget.max_threads_source === "clickhouse_pinned"
              ? "pinned in ClickHouse's own profile"
              : budget.max_threads_source === "clickhouse"
                ? `from ${budget.detected_cores} cores ClickHouse reports`
                : "detection failed — fallback"}
          ), budget from {budget.source} detection.
        </p>
        <p>
          Charts have their own lane: {budget.foreground.concurrency} chart queries at{" "}
          {gib(budget.foreground.per_query_bytes)} each, never queued behind a detector sweep.
        </p>
        {budget.pending_concurrency !== null && (
          <p>
            Concurrent scans is set to {budget.pending_concurrency} but still running at{" "}
            {budget.concurrency} — it takes effect on restart, and both halves of the budget keep
            using the old value until then.
          </p>
        )}
      </div>
    </div>
  );
}
