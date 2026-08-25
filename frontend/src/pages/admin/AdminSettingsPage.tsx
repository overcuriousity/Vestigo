import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Lock, RotateCcw } from "lucide-react";
import { ApiError } from "@/api/client";
import { useHealth } from "@/api/health";
import { settingsApi, type InstanceSetting, type InstanceSettingsResponse } from "@/api/settings";
import { ScanBudgetCard } from "@/components/admin/ScanBudgetCard";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/Select";
import { Spinner } from "@/components/ui/Spinner";
import { Switch } from "@/components/ui/Switch";
import { Tooltip } from "@/components/ui/Tooltip";

/** Edits keyed by field name. A value of `null` means "clear the override".
 * Absent means untouched — only edited fields are ever sent. */
type Draft = Record<string, unknown>;

/** Render a stored value as editable text. Secrets always start empty: the
 * backend never returns them, so the box means "replace", not "current". */
function toText(setting: InstanceSetting): string {
  if (setting.kind === "secret") return "";
  if (setting.value == null) return "";
  if (setting.kind === "json") return JSON.stringify(setting.value);
  return String(setting.value);
}

/** Parse one field's editor text back into what the API expects.
 * Throws for malformed JSON so the save can name the offending field rather
 * than letting the backend reject the whole batch with a pydantic trace. */
function parseValue(setting: InstanceSetting, text: string): unknown {
  const trimmed = text.trim();
  // Empty is ambiguous, and the annotation resolves it: a nullable field means
  // "unset" (storing "" there would leave it reading as customized forever),
  // a plain string means the empty value itself — an empty sigma_rules_path
  // disables the global ruleset. Anything else has no empty form at all.
  if (trimmed === "") return setting.kind === "str" && !setting.nullable ? "" : null;
  if (setting.kind === "int") {
    const n = Number(trimmed);
    if (!Number.isInteger(n)) throw new Error(`${setting.label}: expected a whole number`);
    return n;
  }
  if (setting.kind === "float") {
    const n = Number(trimmed);
    if (Number.isNaN(n)) throw new Error(`${setting.label}: expected a number`);
    return n;
  }
  if (setting.kind === "json") {
    try {
      return JSON.parse(trimmed);
    } catch {
      throw new Error(`${setting.label}: not valid JSON`);
    }
  }
  return trimmed;
}

function SourceBadge({ setting }: { setting: InstanceSetting }) {
  if (setting.source === "env") {
    return (
      <Tooltip content={`Pinned by ${setting.env_var} — edit the deployment to change it.`}>
        <span>
          <Badge variant="muted">
            <Lock size={10} className="mr-1" />
            environment
          </Badge>
        </span>
      </Tooltip>
    );
  }
  if (setting.source === "db") return <Badge variant="accent">customized</Badge>;
  return null;
}

