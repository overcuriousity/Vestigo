/**
 * TemplatesView — the Templates sub-tab: structurally-distinct log-line
 * shapes (W6), browsed and muted independently of any detector run.
 *
 * Variable substrings (timestamps, UUIDs, IPs, hex, digit runs) are masked
 * server-side so e.g. 50M "Allow TCP <IP>:<PORT> -> ..." lines collapse to
 * one template while a structurally distinct line stands out. Not a scored
 * detector — a browser, sorted by shape frequency (or first/last seen).
 *
 * The analyst's verb here is **Mute**: a muted template gets a
 * `kind="routine"`, `detector="log_template"` disposition. Unlike
 * sequence_motif, membership is a direct predicate on the materialized
 * `template_hash` column — no occurrence materialization job, so muting
 * takes effect immediately (no "collapsing…" watcher needed).
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { EyeOff, Filter, Info, Undo2 } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { dispositionsApi } from "@/api/dispositions";
import { useDisposition } from "@/hooks/useDisposition";
import { GuidancePanel } from "@/components/ui/GuidancePanel";
import { AnalysisEmptyState, RefreshButton } from "./detector-shared";
import { Spinner } from "@/components/ui/Spinner";
import { truncate } from "@/lib/format";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

interface Props {
  caseId: string;
  timelineId: string;
  onDrillField?: (field: string, value: string) => void;
}

const FIELD_OPTIONS = [{ value: "message", label: "Message" }];
const ORDER_OPTIONS = [
  { value: "count", label: "Most common" },
  { value: "first_seen", label: "Oldest first" },
  { value: "last_seen", label: "Newest first" },
] as const;

/** Mirrors `db/_template.py::TEMPLATE_NORMALIZE_VERSION`, which is the source
 * of truth — the disposition API rejects any other value, so a bump there must
 * be mirrored here. */
const TEMPLATE_VERSION = 1;

/** Muting is only offered for `message` templates. The grid's collapse
 * predicate is `template_hash NOT IN (...)` against the materialized column,
 * which is hashed over `message` alone; a mute minted from an `attr:*` listing
 * would carry a hash from a different domain and collapse events the row does
 * not describe. Other fields stay browsable — read-only exploration. The API
 * enforces the same rule (`routers/dispositions.py::_validate_scope`). */
const MUTABLE_FIELD = "message";

