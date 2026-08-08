/**
 * SigmaFindings — Sigma rule hits as rows in the rail's Named-techniques group.
 *
 * These are the only findings in the stream that are not statistical opinions.
 * A rule author asserted that this pattern *is* the named technique, which is
 * why the group they sit in leads the rail and why its note reads "a rule
 * author named this" rather than "odd, not necessarily bad".
 *
 * A row is a rule and its match count, because that is what a run records —
 * `SigmaRunResult` carries `match_count`, not the matching rows. The drill
 * hands the grid the same `sigma: <title>` tag the Tools sheet's own drill
 * uses, so the two cannot disagree about what a rule's hits are.
 *
 * No verdict controls: a disposition is scoped to a value key or an event, and
 * a rule-level row has neither. Drilling to the hits and reaching a verdict on
 * one of *those* is the honest path, and it is one click away.
 */
import { Filter, ShieldAlert } from "lucide-react";
import { FindingShell } from "./detector-shared";
import type { SigmaFinding } from "@/hooks/useSigmaFindings";

export function SigmaFindingRows({
  findings,
  onTagFilter,
}: {
  findings: SigmaFinding[];
  onTagFilter?: (tag: string) => void;
}) {
  return (
    <>
      {findings.map((f) => (
        <FindingShell
          key={f.ruleKey}
          details={{ rule: f.ruleKey, level: f.level ?? "—", matches: f.matchCount }}
          onClick={() => onTagFilter?.(f.tag)}
          actions={
            onTagFilter && (
              <button
                data-testid="sigma-drill"
                onClick={(e) => {
                  e.stopPropagation();
                  onTagFilter(f.tag);
                }}
                title="Filter the grid to this rule's hits"
                className="rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-accent)]"
              >
                <Filter size={11} />
              </button>
            )
          }
        >
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="flex shrink-0 items-center gap-1 rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--color-fg-muted)]">
              <ShieldAlert size={10} />
              Sigma rule
            </span>
            <span className="min-w-0 break-all font-mono text-xs font-medium text-[var(--color-fg-primary)]">
              {f.title}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
            <span>
              ×{f.matchCount} event{f.matchCount === 1 ? "" : "s"}
            </span>
            {f.level && <span className="uppercase">{f.level}</span>}
          </div>
        </FindingShell>
      ))}
    </>
  );
}
