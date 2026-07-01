/**
 * SessionMomentum — shows how many events you've triaged this session.
 *
 * The bar fills toward the next milestone of 10 (resets each 10 events),
 * so it always shows forward progress rather than an unreachable 100%.
 */
import { Zap } from "lucide-react";
import { Progress } from "@/components/ui/Progress";
import { Tooltip } from "@/components/ui/Tooltip";

interface Props {
  count: number; // distinct events triaged this session
}

const MILESTONE = 10;

export function SessionMomentum({ count }: Props) {
  const step = count % MILESTONE;
  const pct = count === 0 ? 0 : step === 0 ? 100 : (step / MILESTONE) * 100;
  const next = count === 0 ? MILESTONE : step === 0 ? count + MILESTONE : count - step + MILESTONE;

  return (
    <Tooltip
      content={`${count} event${count !== 1 ? "s" : ""} triaged this session — next milestone: ${next}`}
    >
      <div className="hidden sm:flex items-center gap-2">
        <Zap
          size={13}
          className={
            count > 0
              ? "text-[var(--color-accent)] fill-[var(--color-accent)]"
              : "text-[var(--color-fg-muted)]"
          }
        />
        <div className="w-24">
          <p className="text-xs font-medium uppercase tracking-wide text-[var(--color-fg-muted)] mb-1">
            {count > 0 ? `${count} triaged` : "Triaged"}
          </p>
          <Progress
            value={pct}
            indicatorClassName={
              count > 0
                ? "bg-[var(--color-accent)]"
                : "bg-[var(--color-fg-muted)]"
            }
          />
        </div>
      </div>
    </Tooltip>
  );
}