export function TemplatesView({ caseId, timelineId, onDrillField }: Props) {
  const [field, setField] = useState("message");
  const [order, setOrder] = useState<(typeof ORDER_OPTIONS)[number]["value"]>("count");
  const qc = useQueryClient();

  const { data: fieldsData } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "fields"],
    queryFn: () => anomaliesApi.fields(caseId, timelineId),
    staleTime: 5 * 60 * 1000,
  });
  const fieldOptions = useMemo(() => {
    const attrOptions = (fieldsData?.fields ?? [])
      .filter((f) => f.token.startsWith("attr:"))
      .map((f) => ({ value: f.token, label: f.token.slice(5) }));
    return [...FIELD_OPTIONS, ...attrOptions];
  }, [fieldsData]);

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["log-templates", caseId, timelineId, field, order],
    queryFn: () => anomaliesApi.logTemplates(caseId, timelineId, { field, order, limit: 100 }),
    staleTime: 60_000,
  });

  // Shared cache key with PatternsView's routine-dispositions query — see
  // that component's comment on why this fetches unfiltered and splits by
  // `detector` client-side rather than owning a detector-scoped key.
  const { data: routineData } = useQuery({
    queryKey: ["dispositions", caseId, timelineId, "routine"],
    queryFn: () => dispositionsApi.list(caseId, timelineId, { kind: "routine" }),
  });
  const routineRows = useMemo(
    () => (routineData?.dispositions ?? []).filter((d) => d.detector === "log_template"),
    [routineData],
  );
  // Mutes only ever exist for `message` (see MUTABLE_FIELD), so template ids
  // from an `attr:*` listing must not be matched against them — the two hash
  // domains are unrelated and would collide by coincidence.
  const canMute = field === MUTABLE_FIELD;
  const mutedIds = useMemo(
    () => (canMute ? new Set(routineRows.map((d) => d.value)) : new Set<string | null>()),
    [routineRows, canMute],
  );

  const dispositionMut = useDisposition(caseId, timelineId);
  // `useDisposition` is one shared mutation, so gate the spinner on which row
  // it is actually carrying — otherwise every Mute button spins at once.
  const mutingId = dispositionMut.isPending ? dispositionMut.variables?.value : undefined;
  const unmarkMut = useMutation({
    mutationFn: (id: string) => dispositionsApi.remove(caseId, timelineId, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dispositions", caseId, timelineId] });
      qc.invalidateQueries({ queryKey: ["events"] });
    },
    meta: { errorTitle: "Couldn't unmute template" },
  });

  const templates = useMemo(() => data?.templates ?? [], [data]);
  const activeTemplates = templates.filter((t) => !mutedIds.has(t.template_id));
  // Driven by the dispositions, not by the current page: a muted shape that
  // falls outside the top-`limit` listing (likely under a first/last-seen sort)
  // would otherwise be invisible and therefore impossible to unmute. The page
  // is only used to enrich a row when it happens to be present; everything
  // needed to display and reverse a mute is snapshotted in `details`.
  const mutedTemplates = useMemo(() => {
    if (!canMute) return [];
    const onPage = new Map(templates.map((t) => [t.template_id, t]));
    return routineRows.map((d) => {
      const details = (d.details ?? {}) as { template?: unknown; count_at_mute?: unknown };
      const row = d.value ? onPage.get(d.value) : undefined;
      return {
        dispositionId: d.id,
        templateId: d.value ?? "",
        template: row?.template ?? (typeof details.template === "string" ? details.template : ""),
        count: row?.count ?? (typeof details.count_at_mute === "number" ? details.count_at_mute : 0),
        stale: !row,
      };
    });
  }, [routineRows, templates, canMute]);

  return (
    <div className="space-y-3">
      <GuidancePanel id="investigate-templates" />

      <div className="flex flex-wrap items-center gap-2">
        <span className="shrink-0 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
          Template over
        </span>
        <select
          value={field}
          onChange={(e) => setField(e.target.value)}
          className="min-w-0 flex-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-0.5 text-xs text-[var(--color-fg-primary)] focus:border-[var(--color-accent)] focus:outline-none"
        >
          {fieldOptions.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <select
          value={order}
          onChange={(e) => setOrder(e.target.value as typeof order)}
          className="shrink-0 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-0.5 text-xs text-[var(--color-fg-primary)] focus:border-[var(--color-accent)] focus:outline-none"
        >
          {ORDER_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <RefreshButton isFetching={isFetching} onClick={() => refetch()} />
      </div>

      {data && (
        <div className="text-xs text-[var(--color-fg-muted)]">
          {data.total_templates.toLocaleString()} distinct template
          {data.total_templates === 1 ? "" : "s"}
          {templates.length < data.total_templates && ` — showing top ${templates.length}`}
        </div>
      )}

      {isLoading && (
        <div className="flex justify-center py-6">
          <Spinner size={18} />
        </div>
      )}

      {!isLoading && templates.length === 0 && (
        <AnalysisEmptyState hint="Templates are mined from event messages, so a timeline whose events carry no message text produces none.">
          No templates for this timeline.
        </AnalysisEmptyState>
      )}

      {activeTemplates.length > 0 && (
        <div className="space-y-1.5">
          {activeTemplates.map((t) => (
            <div
              key={t.template_id}
              className="space-y-1 rounded border border-[var(--color-border)] px-2 py-1.5"
            >
              <div className="flex items-start gap-2">
                <span className="min-w-0 flex-1 break-all font-mono text-xs text-[var(--color-fg-primary)]">
                  {truncate(t.template, 200)}
                </span>
                <div className="flex shrink-0 items-center gap-1">
                  {onDrillField && (
                    <button
                      title="Filter the grid to this template"
                      className="rounded p-0.5 text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-accent)]"
                      onClick={() => onDrillField("template_id", t.template_id)}
                    >
                      <Filter size={12} />
                    </button>
                  )}
                  {canMute ? (
                    <button
                      title="Mute: a routine, expected line shape — its events disappear from the grid immediately (always with a visible count). Reversible via Unmute below."
                      className="rounded p-0.5 text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-accent)]"
                      disabled={mutingId === t.template_id}
                      onClick={() =>
                        dispositionMut.mutate({
                          kind: "routine",
                          detector: "log_template",
                          field: "template_id",
                          value: t.template_id,
                          details: {
                            template: t.template,
                            template_version: TEMPLATE_VERSION,
                            field,
                            example: t.example,
                            count_at_mute: t.count,
                          },
                        })
                      }
                    >
                      {mutingId === t.template_id ? <Spinner size={11} /> : <EyeOff size={12} />}
                    </button>
                  ) : (
                    <span
                      title={`Muting is only available for Message templates — the grid collapses events by the materialized message template hash, so a ${field} shape has no collapse predicate.`}
                      className="cursor-not-allowed p-0.5 text-[var(--color-fg-muted)] opacity-40"
                    >
                      <EyeOff size={12} />
                    </span>
                  )}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
                <span>
                  ×<strong className="text-[var(--color-fg-secondary)]">{t.count.toLocaleString()}</strong>
                </span>
                {t.distinct_sources > 1 && <span>{t.distinct_sources} sources</span>}
                {t.first_seen && <span>first {fmtTs(t.first_seen)}</span>}
                {t.last_seen && <span>last {fmtTs(t.last_seen)}</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      {mutedTemplates.length > 0 && (
        <div className="border-t border-[var(--color-border)] pt-2">
          <div className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-secondary)]">
            <EyeOff size={12} />
            Muted templates ({mutedTemplates.length})
          </div>
          <div className="space-y-1">
            {mutedTemplates.map((t) => (
              <div
                key={t.dispositionId}
                className="flex items-center gap-2 rounded border border-[var(--color-border)] px-2 py-1.5 text-xs"
              >
                <span className="min-w-0 flex-1 break-all font-mono text-[var(--color-fg-secondary)]">
                  {truncate(t.template, 120)}
                </span>
                <span
                  className="shrink-0 text-[var(--color-fg-muted)]"
                  title={t.stale ? "Count recorded when this template was muted" : undefined}
                >
                  ×{t.count.toLocaleString()}
                  {t.stale && " at mute"}
                </span>
                <button
                  title="Unmute — its events reappear in the grid immediately"
                  className="flex shrink-0 items-center gap-1 rounded p-0.5 text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-fg-primary)]"
                  disabled={unmarkMut.isPending}
                  onClick={() => unmarkMut.mutate(t.dispositionId)}
                >
                  {unmarkMut.isPending ? <Spinner size={11} /> : <Undo2 size={12} />}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="flex items-start gap-1.5 pt-1 text-xs text-[var(--color-fg-muted)]">
        <Info size={10} className="mt-0.5 shrink-0" />
        <span>
          Timestamps, UUIDs, IPs, hex runs, and numbers are masked to reveal each line's
          shape; templates are grouped and ranked by how often that shape occurs. Mute a
          template to collapse its events in the grid — the grid always shows how many
          were collapsed.
        </span>
      </div>
    </div>
  );
}
