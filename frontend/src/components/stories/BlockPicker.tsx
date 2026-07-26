import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BarChart3, FileText, Plus, Table2, Target } from "lucide-react";
import { savedChartsApi } from "@/api/viz";
import { timelinesApi } from "@/api/timelines";
import { viewsApi } from "@/api/views";
import type { StoryBlockKind } from "@/api/types";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogClose, DialogContent } from "@/components/ui/Dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/DropdownMenu";
import { Input } from "@/components/ui/Input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/Select";
import { Spinner } from "@/components/ui/Spinner";

interface Props {
  caseId: string;
  onInsert: (kind: StoryBlockKind, content: Record<string, unknown>) => void;
  label?: string;
}

type PickerMode = "view" | "chart" | "event" | null;

/**
 * "+ block" menu: markdown inline, embeds through a small picker. Pushing
 * from the analysis pages ("Add to story") is the primary path — this is the
 * secondary one, for assembling a story after the fact.
 */
export function BlockPicker({ caseId, onInsert, label = "Add block" }: Props) {
  const [mode, setMode] = useState<PickerMode>(null);

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="sm" aria-label={label}>
            <Plus size={12} /> Block
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="center">
          <DropdownMenuItem onSelect={() => onInsert("markdown", { text: "" })}>
            <FileText size={13} /> Text
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setMode("view")}>
            <Table2 size={13} /> Saved view
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setMode("chart")}>
            <BarChart3 size={13} /> Saved chart
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setMode("event")}>
            <Target size={13} /> Event
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={mode !== null} onOpenChange={(open) => !open && setMode(null)}>
        <DialogContent
          title={
            mode === "view" ? "Embed a view" : mode === "chart" ? "Embed a chart" : "Embed an event"
          }
          description="Embeds stay live while you write; exporting freezes them."
        >
          {mode === "view" && (
            <ViewPicker
              caseId={caseId}
              onPick={(content) => {
                onInsert("view_ref", content);
                setMode(null);
              }}
            />
          )}
          {mode === "chart" && (
            <ChartPicker
              caseId={caseId}
              onPick={(content) => {
                onInsert("chart_ref", content);
                setMode(null);
              }}
            />
          )}
          {mode === "event" && (
            <EventPicker
              onPick={(content) => {
                onInsert("event_ref", content);
                setMode(null);
              }}
            />
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

function useTimelinePick(caseId: string) {
  const { data: timelines } = useQuery({
    queryKey: ["timelines", caseId],
    queryFn: () => timelinesApi.list(caseId),
  });
  const [timelineId, setTimelineId] = useState<string>("");
  const resolved = timelineId || timelines?.find((t) => t.is_default)?.id || timelines?.[0]?.id || "";
  return { timelines: timelines ?? [], timelineId: resolved, setTimelineId };
}

function TimelineSelect({
  timelines,
  value,
  onChange,
}: {
  timelines: { id: string; name: string }[];
  value: string;
  onChange: (id: string) => void;
}) {
  if (timelines.length <= 1) return null;
  return (
    <div>
      <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">Timeline</label>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {timelines.map((t) => (
            <SelectItem key={t.id} value={t.id}>
              {t.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function ViewPicker({
  caseId,
  onPick,
}: {
  caseId: string;
  onPick: (content: Record<string, unknown>) => void;
}) {
  const { timelines, timelineId, setTimelineId } = useTimelinePick(caseId);
  const { data: views, isLoading } = useQuery({
    queryKey: ["views", caseId],
    queryFn: () => viewsApi.list(caseId),
  });
  const [search, setSearch] = useState("");
  const matches = (views ?? []).filter((v) =>
    v.name.toLowerCase().includes(search.trim().toLowerCase()),
  );

  return (
    <div className="space-y-3">
      <TimelineSelect timelines={timelines} value={timelineId} onChange={setTimelineId} />
      <Input placeholder="Search views…" value={search} onChange={(e) => setSearch(e.target.value)} />
      {isLoading && <Spinner size={16} />}
      {views && views.length === 0 && (
        <p className="text-xs text-[var(--color-fg-muted)]">
          No saved views yet. Save one from the Explorer first.
        </p>
      )}
      <div className="max-h-56 space-y-1 overflow-y-auto">
        {matches.map((view) => (
          <button
            key={view.id}
            type="button"
            className="w-full rounded px-2 py-1.5 text-left text-xs text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)]"
            onClick={() =>
              onPick({
                view_id: view.id,
                timeline_id: timelineId,
                display: { limit: 200 },
              })
            }
          >
            {view.name}
          </button>
        ))}
      </div>
      <div className="flex justify-end">
        <DialogClose asChild>
          <Button variant="ghost" size="sm">
            Cancel
          </Button>
        </DialogClose>
      </div>
    </div>
  );
}

function ChartPicker({
  caseId,
  onPick,
}: {
  caseId: string;
  onPick: (content: Record<string, unknown>) => void;
}) {
  const { timelines, timelineId, setTimelineId } = useTimelinePick(caseId);
  const { data, isLoading } = useQuery({
    queryKey: ["viz-saved-charts", caseId, timelineId],
    queryFn: () => savedChartsApi.list(caseId, timelineId),
    enabled: !!timelineId,
  });
  const charts = data?.charts ?? [];

  return (
    <div className="space-y-3">
      <TimelineSelect timelines={timelines} value={timelineId} onChange={setTimelineId} />
      {isLoading && <Spinner size={16} />}
      {data && charts.length === 0 && (
        <p className="text-xs text-[var(--color-fg-muted)]">
          No saved charts on this timeline. Save one from the Visualize page first.
        </p>
      )}
      <div className="max-h-56 space-y-1 overflow-y-auto">
        {charts.map((chart) => (
          <button
            key={chart.id}
            type="button"
            className="w-full rounded px-2 py-1.5 text-left text-xs text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)]"
            onClick={() => onPick({ chart_id: chart.id, timeline_id: timelineId })}
          >
            {chart.name}
          </button>
        ))}
      </div>
      <div className="flex justify-end">
        <DialogClose asChild>
          <Button variant="ghost" size="sm">
            Cancel
          </Button>
        </DialogClose>
      </div>
    </div>
  );
}

function EventPicker({ onPick }: { onPick: (content: Record<string, unknown>) => void }) {
  const [eventId, setEventId] = useState("");
  const [sourceId, setSourceId] = useState("");

  return (
    <div className="space-y-3">
      <p className="text-xs text-[var(--color-fg-muted)]">
        Pushing from the Explorer's event detail is the usual way in; paste the identifiers
        here if you already have them.
      </p>
      <div>
        <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">Event ID</label>
        <Input value={eventId} onChange={(e) => setEventId(e.target.value)} />
      </div>
      <div>
        <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">Source ID</label>
        <Input value={sourceId} onChange={(e) => setSourceId(e.target.value)} />
      </div>
      <div className="flex justify-end gap-2">
        <DialogClose asChild>
          <Button variant="ghost" size="sm">
            Cancel
          </Button>
        </DialogClose>
        <Button
          variant="accent"
          size="sm"
          disabled={!eventId.trim() || !sourceId.trim()}
          onClick={() => onPick({ event_id: eventId.trim(), source_id: sourceId.trim() })}
        >
          Embed
        </Button>
      </div>
    </div>
  );
}
