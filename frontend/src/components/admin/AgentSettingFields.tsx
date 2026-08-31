import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, XCircle } from "lucide-react";
import { adminApi } from "@/api/admin";
import type { InstanceAgentMeta, InstanceSetting } from "@/api/settings";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Checkbox";
import { Input } from "@/components/ui/Input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/Select";
import { Spinner } from "@/components/ui/Spinner";

/** The agent group's three fields that no generic control can render: a model
 * name the endpoint itself can enumerate, a deny-list over a tool catalogue the
 * settings payload carries separately, and a headers object that wants a
 * textarea rather than one line of JSON.
 *
 * They are controls, not a page: each one edits the same draft entry the
 * generic rows do, so the page keeps exactly one Save. This file exists because
 * `AdminAgentPage` used to be a second console with its own endpoint and its own
 * save button (retired with the `agent_settings` row, migration 0033) — the
 * widgets were worth keeping, the surface was not.
 */

/** What a custom control needs from the page. `value` is the effective value
 * (draft if edited, stored otherwise); `onChange` writes into the page's draft. */
export interface FieldControlProps {
  setting: InstanceSetting;
  value: unknown;
  disabled: boolean;
  onChange: (value: unknown) => void;
  /** Other settings in the payload, for controls that need a sibling's value. */
  siblings: Map<string, InstanceSetting>;
  /** Draft values, so a control sees an unsaved sibling (a just-typed URL). */
  draft: Record<string, unknown>;
  agent: InstanceAgentMeta;
}

/** The effective value of a sibling field: the unsaved draft entry wins, since
 * a model listing must reflect the endpoint the admin is *currently* typing. */
function effective(
  field: string,
  siblings: Map<string, InstanceSetting>,
  draft: Record<string, unknown>,
): string {
  const drafted = draft[field];
  if (field in draft) return drafted == null ? "" : String(drafted);
  const stored = siblings.get(field)?.value;
  return stored == null ? "" : String(stored);
}

