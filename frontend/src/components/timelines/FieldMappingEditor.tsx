import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Merge, Plus, X } from "lucide-react";
import { timelinesApi } from "@/api/timelines";
import { suggestGroups } from "@/lib/fieldSuggest";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import type { FieldCoverageEntry, Source } from "@/api/types";

export type FieldMappings = Record<string, string[]>;

interface Props {
  caseId: string;
  sourceIds: string[];
  sources: Source[];
  value: FieldMappings;
  onChange: (next: FieldMappings) => void;
}

function sourceName(sources: Source[], id: string): string {
  return sources.find((s) => s.id === id)?.name ?? id.slice(0, 8);
}

/**
 * The field-aggregation editor (wizard step 3 / edit dialog): a coverage
 * table of every raw attribute key across the selected sources with sample
 * values, name+value-shape merge suggestions, and manual grouping into named
 * canonical fields. Mappings are optional — an empty value is valid.
 */
export function FieldMappingEditor({ caseId, sourceIds, sources, value, onChange }: Props) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [newGroupName, setNewGroupName] = useState("");

  const { data: coverage, isLoading, error } = useQuery({
    queryKey: ["field-coverage", caseId, [...sourceIds].sort().join(",")],
    queryFn: () => timelinesApi.fieldCoverage(caseId, sourceIds),
    enabled: sourceIds.length > 0,
    staleTime: 60_000,
  });

  const mappedRaws = useMemo(() => new Set(Object.values(value).flat()), [value]);

  const suggestions = useMemo(() => {
    if (!coverage) return [];
    const inputs = coverage.fields.map((f: FieldCoverageEntry) => ({
      key: f.key,
      samples: f.sources.flatMap((s) => s.samples),
      sourceIds: f.sources.map((s) => s.source_id),
    }));
    return suggestGroups(inputs).filter(
      (g) => !g.fields.some((f) => mappedRaws.has(f)) && !(g.name in value),
    );
  }, [coverage, mappedRaws, value]);

  const toggleSelect = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const addGroup = (name: string, fields: string[]) => {
    const trimmed = name.trim();
    if (!trimmed || fields.length === 0) return;
    onChange({ ...value, [trimmed]: fields });
    setSelected(new Set());
    setNewGroupName("");
  };

  const removeGroup = (name: string) => {
    const next = { ...value };
    delete next[name];
    onChange(next);
  };

  const removeRawFromGroup = (name: string, raw: string) => {
    const remaining = value[name].filter((r) => r !== raw);
    if (remaining.length === 0) removeGroup(name);
    else onChange({ ...value, [name]: remaining });
  };

  if (sourceIds.length === 0) {
    return (
      <p className="text-xs text-[var(--color-fg-muted)]">
        Select at least one source to see its fields.
      </p>
    );
  }
  if (isLoading) return <Spinner size={16} />;
  if (error) {
    return <p className="text-xs text-[var(--color-danger)]">{(error as Error).message}</p>;
  }
  if (!coverage) return null;

  return (
    <div className="space-y-4">
      {/* Active canonical groups */}
      {Object.keys(value).length > 0 && (
        <div className="space-y-1.5">
          {Object.entries(value).map(([name, raws]) => (
            <div
              key={name}
              className="flex items-center gap-2 rounded border border-[var(--color-accent)]/40 bg-[var(--color-accent-dim)] px-3 py-1.5"
            >
              <Merge size={12} className="shrink-0 text-[var(--color-accent)]" />
              <span className="font-mono text-xs font-medium text-[var(--color-fg-primary)]">
                {name}
              </span>
              <span className="text-xs text-[var(--color-fg-muted)]">←</span>
              <div className="flex flex-1 flex-wrap items-center gap-1">
                {raws.map((raw) => (
                  <Badge key={raw} variant="muted" className="font-mono">
                    {raw}
                    <button
                      type="button"
                      title={`Remove ${raw} from ${name}`}
                      onClick={() => removeRawFromGroup(name, raw)}
                      className="ml-1 opacity-60 hover:opacity-100"
                    >
                      <X size={9} />
                    </button>
                  </Badge>
                ))}
              </div>
              <Button
                variant="ghost"
                size="icon"
                title={`Remove mapping ${name}`}
                onClick={() => removeGroup(name)}
              >
                <X size={12} />
              </Button>
            </div>
          ))}
        </div>
      )}

      {/* Suggestions */}
      {suggestions.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-xs text-[var(--color-fg-muted)]">Suggested merges:</p>
          {suggestions.map((g) => (
            <div
              key={g.name}
              className="flex items-center gap-2 rounded border border-dashed border-[var(--color-border-strong)] px-3 py-1.5"
            >
              <div className="min-w-0 flex-1">
                <span className="font-mono text-xs text-[var(--color-fg-secondary)]">
                  {g.name} ← {g.fields.join(", ")}
                </span>
                <p className="text-[10px] text-[var(--color-fg-muted)]">{g.reason}</p>
              </div>
              <Button variant="outline" size="sm" onClick={() => addGroup(g.name, g.fields)}>
                <Plus size={11} /> Merge
              </Button>
            </div>
          ))}
        </div>
      )}

      {/* Coverage table */}
      <div className="max-h-64 overflow-y-auto rounded border border-[var(--color-border)]">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-[var(--color-bg-surface)]">
            <tr className="text-left text-[var(--color-fg-muted)]">
              <th className="px-3 py-1.5 font-medium">Field</th>
              <th className="px-3 py-1.5 font-medium">Sources</th>
              <th className="px-3 py-1.5 font-medium">Sample values</th>
            </tr>
          </thead>
          <tbody>
            {coverage.fields.map((f) => {
              const mapped = mappedRaws.has(f.key);
              return (
                <tr
                  key={f.key}
                  className={`border-t border-[var(--color-border)] ${
                    mapped ? "opacity-40" : ""
                  }`}
                >
                  <td className="px-3 py-1.5">
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        disabled={mapped}
                        checked={selected.has(f.key)}
                        onChange={() => toggleSelect(f.key)}
                        className="rounded border-[var(--color-border-strong)] accent-[var(--color-accent)]"
                      />
                      <span className="font-mono text-[var(--color-fg-secondary)]">{f.key}</span>
                    </label>
                  </td>
                  <td className="px-3 py-1.5">
                    <div className="flex flex-wrap gap-1">
                      {f.sources.map((s) => (
                        <span key={s.source_id} title={`${s.count} values`}>
                          <Badge variant="muted">{sourceName(sources, s.source_id)}</Badge>
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="max-w-[16rem] truncate px-3 py-1.5 font-mono text-[var(--color-fg-muted)]">
                    {f.sources.flatMap((s) => s.samples).slice(0, 3).join(" · ")}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Manual merge of the selected fields */}
      <div className="flex items-center gap-2">
        <Input
          placeholder="Canonical name, e.g. ip_address"
          value={newGroupName}
          onChange={(e) => setNewGroupName(e.target.value)}
          className="max-w-56"
        />
        <Button
          variant="outline"
          size="sm"
          disabled={selected.size < 1 || !newGroupName.trim()}
          onClick={() => addGroup(newGroupName, [...selected].sort())}
        >
          <Merge size={12} /> Merge {selected.size > 0 ? `${selected.size} field${selected.size !== 1 ? "s" : ""}` : "selected"}
        </Button>
      </div>
    </div>
  );
}
