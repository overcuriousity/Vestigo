import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, X } from "lucide-react";
import { downloadExport, downloadFieldInventory } from "@/api/export";
import { vizApi } from "@/api/viz";
import { FieldCombo } from "@/components/ui/FieldCombo";
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

/** Mirrors the server's `INVENTORY_MAX_FIELDS`: each extra field multiplies the
 * groups the whole-corpus scan holds, so the cap is a memory bound, not taste. */
const INVENTORY_MAX_FIELDS = 8;

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

  // One picked field per entry, in the order chosen — the export's column order.
  // An empty trailing slot is rendered separately rather than held here, so
  // "nothing chosen yet" is never a field in the request.
  const [fields, setFields] = useState<string[]>([]);
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
  // The first field's distinct count is a floor, not an estimate, once a second
  // field is in play: combining can only split groups, never merge them. Said
  // that way rather than dropped, because "how long is this file" is the
  // question the number is there to answer.
  const distinct = fieldsQuery.data?.fields.find((f) => f.token === fields[0])?.distinct ?? null;

  const fieldOptions = useMemo(
    () =>
      (fieldsQuery.data?.fields ?? []).map((f) => ({
        value: f.token,
        label: f.token,
        hint: f.distinct != null ? `${f.distinct.toLocaleString()} distinct` : undefined,
      })),
    [fieldsQuery.data],
  );

  /** Set slot *index* to *token*; an empty token removes the slot entirely.
   *
   * A field already picked elsewhere is refused by omitting it from the other
   * slots' options, so this only has to handle the add/replace/remove cases. */
  const setFieldAt = (index: number, token: string) => {
    setFields((prev) => {
      const next = [...prev];
      if (!token) next.splice(index, 1);
      else next[index] = token;
      return next;
    });
  };

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
            { fields, columns, separator, orderBy },
            o,
          ),
    onSuccess: () => setOpen(false),
  });

  const description =
    mode === "events"
      ? total !== null
        ? `Download all ${total.toLocaleString()} matching events with current filters applied.`
        : "Download all matching events with current filters applied."
      : "Download one row per distinct value of a field — or per distinct combination of several — with how often it occurs and when it was first and last seen, computed over the same filtered view.";

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
                  {fields.length > 1 ? "Fields" : "Field"}
                </label>
                {/* One `FieldCombo` per chosen field plus an empty one below,
                    so a second field is always one click away and never in the
                    way: nothing is required beyond the first. `FieldCombo` is
                    what every other "which field?" surface uses — this list
                    runs to hundreds of tokens in a real timeline, so typing a
                    prefix to narrow it is the whole point — and the
                    placeholder still states which of the three states the list
                    is in. Without it a failed or in-flight `viz/fields`
                    renders as an empty picker with the Download button
                    permanently disabled and nothing saying why. */}
                <div className="space-y-2">
                  {/* The empty trailing slot disappears at the cap — an
                      offer the server would refuse is worse than no offer. */}
                  {(fields.length < INVENTORY_MAX_FIELDS ? [...fields, ""] : fields).map(
                    (token, index) => {
                      const first = index === 0;
                      const taken = new Set(fields.filter((_, i) => i !== index));
                      return (
                        <div key={`${token || "empty"}-${index}`} className="flex gap-2">
                          <FieldCombo
                            className="flex-1"
                            aria-label={first ? "Field" : `Field ${index + 1}`}
                            disabled={
                              download.active || fieldsQuery.isPending || fieldsQuery.isError
                            }
                            placeholder={
                              fieldsQuery.isPending
                                ? "Loading fields…"
                                : fieldsQuery.isError
                                  ? "Could not load fields"
                                  : first
                                    ? "Choose a field…"
                                    : "Add another field (optional)…"
                            }
                            options={fieldOptions.filter((o) => !taken.has(o.value))}
                            value={token}
                            onChange={(v) => setFieldAt(index, v)}
                          />
                          {/* An emptied combo commits nothing (`FieldCombo`
                              only emits `""` where the caller offers it as a
                              row), so dropping a field needs its own control
                              rather than a box the analyst can clear. */}
                          {token ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              aria-label={`Remove ${token}`}
                              disabled={download.active}
                              onClick={() => setFieldAt(index, "")}
                            >
                              <X size={13} />
                            </Button>
                          ) : null}
                        </div>
                      );
                    },
                  )}
                </div>
                {fields.length >= INVENTORY_MAX_FIELDS ? (
                  <p className="mt-1.5 text-xs text-[var(--color-fg-muted)]">
                    {INVENTORY_MAX_FIELDS} fields is the most one inventory can combine —
                    each one multiplies the number of groups the scan holds.
                  </p>
                ) : null}
                {fieldsQuery.isError ? (
                  <div className="mt-1.5 flex items-center gap-2">
                    <p className="text-xs text-[var(--color-danger)]">
                      The field list could not be loaded, so there is nothing to inventory yet.
                    </p>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => void fieldsQuery.refetch()}
                      disabled={fieldsQuery.isFetching}
                    >
                      Retry
                    </Button>
                  </div>
                ) : null}
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
                  by it.{" "}
                  {fields.length > 1
                    ? "The value columns come first, one per field, headed with its field name."
                    : "Value is always the first column, headed with the field name."}
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
                  ? fields.length > 1
                    ? `At least ${distinct.toLocaleString()} rows before filtering — combining fields only splits groups further. `
                    : `About ${distinct.toLocaleString()} rows before filtering. `
                  : ""}
                {fields.length > 1
                  ? "A row is written unless every one of its fields is empty, so a value that only ever appears without its partner still gets one, with an empty cell beside it. "
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
              disabled={download.active || (mode === "inventory" && fields.length === 0)}
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