export function AgentModelField({
  value,
  disabled,
  onChange,
  siblings,
  draft,
}: FieldControlProps) {
  /** Free-text entry: forced when the endpoint offers no listing, opt-in
   * otherwise (a model the listing omits is still a legitimate value). */
  const [manual, setManual] = useState(false);

  const baseUrl = effective("agent_api_base_url", siblings, draft);
  const typedKey = effective("agent_api_key", siblings, draft);
  const provider = effective("agent_provider", siblings, draft);
  const keyStored = !!siblings.get("agent_api_key")?.value_set;

  // Debounced so typing a base URL doesn't fire a request per keystroke at a
  // half-written host.
  const [creds, setCreds] = useState({ api_base_url: "", api_key: "" });
  useEffect(() => {
    const id = setTimeout(
      () => setCreds({ api_base_url: baseUrl, api_key: typedKey }),
      600,
    );
    return () => clearTimeout(id);
  }, [baseUrl, typedKey]);

  // The key is only sent when the admin typed a new one; otherwise the backend
  // falls back to the stored/env-pinned key, which the browser never holds.
  // An env-pinned model can't be changed here, so don't bother the endpoint.
  const canList =
    !!creds.api_base_url && (!!creds.api_key || keyStored) && !disabled;
  const modelsQuery = useQuery({
    queryKey: ["admin", "agent-models", creds, provider],
    queryFn: () =>
      adminApi.listAgentModels({
        api_base_url: creds.api_base_url,
        provider: provider || undefined,
        ...(creds.api_key ? { api_key: creds.api_key } : {}),
      }),
    enabled: canList,
    // The operator's own endpoint, but still a network call — don't re-poll it.
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
  const models = modelsQuery.data?.models ?? [];
  const current = value == null ? "" : String(value);

  return (
    <div className="w-72">
      {disabled || manual || models.length === 0 ? (
        <Input
          value={current}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          placeholder="gpt-4o-mini"
        />
      ) : (
        <Select value={current || undefined} onValueChange={onChange}>
          <SelectTrigger>
            <SelectValue placeholder="Select model" />
          </SelectTrigger>
          <SelectContent>
            {/* A saved model the endpoint no longer lists still has to be
                selectable, or opening the dropdown would silently drop it. */}
            {(models.includes(current) || !current
              ? models
              : [current, ...models]
            ).map((m) => (
              <SelectItem key={m} value={m}>
                {m}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      {!disabled && (
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-[var(--color-fg-muted)]">
          {modelsQuery.isFetching ? (
            <>
              <Spinner size={10} /> Loading models from the endpoint…
            </>
          ) : models.length > 0 ? (
            <>
              <span>
                {models.length} model{models.length === 1 ? "" : "s"} from the
                endpoint.
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setManual((m) => !m)}
              >
                {manual ? "Choose from the list" : "Enter manually"}
              </Button>
            </>
          ) : canList ? (
            <span>
              The endpoint listed no models — enter the name manually.
            </span>
          ) : (
            <span>
              Set the endpoint URL and key to load the endpoint&rsquo;s model
              list.
            </span>
          )}
        </div>
      )}
    </div>
  );
}

export function AgentToolsField({
  value,
  disabled,
  onChange,
  agent,
}: FieldControlProps) {
  const denied = new Set(Array.isArray(value) ? (value as string[]) : []);
  return (
    <div className="max-h-64 w-72 space-y-0.5 overflow-y-auto rounded border border-[var(--color-border)] p-1.5">
      {agent.tools.map((tool) => (
        <label
          key={tool.name}
          className={`flex items-start gap-2 rounded px-1.5 py-1 hover:bg-[var(--color-bg-elevated)] ${
            disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"
          }`}
        >
          <Checkbox
            checked={!denied.has(tool.name)}
            disabled={disabled}
            onCheckedChange={(checked) => {
              const next = new Set(denied);
              if (checked === true) next.delete(tool.name);
              else next.add(tool.name);
              // An empty deny-list is "no opinion", which clears the override
              // rather than storing [].
              onChange(next.size > 0 ? [...next].sort() : null);
            }}
            className="mt-0.5"
          />
          <span className="min-w-0 text-xs">
            <span className="font-mono">{tool.name}</span>
            <span className="block text-xs text-[var(--color-fg-muted)]">
              {tool.description}
            </span>
          </span>
        </label>
      ))}
    </div>
  );
}

export function AgentHeadersField({
  value,
  disabled,
  onChange,
}: FieldControlProps) {
  // The page parses this back with JSON.parse on save (kind: "json"), so the
  // control edits text, not an object — a half-typed object is not valid JSON.
  const text =
    typeof value === "string"
      ? value
      : value == null
        ? ""
        : JSON.stringify(value, null, 2);
  return (
    <textarea
      value={text}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      rows={4}
      spellCheck={false}
      placeholder={'{\n  "X-Custom-Header": "value"\n}'}
      className="w-72 rounded border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] px-3 py-2 font-mono text-xs text-[var(--color-fg-primary)] transition-base placeholder:text-[var(--color-fg-muted)] focus:border-[var(--color-accent)] focus:outline-none disabled:opacity-40"
    />
  );
}

/** Field name → the control that replaces the generic editor for it. */
export const AGENT_FIELD_CONTROLS: Record<
  string,
  (props: FieldControlProps) => React.ReactNode
> = {
  agent_model: AgentModelField,
  agent_disabled_tools: AgentToolsField,
  agent_extra_headers: AgentHeadersField,
};

/**
 * Advisory guard-rails on the *combination* of resolved values, plus the
 * connection test. Both belong above the agent rows: the warnings describe
 * fields the operator is about to edit, and "is this endpoint actually
 * reachable" is the question the whole group exists to answer.
 */
export function AgentSectionHeader({
  agent,
  onTest,
  dirty,
}: {
  agent: InstanceAgentMeta;
  /** Saves any pending edits, then re-probes. Resolves to the probe verdict. */
  onTest: () => Promise<boolean>;
  dirty: boolean;
}) {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  const test = async () => {
    setTesting(true);
    setResult(null);
    setError(null);
    try {
      setResult(await onTest());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="mb-2 space-y-2">
      {agent.warnings.map((w) => (
        <div
          key={w}
          className="flex items-start gap-2 rounded border border-[var(--color-border)] bg-[var(--color-warning-dim)] p-3 text-xs text-[var(--color-fg-primary)]"
        >
          <AlertTriangle
            size={13}
            className="mt-0.5 shrink-0 text-[var(--color-warning)]"
          />
          <span>{w}</span>
        </div>
      ))}
      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" disabled={testing} onClick={test}>
          {testing ? "Testing…" : "Test connection"}
        </Button>
        <span className="text-xs text-[var(--color-fg-muted)]">
          {dirty
            ? "Saves the pending changes first, then probes the endpoint."
            : null}
        </span>
        {result === true && (
          <span className="flex items-center gap-1 text-xs text-[var(--color-success)]">
            <CheckCircle2 size={14} /> Reachable
          </span>
        )}
        {result === false && (
          <span className="flex items-center gap-1 text-xs text-[var(--color-danger)]">
            <XCircle size={14} /> Unreachable
          </span>
        )}
        {error && (
          <span className="text-xs text-[var(--color-danger)]">{error}</span>
        )}
      </div>
    </div>
  );
}
