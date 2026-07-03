import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { adminApi } from "@/api/admin";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
} from "@/components/ui/Table";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { fmtTimestampFull } from "@/lib/time";

export function AdminAuditPage() {
  const [action, setAction] = useState("");
  const [caseId, setCaseId] = useState("");
  const debouncedAction = useDebouncedValue(action, 300);
  const debouncedCaseId = useDebouncedValue(caseId, 300);

  const { data: rows, isLoading } = useQuery({
    queryKey: ["admin", "audit", debouncedAction, debouncedCaseId],
    queryFn: () =>
      adminApi.queryAudit({
        action: debouncedAction || undefined,
        case_id: debouncedCaseId || undefined,
        limit: 500,
      }),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-[var(--color-fg-primary)]">
          Audit log {rows ? `(${rows.length} of most recent 500)` : ""}
        </h2>
        <div className="flex gap-2">
          <Input
            placeholder="Filter by action..."
            value={action}
            onChange={(e) => setAction(e.target.value)}
            className="w-48"
          />
          <Input
            placeholder="Filter by case_id..."
            value={caseId}
            onChange={(e) => setCaseId(e.target.value)}
            className="w-48"
          />
        </div>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner size={20} />
        </div>
      ) : (
        <Table>
          <TableHead>
            <tr>
              <TableHeaderCell>Time</TableHeaderCell>
              <TableHeaderCell>User</TableHeaderCell>
              <TableHeaderCell>Action</TableHeaderCell>
              <TableHeaderCell>Route</TableHeaderCell>
              <TableHeaderCell>Case</TableHeaderCell>
              <TableHeaderCell>Status</TableHeaderCell>
            </tr>
          </TableHead>
          <TableBody>
            {rows?.map((r) => (
              <TableRow key={r.id}>
                <TableCell className="whitespace-nowrap text-xs text-[var(--color-fg-muted)]">
                  {fmtTimestampFull(r.timestamp)}
                </TableCell>
                <TableCell className="text-xs">{r.username ?? "anonymous"}</TableCell>
                <TableCell className="font-mono text-xs">{r.action}</TableCell>
                <TableCell className="font-mono text-xs text-[var(--color-fg-muted)]">
                  {r.method} {r.route}
                </TableCell>
                <TableCell className="font-mono text-xs text-[var(--color-fg-muted)]">
                  {r.case_id ?? "—"}
                </TableCell>
                <TableCell className="text-xs">{r.status_code ?? "—"}</TableCell>
              </TableRow>
            ))}
            {rows?.length === 0 && (
              <TableRow>
                <TableCell colSpan={6} className="text-center text-[var(--color-fg-muted)]">
                  No matching audit rows.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
