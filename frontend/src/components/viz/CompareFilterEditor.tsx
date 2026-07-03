import { useState } from "react";
import { Plus } from "lucide-react";
import type { EventFilters } from "@/api/types";
import type { VizFieldInfo } from "@/api/types";
import { FilterChips } from "@/components/explorer/FilterChips";
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

  const addFieldFilter = () => {
    if (!fieldKey || !fieldValue) return;
    onChange({ ...filters, filters: { ...filters.filters, [fieldKey]: fieldValue } });
    setFieldValue("");
  };

  const handleRemove = (key: string, fieldKey?: string) => {
    const next: EventFilters = { ...filters };
    if (key === "q") delete next.q;
    if (key === "filters" && fieldKey && next.filters) {
      const { [fieldKey]: _removed, ...rest } = next.filters;
      next.filters = Object.keys(rest).length > 0 ? rest : undefined;
      if (next.filters === undefined) delete next.filters;
    }
    onChange(next);
  };

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
        <Select value={fieldKey || undefined} onValueChange={setFieldKey}>
          <SelectTrigger className="h-7 flex-1 text-xs">
            <SelectValue placeholder="Field…" />
          </SelectTrigger>
          <SelectContent>
            {fields.map((f) => (
              <SelectItem key={f.token} value={f.token}>
                {f.token}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          value={fieldValue}
          onChange={(e) => setFieldValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") addFieldFilter();
          }}
          placeholder="value"
          className="h-7 flex-1 text-xs"
        />
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