function SettingRow({
  setting,
  draft,
  onChange,
  secretsDisabled,
}: {
  setting: InstanceSetting;
  draft: Draft;
  onChange: (field: string, value: unknown) => void;
  secretsDisabled: boolean;
}) {
  const disabled =
    !setting.editable || (setting.kind === "secret" && secretsDisabled) || !!setting.managed_by;
  const edited = setting.field in draft;
  const draftValue = draft[setting.field];

  const control = () => {
    if (setting.kind === "bool") {
      const checked = edited ? !!draftValue : !!setting.value;
      return (
        <Switch
          checked={checked}
          disabled={disabled}
          onCheckedChange={(v) => onChange(setting.field, v)}
        />
      );
    }
    if (setting.kind === "choice" && setting.choices) {
      const value = edited ? String(draftValue ?? "") : String(setting.value ?? "");
      return (
        <Select value={value} disabled={disabled} onValueChange={(v) => onChange(setting.field, v)}>
          <SelectTrigger className="w-56">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {setting.choices.map((c) => (
              <SelectItem key={c} value={c}>
                {c}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    }
    const text = edited ? String(draftValue ?? "") : toText(setting);
    return (
      <Input
        className="w-72"
        type={setting.kind === "secret" ? "password" : "text"}
        inputMode={setting.kind === "int" || setting.kind === "float" ? "numeric" : undefined}
        value={text}
        disabled={disabled}
        placeholder={
          setting.kind === "secret"
            ? setting.value_set
              ? "•••••••• (stored) — type to replace"
              : "not set"
            : String(setting.default ?? "")
        }
        onChange={(e) => onChange(setting.field, e.target.value)}
      />
    );
  };

  return (
    <div className="flex flex-col gap-2 border-b border-[var(--color-border)] py-3 last:border-b-0 md:flex-row md:items-start md:justify-between md:gap-6">
      <div className="min-w-0 md:flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-[var(--color-fg-primary)]">
            {setting.label}
          </span>
          <SourceBadge setting={setting} />
          {setting.restart_required && <Badge variant="muted">restart required</Badge>}
          {setting.managed_by === "agent" && <Badge variant="muted">set on the Agent tab</Badge>}
        </div>
        <p className="mt-1 text-xs text-[var(--color-fg-muted)]">{setting.help}</p>
        <p className="mt-1 font-mono text-[11px] text-[var(--color-fg-muted)]">{setting.env_var}</p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {control()}
        {setting.source === "db" && setting.editable && (
          <Tooltip content="Clear the stored override and fall back to the default">
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Reset ${setting.label}`}
              onClick={() => onChange(setting.field, null)}
            >
              <RotateCcw size={14} />
            </Button>
          </Tooltip>
        )}
      </div>
    </div>
  );
}

export function AdminSettingsPage() {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Draft>({});
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<number | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["admin", "settings"],
    queryFn: settingsApi.get,
  });
  const health = useHealth();

  const mutation = useMutation({
    mutationFn: (values: Record<string, unknown>) => settingsApi.update(values),
    onSuccess: (resp: InstanceSettingsResponse) => {
      qc.setQueryData(["admin", "settings"], resp);
      // A capability may have flipped (embeddings endpoint, MCP, transfer),
      // and health is what the rest of the app gates on.
      qc.invalidateQueries({ queryKey: ["health"] });
      setSaved(resp.applied?.length ?? 0);
      setDraft({});
      setError(null);
    },
    onError: (e: unknown) => {
      setError(e instanceof ApiError ? e.message : String(e));
      setSaved(null);
    },
  });

  const byGroup = useMemo(() => {
    const map = new Map<string, InstanceSetting[]>();
    for (const s of data?.settings ?? []) {
      const list = map.get(s.group) ?? [];
      list.push(s);
      map.set(s.group, list);
    }
    return map;
  }, [data]);

  if (isLoading || !data) return <Spinner />;

  const setField = (field: string, value: unknown) => {
    setSaved(null);
    setDraft((prev) => ({ ...prev, [field]: value }));
  };

  const save = () => {
    const settings = new Map(data.settings.map((s) => [s.field, s]));
    const values: Record<string, unknown> = {};
    try {
      for (const [field, raw] of Object.entries(draft)) {
        const setting = settings.get(field)!;
        if (raw === null) {
          values[field] = null;
        } else if (setting.kind === "bool") {
          values[field] = !!raw;
        } else if (setting.kind === "choice") {
          values[field] = raw;
        } else {
          values[field] = parseValue(setting, String(raw));
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return;
    }
    mutation.mutate(values);
  };

  const dirty = Object.keys(draft).length;

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4 text-xs text-[var(--color-fg-muted)]">
        Every setting is stored in the database and applied without a restart, except where a
        badge says otherwise. A value pinned in the deployment environment always wins and is
        shown read-only here — clear the variable to make it editable again.
        {data.secrets_mode === "env-only" && (
          <p className="mt-2 text-[var(--color-warning)]">
            VESTIGO_SECRETS_MODE=env-only: secrets cannot be stored in the database on this
            instance. Set them as environment variables.
          </p>
        )}
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded border border-[var(--color-danger)]/40 bg-[var(--color-danger-dim)] p-3 text-xs text-[var(--color-danger)]">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {saved !== null && (
        <p className="text-xs text-[var(--color-accent)]">
          Saved. {saved} override{saved === 1 ? "" : "s"} currently applied.
        </p>
      )}

      {data.groups.map((group) => {
        const settings = byGroup.get(group.key) ?? [];
        if (settings.length === 0) return null;
        return (
          <section key={group.key}>
            <h2 className="text-sm font-semibold text-[var(--color-fg-primary)]">{group.label}</h2>
            <p className="mb-2 text-xs text-[var(--color-fg-muted)]">{group.description}</p>
            {/* The verdict belongs next to the knobs that move it: an operator
                editing the scan budget is the person who needs to know it does
                not currently fit. */}
            {group.key === "scans" && <ScanBudgetCard budget={health.data?.scan_budget} />}
            <div className="rounded-lg border border-[var(--color-border)] px-4">
              {settings.map((s) => (
                <SettingRow
                  key={s.field}
                  setting={s}
                  draft={draft}
                  onChange={setField}
                  secretsDisabled={data.secrets_mode === "env-only"}
                />
              ))}
            </div>
          </section>
        );
      })}

      <div className="sticky bottom-0 flex items-center justify-end gap-3 border-t border-[var(--color-border-strong)] bg-[var(--color-bg-base)] py-3">
        {dirty > 0 && (
          <span className="text-xs text-[var(--color-fg-muted)]">
            {dirty} unsaved change{dirty === 1 ? "" : "s"}
          </span>
        )}
        <Button variant="ghost" disabled={!dirty} onClick={() => setDraft({})}>
          Discard
        </Button>
        <Button variant="accent" disabled={!dirty || mutation.isPending} onClick={save}>
          {mutation.isPending ? "Saving…" : "Save changes"}
        </Button>
      </div>
    </div>
  );
}
