import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { downloadExport, downloadFieldInventory } from "@/api/export";
import { vizApi } from "@/api/viz";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Checkbox";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { TransferProgressRow } from "@/components/ui/TransferProgressRow";
import { useFileTransfer } from "@/hooks/useFileTransfer";
import type {
  EventFilters,
  FieldInventoryColumn,
  FieldInventoryOrder,
  FieldInventorySeparator,
} from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  filters: EventFilters;
  total: number | null;
}

type Mode = "events" | "inventory";

const COLUMN_LABELS: Record<FieldInventoryColumn, string> = {
  value: "Value",
  count: "Count",
  first_seen: "First seen",
  last_seen: "Last seen",
};

/** `value` is what the file is *of*, so it is not offered as a toggle. */
const OPTIONAL_COLUMNS: FieldInventoryColumn[] = ["first_seen", "last_seen", "count"];

const SEPARATORS: { id: FieldInventorySeparator; label: string }[] = [
  { id: "comma", label: "," },
  { id: "semicolon", label: ";" },
  { id: "tab", label: "tab" },
  { id: "pipe", label: "|" },
];

const ORDERINGS: { id: FieldInventoryOrder; label: string }[] = [
  { id: "count_desc", label: "Most frequent first" },
  { id: "count_asc", label: "Least frequent first" },
  { id: "value_asc", label: "Value (A→Z)" },
  { id: "value_desc", label: "Value (Z→A)" },
  { id: "first_seen_asc", label: "First seen (oldest first)" },
  { id: "first_seen_desc", label: "First seen (newest first)" },
  { id: "last_seen_asc", label: "Last seen (oldest first)" },
  { id: "last_seen_desc", label: "Last seen (newest first)" },
];

const SELECT_CLASS =
  "h-8 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-2 text-xs text-[var(--color-fg-primary)]";

/** The column an ordering sorts by — always written to the file, since a file
 * sorted by a column it doesn't contain reads as shuffled. Mirrors the
 * server's own rule, so the checkbox list can say so before the download. */
function sortColumn(order: FieldInventoryOrder): FieldInventoryColumn {
  return order.slice(0, order.lastIndexOf("_")) as FieldInventoryColumn;
}

