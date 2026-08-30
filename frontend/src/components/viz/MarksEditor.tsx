/**
 * The rail's Marks section: the chart's mark sources, each with the status
 * the server resolved it to (drawn / capped / undated), and an add menu that
 * turns the eight things an analyst wants to point at into `MarkSource`s.
 * Every entry is a source, never a pixel — the figure resolves them again on
 * every draw (`useResolvedMarks`), so a mark keeps meaning what it meant.
 */
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/Select";
import { baselinesApi } from "@/api/baselines";
import { viewsApi } from "@/api/views";
import { dispositionsApi } from "@/api/dispositions";
import type { EventFilters, ResolvedMarksResponse, VizFieldInfo } from "@/api/types";
import type { MarkSource } from "@/components/viz/lib/chartConfig";
import { describeFilters } from "@/components/viz/lib/caption";
import { CompareFilterEditor } from "./CompareFilterEditor";

type AddKind = "event" | "tag" | "confirmed" | "filter" | "baseline" | "view" | "instant" | "range";

const ADD_ENTRIES: { value: AddKind; label: string }[] = [
  { value: "event", label: "Event id" },
  { value: "tag", label: "Tag" },
  { value: "confirmed", label: "Confirmed findings" },
  { value: "filter", label: "Custom filter" },
  { value: "baseline", label: "Baseline definition" },
  { value: "view", label: "Saved view" },
  { value: "instant", label: "Instant" },
  { value: "range", label: "Range" },
];

interface Props {
  caseId: string;
  timelineId: string;
  marks: MarkSource[];
  onChange: (marks: MarkSource[]) => void;
  fields: VizFieldInfo[];
  resolved?: ResolvedMarksResponse;
}

const input =
  "w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1.5 py-0.5 text-xs text-[var(--color-fg-primary)] focus:border-[var(--color-accent)] focus:outline-none";
const labelCls = "mb-1 block text-xs text-[var(--color-fg-secondary)]";

/** `datetime-local` value → ISO instant (UTC). */
const localToIso = (v: string) => new Date(v.endsWith("Z") ? v : `${v}Z`).toISOString();

function describeMark(m: MarkSource): string {
  switch (m.kind) {
    case "events":
      return m.label ?? describeFilters(m.filters);
    case "baseline":
      return `baseline ${m.definitionId}`;
    case "view":
      return `saved view ${m.viewId}`;
    case "instant":
      return `${m.label} @ ${m.at}`;
    case "range":
      return `${m.label} ${m.start} → ${m.end}`;
  }
}

