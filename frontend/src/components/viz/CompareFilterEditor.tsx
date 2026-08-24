import { useState } from "react";
import { Plus } from "lucide-react";
import type { EventFilters } from "@/api/types";
import type { VizFieldInfo } from "@/api/types";
import { fieldTokenLabel, fieldValueLabel } from "@/components/viz/lib/fieldDisplay";
import { TIME_FIELDS } from "@/components/viz/lib/timeFields";
import { FilterChips } from "@/components/explorer/FilterChips";
import { removeFilterEntry } from "@/lib/fieldFilters";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/Select";

interface Props {
  filters: EventFilters;
  onChange: (filters: EventFilters) => void;
  /** Chartable fields for the field-filter picker (same list as the rail's field picker). */
  fields: VizFieldInfo[];
}

/**
 * Compact filter editor for the custom comparison layer: free-text search
 * plus field=value equality filters, displayed as the Explorer's own filter
 * chips. Deliberately a subset of the Explorer filter bar (no tag/artifact/
 * exclusion editing in v1) — the common A-vs-B comparisons are a search term
 * or a field value. Time range is not editable here by design: the backend
 * pins the comparison layer to the primary's window (comparability
 * invariant).
 */
export function CompareFilterEditor({ filters, onChange, fields }: Props) {
  const [fieldKey, setFieldKey] = useState<string>("");
  const [fieldValue, setFieldValue] = useState("");
  // A bounded `time:` field has a known, complete value list, and its
  // canonical values are opaque ("1" = Monday). Free text would let the
  // analyst type the label they can see on every axis and build a filter that
  // silently matches nothing — so offer the domain instead of an input.
  const timeDomain = fieldKey ? TIME_FIELDS[fieldKey]?.domain : undefined;

  const addFieldFilter = () => {
    if (!fieldKey || !fieldValue) return;
    const prev = filters.filters?.[fieldKey] ?? [];
    const next = prev.includes(fieldValue) ? prev : [...prev, fieldValue];
    onChange({ ...filters, filters: { ...filters.filters, [fieldKey]: next } });
    setFieldValue("");
  };

  // `FilterChips` renders exclusions, tags and time bounds too — a hand-rolled
  // remover that only knew `q` and `filters` left those chips un-removable, so
  // this shares the Explorer's semantics instead.
  const handleRemove = (key: string, fieldKey?: string, value?: string) =>
    onChange(removeFilterEntry(filters, key, fieldKey, value));

  return (
    <div className="space-y-2">
      <Input
        value={filters.q ?? ""}
        onChange={(e) => {
          const q = e.target.value;
          const next = { ...filters };
          if (q) next.q = q;
          else delete next.q;
          onChange(next);
        }}
        placeholder="Search text…"
        className="h-7 text-xs"
      />
      <div className="flex items-center gap-1">
        <Select
          value={fieldKey || undefined}
          onValueChange={(v) => {
            setFieldKey(v);
            // The pending value belongs to the old field's vocabulary.
            setFieldValue("");
          }}
        >
          <SelectTrigger className="h-7 flex-1 text-xs">
            <SelectValue placeholder="Field…" />
          </SelectTrigger>
          <SelectContent>
            {fields.map((f) => (
              <SelectItem key={f.token} value={f.token}>
                {fieldTokenLabel(f.token)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {timeDomain ? (
          <Select value={fieldValue || undefined} onValueChange={setFieldValue}>
            <SelectTrigger className="h-7 flex-1 text-xs">
              <SelectValue placeholder="value" />
            </SelectTrigger>
            <SelectContent>
              {timeDomain.map((v) => (
                // Label is display-only; the canonical value is what is
                // stored, so relabelling never invalidates a saved filter.
                <SelectItem key={v} value={v}>
                  {fieldValueLabel(fieldKey, v)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <Input
            value={fieldValue}
            onChange={(e) => setFieldValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") addFieldFilter();
            }}
            placeholder="value"
            className="h-7 flex-1 text-xs"
          />
        )}
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-1.5"
          onClick={addFieldFilter}
          disabled={!fieldKey || !fieldValue}
          aria-label="Add field filter"
        >
          <Plus size={13} />
        </Button>
      </div>
      <FilterChips filters={filters} onRemove={handleRemove} />
    </div>
  );
}