export function ExportDialog({ caseId, timelineId, filters, total }: Props) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>("events");
  const [format, setFormat] = useState<"csv" | "jsonl">("csv");

  const [field, setField] = useState<string | null>(null);
  const [picked, setPicked] = useState<FieldInventoryColumn[]>(["first_seen", "last_seen"]);
  const [separator, setSeparator] = useState<FieldInventorySeparator>("comma");
  const [orderBy, setOrderBy] = useState<FieldInventoryOrder>("count_desc");

  // Same list the Visualization page picks from — it carries distinct counts,
  // which is the one number that tells the analyst how long this file will be
  // before they ask for it.
  const fieldsQuery = useQuery({
    queryKey: ["viz-fields", caseId, timelineId],
    queryFn: () => vizApi.fields(caseId, timelineId),
    enabled: open && mode === "inventory",
  });

  const columns = useMemo<FieldInventoryColumn[]>(() => {
    const chosen: FieldInventoryColumn[] = ["value", ...picked];
    const sorted = sortColumn(orderBy);
    return chosen.includes(sorted) ? chosen : [...chosen, sorted];
  }, [picked, orderBy]);

  const inventoryExt = separator === "tab" ? "tsv" : "csv";
  const distinct = fieldsQuery.data?.fields.find((f) => f.token === field)?.distinct ?? null;

  // The response is a chunked stream with no `Content-Length`, so progress can
  // only ever be bytes-so-far against an unknown total — an indeterminate bar.
  // That is still the difference between "working" and "hung" on an export of
  // a few million events, which is the whole point.
  const download = useFileTransfer({
    mutationFn: (o) =>
      mode === "events"
        ? downloadExport(caseId, timelineId, format, filters, o)
        : downloadFieldInventory(
            caseId,
            timelineId,
            filters,
            { field: field!, columns, separator, orderBy },
            o,
          ),
    onSuccess: () => setOpen(false),
  });

  const description =
    mode === "events"
      ? total !== null
        ? `Download all ${total.toLocaleString()} matching events with current filters applied.`
        : "Download all matching events with current filters applied."
      : "Download one row per distinct value of a field — with how often it occurs and when it was first and last seen — computed over the same filtered view.";

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Download size={13} /> Export
        </Button>
      </DialogTrigger>
      <DialogContent title="Export" description={description}>
        {/* Every control that shapes the request is frozen while a download is
            in flight: the request captured its values at submit, so a control
            that still moves makes the progress label, the button's extension
            and the saved filename disagree with the file actually arriving. */}
        <div className="space-y-4">
          <SegmentedControl<Mode>
            value={mode}
            onChange={setMode}
            disabled={download.active}
            options={[
              { id: "events", label: "Events" },
              { id: "inventory", label: "Value inventory" },
            ]}
          />

          {mode === "events" ? (
            <>
              <div>
                <label className="mb-2 block text-xs text-[var(--color-fg-muted)]">
                  Format
                </label>
                <div className="flex gap-2">
                  {(["csv", "jsonl"] as const).map((f) => (
                    <button
                      key={f}
                      onClick={() => setFormat(f)}
                      disabled={download.active}
                      className={`flex-1 rounded border px-3 py-2 text-sm font-mono transition-base disabled:cursor-not-allowed disabled:opacity-50 ${
                        format === f
                          ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)] text-[var(--color-accent)]"
                          : "border-[var(--color-border)] text-[var(--color-fg-muted)] hover:border-[var(--color-border-strong)]"
                      }`}
                    >
                      .{f}
                    </button>
                  ))}
                </div>
              </div>
              <p className="text-xs text-[var(--color-fg-muted)]">
                The server streams this with no row limit, but your browser holds the whole
                file in memory until it finishes — an export of many million events can be
                slow and heavy here even though the backend is fine. For a full copy of the
                case, export the case archive instead.
              </p>
            </>
          ) : (
            <>
              <div>
                <label className="mb-2 block text-xs text-[var(--color-fg-muted)]">
                  Field
                </label>
                {/* Native select rather than the Radix one: this list runs to
                    hundreds of fields in a real timeline, and typing a prefix
                    to jump is worth more here than styling. */}
                <select
                  aria-label="Field"
                  className={SELECT_CLASS}
                  disabled={download.active}
                  value={field ?? ""}
                  onChange={(e) => setField(e.target.value || null)}
                >
                  <option value="">Choose a field…</option>
                  {(fieldsQuery.data?.fields ?? []).map((f) => (
                    <option key={f.token} value={f.token}>
                      {f.token}
                      {f.distinct != null ? ` (${f.distinct.toLocaleString()} distinct)` : ""}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="mb-2 block text-xs text-[var(--color-fg-muted)]">
                  Columns
                </label>
                <div className="flex flex-wrap gap-x-4 gap-y-2">
                  {OPTIONAL_COLUMNS.map((column) => {
                    const forced = sortColumn(orderBy) === column;
                    return (
                      <label
                        key={column}
                        className="flex items-center gap-1.5 text-xs text-[var(--color-fg-secondary)]"
                      >
                        <Checkbox
                          checked={columns.includes(column)}
                          disabled={forced || download.active}
                          onCheckedChange={(checked) =>
                            setPicked((prev) =>
                              checked ? [...prev, column] : prev.filter((c) => c !== column),
                            )
                          }
                          aria-label={COLUMN_LABELS[column]}
                        />
                        {COLUMN_LABELS[column]}
                      </label>
                    );
                  })}
                </div>
                <p className="mt-1.5 text-xs text-[var(--color-fg-muted)]">
                  {COLUMN_LABELS[sortColumn(orderBy)]} is written because the file is sorted
                  by it. Value is always the first column.
                </p>
              </div>

              <div className="flex gap-4">
                <div className="flex-1">
                  <label className="mb-2 block text-xs text-[var(--color-fg-muted)]">
                    Separator
                  </label>
                  <SegmentedControl<FieldInventorySeparator>
                    value={separator}
                    onChange={setSeparator}
                    disabled={download.active}
                    options={SEPARATORS.map((s) => ({ id: s.id, label: s.label }))}
                  />
                </div>
                <div className="flex-1">
                  <label className="mb-2 block text-xs text-[var(--color-fg-muted)]">
                    Order
                  </label>
                  <select
                    aria-label="Order"
                    className={SELECT_CLASS}
                    disabled={download.active}
                    value={orderBy}
                    onChange={(e) => setOrderBy(e.target.value as FieldInventoryOrder)}
                  >
                    {ORDERINGS.map((o) => (
                      <option key={o.id} value={o.id}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <p className="text-xs text-[var(--color-fg-muted)]">
                {distinct != null
                  ? `About ${distinct.toLocaleString()} rows before filtering. `
                  : ""}
                Times are ISO-8601 UTC, the same format the events export writes, so the
                two files join. A value only ever seen on events without a timestamp gets
                empty time cells rather than a made-up one.
              </p>
            </>
          )}

          {download.active && (
            <TransferProgressRow
              label={mode === "events" ? `Downloading .${format}` : `Downloading .${inventoryExt}`}
              state={download.state}
              // Nothing is written to disk until the transfer completes, so
              // cancelling simply drops it.
              onCancel={download.cancel}
              cancelLabel="Cancel download"
            />
          )}
          {download.error && (
            <p className="text-xs text-[var(--color-danger)]">{download.error}</p>
          )}
          <div className="flex justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Cancel</Button>
            </DialogClose>
            <Button
              variant="accent"
              size="sm"
              disabled={download.active || (mode === "inventory" && !field)}
              onClick={() => download.submit()}
            >
              <Download size={13} />
              {download.active
                ? "Downloading…"
                : `Download .${mode === "events" ? format : inventoryExt}`}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
