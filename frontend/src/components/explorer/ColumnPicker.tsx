/**
 * ColumnPicker — toolbar popover for configuring event grid columns.
 *
 * Fetches the timeline's field list from /fields (top-level + dynamic
 * attributes) and renders a searchable checkbox list.  Selection is persisted
 * to the UI store (localStorage) via setVisibleColumns.
 */
import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Columns3, RotateCcw, Search } from "lucide-react";
import { eventsApi } from "@/api/events";
import { useUiStore, DEFAULT_COLUMNS } from "@/stores/ui";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import {
  Popover,
  PopoverTrigger,
  PopoverContent,
} from "@/components/ui/Popover";
import { cn } from "@/lib/cn";

interface Props {
  caseId: string;
  timelineId: string;
}

/** Human-readable labels for the built-in top-level columns. */
const TOP_LEVEL_LABELS: Record<string, string> = {
  timestamp: "Timestamp",
  source_id: "Source",
  artifact: "Artifact",
  artifact_long: "Artifact (long)",
  display_name: "Display Name",
  message: "Message",
  timestamp_desc: "Timestamp Desc",
};

function ColumnRow({
  id,
  label,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  checked: boolean;
  onChange: (id: string, checked: boolean) => void;
}) {
  return (
    <label
      className={cn(
        "flex items-center gap-2.5 rounded px-2 py-1.5 cursor-pointer select-none",
        "text-xs text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)] transition-base",
      )}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(id, e.target.checked)}
        className="h-3.5 w-3.5 cursor-pointer rounded border-[var(--color-border-strong)] accent-[var(--color-accent)]"
      />
      <span className={cn("flex-1 truncate", checked && "text-[var(--color-fg-primary)]")}>
        {label}
      </span>
    </label>
  );
}

export function ColumnPicker({ caseId, timelineId }: Props) {
  const [search, setSearch] = useState("");
  const tlKey = `${caseId}/${timelineId}`;
  const visibleColumns = useUiStore((s) => s.visibleColumnsByTimeline[tlKey] ?? DEFAULT_COLUMNS);
  const setVisibleColumnsStore = useUiStore((s) => s.setVisibleColumns);
  const setVisibleColumns = (cols: string[]) => setVisibleColumnsStore(tlKey, cols);

  const { data: fields, isLoading } = useQuery({
    queryKey: ["fields", caseId, timelineId],
    queryFn: () => eventsApi.fields(caseId, timelineId),
    staleTime: 5 * 60 * 1000,
  });

  const allColumns = useMemo(() => {
    const top = (fields?.top_level ?? DEFAULT_COLUMNS).map((id) => ({
      id,
      label: TOP_LEVEL_LABELS[id] ?? id,
      group: "Standard",
    }));
    const attrs = (fields?.attributes ?? []).map((id) => ({
      id,
      label: id,
      group: "Dynamic fields",
    }));
    return [...top, ...attrs];
  }, [fields]);

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    return q
      ? allColumns.filter(
          (c) => c.id.toLowerCase().includes(q) || c.label.toLowerCase().includes(q),
        )
      : allColumns;
  }, [allColumns, search]);

  const visibleSet = new Set(visibleColumns);

  const toggle = (id: string, checked: boolean) => {
    if (checked) {
      // Append at end, preserving existing order
      if (!visibleSet.has(id)) {
        setVisibleColumns([...visibleColumns, id]);
      }
    } else {
      setVisibleColumns(visibleColumns.filter((c) => c !== id));
    }
  };

  // Group the filtered list
  const standard = filtered.filter((c) => c.group === "Standard");
  const dynamic = filtered.filter((c) => c.group === "Dynamic fields");

  const activeCount = visibleColumns.filter((c) => c !== "_select" && c !== "_expand").length;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm">
          <Columns3 size={13} />
          Columns
          {activeCount > 0 && (
            <span className="ml-1 rounded bg-[var(--color-accent-dim)] px-1 text-xs font-semibold text-[var(--color-accent)]">
              {activeCount}
            </span>
          )}
        </Button>
      </PopoverTrigger>

      <PopoverContent className="w-64 p-0" align="end">
        {/* Search */}
        <div className="border-b border-[var(--color-border)] p-2">
          <div className="relative">
            <Search
              size={12}
              className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--color-fg-muted)]"
            />
            <Input
              placeholder="Search fields…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-6"
            />
          </div>
        </div>

        {/* Column list */}
        <div className="max-h-72 overflow-y-auto px-1 py-1">
          {isLoading ? (
            <div className="flex items-center justify-center py-4">
              <Spinner size={16} />
            </div>
          ) : (
            <>
              {standard.length > 0 && (
                <div>
                  <p className="px-2 pb-1 pt-1.5 text-xs font-semibold uppercase tracking-wider text-[var(--color-fg-secondary)]">
                    Standard
                  </p>
                  {standard.map((c) => (
                    <ColumnRow
                      key={c.id}
                      id={c.id}
                      label={c.label}
                      checked={visibleSet.has(c.id)}
                      onChange={toggle}
                    />
                  ))}
                </div>
              )}

              {dynamic.length > 0 && (
                <div className={standard.length > 0 ? "mt-1 border-t border-[var(--color-border-subtle)] pt-1" : ""}>
                  <p className="px-2 pb-1 pt-1.5 text-xs font-semibold uppercase tracking-wider text-[var(--color-fg-secondary)]">
                    Dynamic fields
                  </p>
                  {dynamic.map((c) => (
                    <ColumnRow
                      key={c.id}
                      id={c.id}
                      label={c.label}
                      checked={visibleSet.has(c.id)}
                      onChange={toggle}
                    />
                  ))}
                </div>
              )}

              {filtered.length === 0 && (
                <p className="px-2 py-3 text-xs text-[var(--color-fg-muted)]">
                  No fields match &ldquo;{search}&rdquo;
                </p>
              )}
            </>
          )}
        </div>

        {/* Reset footer */}
        <div className="border-t border-[var(--color-border)] p-2">
          <button
            className="flex items-center gap-1.5 text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] transition-base"
            onClick={() => setVisibleColumns(DEFAULT_COLUMNS)}
          >
            <RotateCcw size={10} /> Reset to defaults
          </button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