export function MarksEditor({ caseId, timelineId, marks, onChange, fields, resolved }: Props) {
  const [adding, setAdding] = useState<AddKind | "">("");
  const [text, setText] = useState("");
  const [label, setLabel] = useState("");
  const [at, setAt] = useState("");
  const [end, setEnd] = useState("");
  const [filters, setFilters] = useState<EventFilters>({});
  const [notice, setNotice] = useState<string | null>(null);

  const baselines = useQuery({
    queryKey: ["baselines", caseId, timelineId],
    queryFn: () => baselinesApi.list(caseId, timelineId),
    enabled: adding === "baseline",
  });
  const views = useQuery({
    queryKey: ["views", caseId],
    queryFn: () => viewsApi.list(caseId),
    enabled: adding === "view",
  });
  const confirmed = useQuery({
    queryKey: ["dispositions", caseId, timelineId, "confirmed"],
    queryFn: () => dispositionsApi.list(caseId, timelineId, { kind: "confirmed" }),
    enabled: adding === "confirmed",
  });

  const reset = () => {
    setAdding("");
    setText("");
    setLabel("");
    setAt("");
    setEnd("");
    setFilters({});
  };
  const add = (mark: MarkSource) => {
    onChange([...marks, mark]);
    reset();
  };

  // Confirmed findings resolve as soon as the list arrives: one mark over
  // every confirmed event id, or a notice when there is nothing to mark.
  const confirmedData = adding === "confirmed" ? confirmed.data : undefined;
  // A rejected lookup used to end the interaction wordlessly: `isLoading` goes
  // false, the effect below early-returns on the absent data, and `adding`
  // stays "confirmed" — leaving the analyst unable to tell a failed request
  // from a timeline with no confirmed findings, which *is* reported. Same
  // shape as the empty case: say what happened and clear the picker.
  const confirmedError = adding === "confirmed" ? confirmed.error : undefined;
  useEffect(() => {
    if (!confirmedError) return;
    setNotice("Could not load confirmed findings — the request failed.");
    reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [confirmedError]);
  useEffect(() => {
    if (!confirmedData) return;
    const ids = [
      ...new Set(
        confirmedData.dispositions.map((d) => d.event_id).filter((v): v is string => !!v),
      ),
    ];
    if (ids.length) {
      onChange([...marks, { kind: "events", filters: { ids }, label: "confirmed findings" }]);
    } else {
      setNotice("No confirmed findings in this timeline.");
    }
    reset();
    // `marks`/`onChange` are read at the moment the list lands, on purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [confirmedData]);

  return (
    <div className="space-y-2">
      {marks.length === 0 && (
        <p className="text-xs text-[var(--color-fg-muted)]">
          No marks. Add one to point at an instant or a window.
        </p>
      )}
      <ul className="space-y-1">
        {marks.map((m, i) => {
          const status = resolved?.sources.find((s) => s.index === i);
          const name = describeMark(m);
          return (
            <li key={i} className="rounded border border-[var(--color-border)] px-1.5 py-1 text-xs">
              <div className="flex items-start justify-between gap-2">
                <span className="min-w-0 break-words text-[var(--color-fg-primary)]">{name}</span>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Remove mark ${name}`}
                  onClick={() => onChange(marks.filter((_, j) => j !== i))}
                >
                  <X size={12} />
                </Button>
              </div>
              {status && (m.kind === "events" || m.kind === "view") && (
                <p className="mt-0.5 text-[var(--color-fg-muted)]">
                  {status.overflow
                    ? `${status.shown} of ${status.count} drawn — capped at ${resolved!.cap}`
                    : `${status.shown} drawn`}
                  {status.undated > 0 && `; ${status.undated} undated not drawn`}
                </p>
              )}
              {status && m.kind === "baseline" && (
                <p className="mt-0.5 text-[var(--color-fg-muted)]">
                  {status.label} — {status.count} window{status.count === 1 ? "" : "s"}
                </p>
              )}
            </li>
          );
        })}
      </ul>
      {notice && <p className="text-xs text-[var(--color-fg-muted)]">{notice}</p>}

      <Select
        value={adding}
        onValueChange={(v) => {
          setNotice(null);
          setAdding(v as AddKind);
        }}
      >
        <SelectTrigger className="h-7 text-xs" aria-label="Add mark">
          <SelectValue placeholder="Add mark…" />
        </SelectTrigger>
        <SelectContent>
          {ADD_ENTRIES.map((e) => (
            <SelectItem key={e.value} value={e.value}>
              {e.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {(adding === "event" || adding === "tag") && (
        <div className="space-y-1">
          <label className={labelCls} htmlFor="mark-text">
            {adding === "event" ? "Event id" : "Tag"}
          </label>
          <input
            id="mark-text"
            className={input}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <Button
            size="sm"
            variant="outline"
            disabled={!text.trim()}
            onClick={() =>
              add(
                adding === "event"
                  ? { kind: "events", filters: { ids: [text.trim()] }, label: `event ${text.trim()}` }
                  : {
                      kind: "events",
                      filters: { tagsInclude: [text.trim()] },
                      label: `tag ${text.trim()}`,
                    },
              )
            }
          >
            Add
          </Button>
        </div>
      )}
      {adding === "filter" && (
        <div className="space-y-1">
          <CompareFilterEditor filters={filters} onChange={setFilters} fields={fields} />
          <label className={labelCls} htmlFor="mark-label">
            Label
          </label>
          <input
            id="mark-label"
            className={input}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="optional"
          />
          <Button
            size="sm"
            variant="outline"
            onClick={() =>
              add({ kind: "events", filters, ...(label.trim() ? { label: label.trim() } : {}) })
            }
          >
            Add
          </Button>
        </div>
      )}
      {adding === "baseline" && (
        <Select onValueChange={(v) => add({ kind: "baseline", definitionId: v })}>
          <SelectTrigger className="h-7 text-xs" aria-label="Baseline definition">
            <SelectValue placeholder="Choose…" />
          </SelectTrigger>
          <SelectContent>
            {(baselines.data?.baselines ?? []).map((b) => (
              <SelectItem key={b.id} value={b.id}>
                {b.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      {adding === "view" && (
        <Select onValueChange={(v) => add({ kind: "view", viewId: v })}>
          <SelectTrigger className="h-7 text-xs" aria-label="Saved view">
            <SelectValue placeholder="Choose…" />
          </SelectTrigger>
          <SelectContent>
            {(views.data ?? []).map((v) => (
              <SelectItem key={v.id} value={v.id}>
                {v.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      {adding === "confirmed" && confirmed.isLoading && (
        <p className="text-xs text-[var(--color-fg-muted)]">Looking up confirmed findings…</p>
      )}
      {(adding === "instant" || adding === "range") && (
        <div className="space-y-1">
          <label className={labelCls} htmlFor="mark-at">
            {adding === "instant" ? "At" : "Start"}
          </label>
          <input
            id="mark-at"
            type="datetime-local"
            className={input}
            value={at}
            onChange={(e) => setAt(e.target.value)}
          />
          {adding === "range" && (
            <>
              <label className={labelCls} htmlFor="mark-end">
                End
              </label>
              <input
                id="mark-end"
                type="datetime-local"
                className={input}
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </>
          )}
          <label className={labelCls} htmlFor="mark-label">
            Label
          </label>
          <input
            id="mark-label"
            className={input}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
          <Button
            size="sm"
            variant="outline"
            disabled={!at || !label.trim() || (adding === "range" && (!end || end <= at))}
            onClick={() =>
              add(
                adding === "instant"
                  ? { kind: "instant", at: localToIso(at), label: label.trim() }
                  : {
                      kind: "range",
                      start: localToIso(at),
                      end: localToIso(end),
                      label: label.trim(),
                    },
              )
            }
          >
            Add
          </Button>
          <p className="text-xs text-[var(--color-fg-muted)]">Times are UTC.</p>
        </div>
      )}
    </div>
  );
}
