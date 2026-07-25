/**
 * Audit rows restored from a .vestigo archive keep the actor, action and
 * timestamp the archive asserted — nothing on this instance vouches for them,
 * and any authenticated user can upload an archive. The importer stamps
 * detail.imported on every such row; this page is where an auditor would
 * otherwise mistake one for locally recorded activity, so the badge is the
 * point of the whole marker.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AdminAuditPage } from "@/pages/admin/AdminAuditPage";
import type { AuditEntry } from "@/api/types";

const queryAuditMock = vi.fn();

vi.mock("@/api/admin", () => ({
  adminApi: {
    queryAudit: (...a: unknown[]) => queryAuditMock(...a),
  },
}));

function entry(over: Partial<AuditEntry> = {}): AuditEntry {
  return {
    id: "audit_1",
    timestamp: "2026-07-25T10:00:00+00:00",
    user_id: null,
    username: "alice",
    action: "source.upload",
    method: "POST",
    path: "/api/cases/c1/sources",
    route: "/api/cases/{case_id}/sources",
    case_id: "case_1",
    target_type: "source",
    target_id: "source_1",
    status_code: 200,
    ip: null,
    user_agent: null,
    detail: null,
    ...over,
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <AdminAuditPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("AdminAuditPage imported marker", () => {
  it("badges rows the importer stamped", async () => {
    queryAuditMock.mockResolvedValue([
      entry({
        id: "audit_imported",
        detail: { imported: { job_id: "job-1", by: "bob", archive_case_id: "case_old" } },
      }),
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("alice")).toBeTruthy());
    expect(screen.getByText("imported")).toBeTruthy();
  });

  it("leaves locally recorded rows unbadged", async () => {
    queryAuditMock.mockResolvedValue([entry({ detail: { bytes: 12 } })]);
    renderPage();
    await waitFor(() => expect(screen.getByText("alice")).toBeTruthy());
    expect(screen.queryByText("imported")).toBeNull();
  });
});
