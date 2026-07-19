/**
 * FindingCard — renders an agent `propose_finding` tool call as an
 * applyable card: title, explanation, the proposed filter set as chips, and
 * an "Apply to Explorer" button that writes the filters into the URL
 * (sandbox + apply model — the agent never touches the analyst's view
 * itself).
 */
import { ArrowRight, Lightbulb } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { specToEventFilters, type AgentFilterSpec } from "@/api/agent";
import type { EventFilters } from "@/api/types";

interface Props {
  title: string;
  description: string;
  spec: AgentFilterSpec;
  /** Hit count reported by the backend when the finding was proposed. */
  total?: number | null;
  onApply: (filters: EventFilters) => void;
}

function specChips(spec: AgentFilterSpec): string[] {
  const chips: string[] = [];
  if (spec.q) chips.push(spec.q_regex ? `q ~ ${spec.q}` : `q: ${spec.q}`);
  if (spec.artifacts?.length) chips.push(`artifact: ${spec.artifacts.join(", ")}`);
  if (spec.source_id) chips.push(`source: ${spec.source_id}`);
  if (spec.start) chips.push(`from ${spec.start}`);
  if (spec.end) chips.push(`until ${spec.end}`);
  for (const [field, values] of Object.entries(spec.filters ?? {})) {
    chips.push(`${field} = ${values.join(" | ")}`);
  }
  for (const [field, values] of Object.entries(spec.exclusions ?? {})) {
    chips.push(`${field} ≠ ${values.join(" | ")}`);
  }
  if (spec.tags_include?.length) chips.push(`tag: ${spec.tags_include.join(", ")}`);
  if (spec.tags_exclude?.length) chips.push(`-tag: ${spec.tags_exclude.join(", ")}`);
  return chips;
}

export function FindingCard({ title, description, spec, total, onApply }: Props) {
  return (
    <div className="rounded-md border border-[var(--color-accent)] bg-[var(--color-accent-dim)] p-2.5 text-xs">
      <div className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-primary)]">
        <Lightbulb size={13} className="shrink-0 text-[var(--color-accent)]" />
        <span className="min-w-0 break-words">{title}</span>
      </div>
      {description && (
        <p className="mt-1 whitespace-pre-wrap text-[var(--color-fg-secondary)]">{description}</p>
      )}
      <div className="mt-1.5 flex flex-wrap gap-1">
        {specChips(spec).map((chip, i) => (
          <span
            key={i}
            className="rounded bg-[var(--color-bg-surface)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--color-fg-primary)]"
          >
            {chip}
          </span>
        ))}
      </div>
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className="text-[var(--color-fg-secondary)]">
          {typeof total === "number" ? `${total.toLocaleString()} matching events` : ""}
        </span>
        <Button variant="accent" size="sm" onClick={() => onApply(specToEventFilters(spec))}>
          Apply to Explorer
          <ArrowRight size={12} />
        </Button>
      </div>
    </div>
  );
}
