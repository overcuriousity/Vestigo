import { Link } from "react-router-dom";
import { FolderOpen, ChevronRight, Users, User as UserIcon } from "lucide-react";
import { fmtRelative } from "@/lib/time";
import { DeleteCaseDialog } from "./DeleteCaseDialog";
import { ChangeCaseScopeDialog } from "./ChangeCaseScopeDialog";
import { Badge } from "@/components/ui/Badge";
import { canManageCase } from "@/lib/caseAccess";
import { useAuthStore } from "@/stores/auth";
import type { Case } from "@/api/types";

interface Props {
  case_: Case;
}

export function CaseCard({ case_ }: Props) {
  const user = useAuthStore((s) => s.user);
  const team = user?.teams?.find((t) => t.id === case_.team_id);
  const canManage = canManageCase(case_, user);

  return (
    <div className="group relative flex items-center gap-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-5 py-4 hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-elevated)] transition-base">
      <FolderOpen
        size={20}
        className="shrink-0 text-[var(--color-accent)] opacity-80"
      />
      <Link
        to={`/cases/${case_.id}`}
        className="flex-1 min-w-0"
      >
        <div className="flex items-center gap-2">
          <h3 className="truncate font-semibold text-[var(--color-fg-primary)]">
            {case_.name}
          </h3>
          {case_.team_id ? (
            <Badge variant="accent" className="inline-flex items-center gap-1">
              <Users size={10} /> {team?.name ?? "team"}
            </Badge>
          ) : (
            <Badge variant="muted" className="inline-flex items-center gap-1">
              <UserIcon size={10} /> personal
            </Badge>
          )}
        </div>
        {case_.description && (
          <p className="mt-0.5 truncate text-xs text-[var(--color-fg-muted)]">
            {case_.description}
          </p>
        )}
        <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
          Created {fmtRelative(case_.created_at)}
        </p>
      </Link>
      <div className="flex items-center gap-1">
        {canManage && <ChangeCaseScopeDialog case_={case_} />}
        {canManage && <DeleteCaseDialog case_={case_} />}
        <Link to={`/cases/${case_.id}`} tabIndex={-1}>
          <ChevronRight size={16} className="text-[var(--color-fg-muted)]" />
        </Link>
      </div>
    </div>
  );
}
