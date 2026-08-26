/**
 * ScanBudgetCard: the `/api/health` scan-budget verdict, rendered above the
 * "scans" group on the admin settings page.
 *
 * The verdict has been served and logged since 1.15 and rendered nowhere, which
 * is how an airgap install ran with no ClickHouse ceiling for a release.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { ScanBudgetCard } from "@/components/admin/ScanBudgetCard";
import type { ScanBudget } from "@/api/types";

const base: ScanBudget = {
  risk: "ok",
  per_query_bytes: 2 * 1024 ** 3,
  total_bytes: 4 * 1024 ** 3,
  cache_bytes: 3.5 * 1024 ** 3,
  cache_breakdown: { mark_cache_size: 2 * 1024 ** 3 },
  headroom_bytes: 2 * 1024 ** 3,
  clickhouse_ceiling_bytes: 9.5 * 1024 ** 3,
  clickhouse_ceiling_is_explicit: true,
  budget_ceiling_bytes: 9.5 * 1024 ** 3,
  local_detected_bytes: 64 * 1024 ** 3,
  source: "clickhouse",
  concurrency: 2,
  pending_concurrency: null,
  max_threads: 10,
  max_threads_source: "clickhouse",
  detected_cores: 20,
};

describe("ScanBudgetCard", () => {
  it("states the resolved numbers when everything fits", () => {
    render(<ScanBudgetCard budget={base} />);
    expect(screen.getByText(/fits under ClickHouse/i)).toBeInTheDocument();
    expect(screen.getByText(/10 threads per scan/i)).toBeInTheDocument();
  });

  it("names the caches when scans plus caches exceed the ceiling", () => {
    render(<ScanBudgetCard budget={{ ...base, risk: "over_budget" }} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/caches/i);
  });

  it("says the kernel is the only backstop when ClickHouse is unbounded", () => {
    render(<ScanBudgetCard budget={{ ...base, risk: "unbounded" }} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/kernel/i);
  });

  it("distinguishes a ClickHouse-side thread pin from a resolved core count", () => {
    // A pinned `max_threads` is a thread limit, so it is honoured as written
    // and there is no core count to attribute it to.
    render(
      <ScanBudgetCard
        budget={{
          ...base,
          max_threads: 8,
          max_threads_source: "clickhouse_pinned",
          detected_cores: null,
        }}
      />,
    );
    expect(screen.getByText(/8 threads per scan/i)).toBeInTheDocument();
    expect(screen.getByText(/ClickHouse's own profile/i)).toBeInTheDocument();
    expect(screen.queryByText(/cores ClickHouse reports/i)).not.toBeInTheDocument();
  });

  it("discloses a concurrency edit waiting for a restart", () => {
    render(<ScanBudgetCard budget={{ ...base, pending_concurrency: 4 }} />);
    expect(screen.getByText(/restart/i)).toBeInTheDocument();
  });

  it("renders nothing without a budget — an anonymous health response has none", () => {
    const { container } = render(<ScanBudgetCard budget={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });
});
