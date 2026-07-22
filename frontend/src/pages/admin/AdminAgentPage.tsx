import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, XCircle } from "lucide-react";
import { adminApi } from "@/api/admin";
import { ApiError } from "@/api/client";
import { healthApi } from "@/api/health";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Checkbox";
import { Input } from "@/components/ui/Input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/Select";
import { Spinner } from "@/components/ui/Spinner";

const PROVIDERS = ["openai", "anthropic"] as const;
const EFFORTS = ["off", "low", "medium", "high", "max"] as const;
const FIDELITIES = ["full", "message", "minimal", "auto"] as const;

/** Radix cannot carry an empty `value`, so "back to unset" needs a sentinel.
 * `set()` maps it to "" and `buildPatch` already sends `null` for that — which
 * is what clears the DB row and restores the default. */
const UNSET = "__unset__";
const unsetValue = (v: string) => (v === UNSET ? "" : v);

/** Local editable form state, mirroring the DB-editable AgentSettingsUpdate fields
 * (see `src/vestigo/api/routers/admin.py::AgentSettingsUpdate`). `apiKey` and
 * `clearApiKey` are handled separately since the backend never returns the
 * plaintext key — only `api_key_set`. */
interface FormState {
  model: string;
  provider: string;
  api_base_url: string;
  user_agent: string;
  extra_headers: string; // raw JSON text, validated on submit
  max_turns: string;
  reasoning_effort: string;
  context_window: string;
  tool_fidelity: string;
  /** Sorted tool names the admin has hard-denied. */
  disabled_tools: string[];
}

const EMPTY_FORM: FormState = {
  model: "",
  provider: "",
  api_base_url: "",
  user_agent: "",
  extra_headers: "",
  max_turns: "",
  reasoning_effort: "",
  context_window: "",
  tool_fidelity: "",
  disabled_tools: [],
};

function toFormState(effective: Record<string, unknown>): FormState {
  const str = (v: unknown) => (v == null ? "" : String(v));
  return {
    model: str(effective.model),
    provider: str(effective.provider),
    api_base_url: str(effective.api_base_url),
    user_agent: str(effective.user_agent),
    extra_headers:
      effective.extra_headers && typeof effective.extra_headers === "object"
        ? JSON.stringify(effective.extra_headers, null, 2)
        : "",
    max_turns: str(effective.max_turns),
    reasoning_effort: str(effective.reasoning_effort),
    context_window: str(effective.context_window),
    tool_fidelity: str(effective.tool_fidelity),
    disabled_tools: Array.isArray(effective.disabled_tools)
      ? [...(effective.disabled_tools as string[])].sort()
      : [],
  };
}

/** Diff the current form against the loaded baseline and build the PUT patch body.
 * Only changed fields are included so untouched settings (including any DB override
 * on a field the form never surfaced, e.g. cleared-then-reloaded) are left alone. */
function buildPatch(
  form: FormState,
  baseline: FormState,
  apiKeyInput: string,
  clearApiKey: boolean,
): { patch: Record<string, unknown>; error: string | null } {
  const patch: Record<string, unknown> = {};

  if (form.model !== baseline.model) patch.model = form.model || null;
  if (form.provider !== baseline.provider) patch.provider = form.provider || null;
  if (form.api_base_url !== baseline.api_base_url) patch.api_base_url = form.api_base_url || null;
  if (form.user_agent !== baseline.user_agent) patch.user_agent = form.user_agent || null;

  if (form.max_turns !== baseline.max_turns) {
    if (form.max_turns === "") {
      patch.max_turns = null;
    } else {
      const n = Number(form.max_turns);
      if (!Number.isInteger(n) || n < 1 || n > 100) {
        return { patch: {}, error: "Max turns must be an integer between 1 and 100." };
      }
      patch.max_turns = n;
    }
  }

  if (form.reasoning_effort !== baseline.reasoning_effort) {
    patch.reasoning_effort = form.reasoning_effort || null;
  }

  if (form.context_window !== baseline.context_window) {
    if (form.context_window === "") {
      patch.context_window = null;
    } else {
      const n = Number(form.context_window);
      if (!Number.isInteger(n) || n < 1024) {
        return { patch: {}, error: "Context window must be an integer ≥ 1024 tokens." };
      }
      patch.context_window = n;
    }
  }

  if (form.tool_fidelity !== baseline.tool_fidelity) {
    patch.tool_fidelity = form.tool_fidelity || null;
  }
  if (JSON.stringify(form.disabled_tools) !== JSON.stringify(baseline.disabled_tools)) {
    patch.disabled_tools = form.disabled_tools.length > 0 ? form.disabled_tools : null;
  }

  if (form.extra_headers !== baseline.extra_headers) {
    if (form.extra_headers.trim() === "") {
      patch.extra_headers = null;
    } else {
      let parsed: unknown;
      try {
        parsed = JSON.parse(form.extra_headers);
      } catch {
        return { patch: {}, error: "Extra headers must be valid JSON." };
      }
      if (
        typeof parsed !== "object" ||
        parsed === null ||
        Array.isArray(parsed) ||
        !Object.values(parsed as Record<string, unknown>).every((v) => typeof v === "string")
      ) {
        return { patch: {}, error: "Extra headers must be a JSON object of string values." };
      }
      patch.extra_headers = parsed;
    }
  }

  if (clearApiKey) {
    patch.api_key = null;
  } else if (apiKeyInput.trim() !== "") {
    patch.api_key = apiKeyInput;
  }

  return { patch, error: null };
}

export function AdminAgentPage() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["admin", "agent-settings"],
    queryFn: () => adminApi.getAgentSettings(),
  });

  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [baseline, setBaseline] = useState<FormState>(EMPTY_FORM);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [clearApiKey, setClearApiKey] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<"reachable" | "unreachable" | null>(null);
  /** Free-text model entry: forced when the endpoint offers no listing, opt-in
   * otherwise (a model the listing omits is still a legitimate value). */
  const [customModel, setCustomModel] = useState(false);

  // Debounced so typing a base URL doesn't fire a request per keystroke at a
  // half-written host.
  const [modelQueryCreds, setModelQueryCreds] = useState({ api_base_url: "", api_key: "" });
  useEffect(() => {
    const id = setTimeout(
      () => setModelQueryCreds({ api_base_url: form.api_base_url, api_key: apiKeyInput }),
      600,
    );
    return () => clearTimeout(id);
  }, [form.api_base_url, apiKeyInput]);

  // The key is only sent when the admin typed a new one; otherwise the backend
  // falls back to the stored/env-pinned key, which the browser never holds.
  // An env-pinned model can't be changed here, so don't bother the endpoint.
  const canListModels =
    !!modelQueryCreds.api_base_url &&
    (!!modelQueryCreds.api_key || !!data?.effective.api_key_set) &&
    data?.sources.model !== "env";
  const modelsQuery = useQuery({
    queryKey: ["admin", "agent-models", modelQueryCreds, form.provider],
    queryFn: () =>
      adminApi.listAgentModels({
        api_base_url: modelQueryCreds.api_base_url,
        provider: form.provider || undefined,
        ...(modelQueryCreds.api_key ? { api_key: modelQueryCreds.api_key } : {}),
      }),
    enabled: canListModels,
    // The operator's own endpoint, but still a network call — don't re-poll it.
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
  const models = modelsQuery.data?.models ?? [];

  useEffect(() => {
    if (!data) return;
    const next = toFormState(data.effective);
    setForm(next);
    setBaseline(next);
    setApiKeyInput("");
    setClearApiKey(false);
  }, [data]);

  const saveMutation = useMutation({
    mutationFn: (patch: Record<string, unknown>) => adminApi.putAgentSettings(patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "agent-settings"] });
      qc.invalidateQueries({ queryKey: ["health"] });
    },
  });

  const testMutation = useMutation({
    mutationFn: async () => {
      const { patch, error } = buildPatch(form, baseline, apiKeyInput, clearApiKey);
      if (error) throw new Error(error);
      // Always PUT, even with an empty patch: the backend resets the probe
      // cache on any PUT, and "Test connection" must force a fresh probe
      // rather than risk returning a stale (up to agent_probe_ttl_seconds
      // old) cached result when the form is unchanged.
      await adminApi.putAgentSettings(patch);
      return healthApi.check();
    },
    onSuccess: (health) => {
      qc.invalidateQueries({ queryKey: ["admin", "agent-settings"] });
      qc.invalidateQueries({ queryKey: ["health"] });
      setTestResult(health.agent_available ? "reachable" : "unreachable");
    },
  });

  if (isLoading || !data) {
    return (
      <div className="flex justify-center py-12">
        <Spinner size={20} />
      </div>
    );
  }

  const sources = data.sources;
  const envVars = data.env_vars;
  const isEnvPinned = (field: string) => sources[field] === "env";

  const set = (field: keyof FormState) => (value: string) => {
    setForm((f) => ({ ...f, [field]: value }));
    setFormError(null);
    setTestResult(null);
  };

  const handleSave = () => {
    const { patch, error } = buildPatch(form, baseline, apiKeyInput, clearApiKey);
    if (error) {
      setFormError(error);
      return;
    }
    setFormError(null);
    saveMutation.mutate(patch, {
      onSuccess: () => {
        setTestResult(null);
      },
    });
  };

  const handleTest = () => {
    setFormError(null);
    setTestResult(null);
    testMutation.mutate();
  };

  const pinnedBadge = (field: string) =>
    isEnvPinned(field) && (
      <Badge variant="muted" className="ml-2">
        pinned by {envVars[field]}
      </Badge>
    );

  const errorMessage =
    formError ??
    (saveMutation.isError
      ? saveMutation.error instanceof ApiError
        ? saveMutation.error.message
        : "Failed to save agent settings."
      : testMutation.isError
        ? testMutation.error instanceof Error
          ? testMutation.error.message
          : "Failed to test connection."
        : null);

  return (
    <div className="max-w-xl space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-[var(--color-fg-primary)]">AI agent</h2>
        <p className="mt-0.5 text-xs text-[var(--color-fg-muted)]">
          Configure the optional AI investigation agent. Fields pinned by an environment
          variable cannot be edited here — unset the variable to allow a DB override.
        </p>
      </div>

      <div className="space-y-3">
        <Field label="Model" pinnedBadge={pinnedBadge("model")}>
          {/* A dropdown only once the endpoint has actually listed something.
              Free text is the fallback whenever it hasn't — no listing, an
              unreachable endpoint, or credentials not filled in yet — and
              stays reachable via "Enter manually" for models a listing omits. */}
          {isEnvPinned("model") || customModel || models.length === 0 ? (
            <Input
              value={form.model}
              disabled={isEnvPinned("model")}
              onChange={(e) => set("model")(e.target.value)}
              placeholder="gpt-4o-mini"
            />
          ) : (
            <Select value={form.model || undefined} onValueChange={(v) => set("model")(v)}>
              <SelectTrigger>
                <SelectValue placeholder="Select model" />
              </SelectTrigger>
              <SelectContent>
                {/* A saved model the endpoint no longer lists still has to be
                    selectable, or opening the dropdown would silently drop it. */}
                {(models.includes(form.model) || !form.model ? models : [form.model, ...models]).map(
                  (m) => (
                    <SelectItem key={m} value={m}>
                      {m}
                    </SelectItem>
                  ),
                )}
              </SelectContent>
            </Select>
          )}
          {!isEnvPinned("model") && (
            <p className="mt-1 flex items-center gap-1.5 text-[11px] text-[var(--color-fg-muted)]">
              {modelsQuery.isFetching ? (
                <>
                  <Spinner size={10} /> Loading models from the endpoint…
                </>
              ) : models.length > 0 ? (
                <>
                  {models.length} model{models.length === 1 ? "" : "s"} from the endpoint.{" "}
                  <button
                    type="button"
                    className="underline hover:text-[var(--color-fg-primary)]"
                    onClick={() => setCustomModel((c) => !c)}
                  >
                    {customModel ? "Choose from the list" : "Enter manually"}
                  </button>
                </>
              ) : canListModels ? (
                <>The endpoint listed no models — enter the name manually.</>
              ) : (
                <>Set the API base URL and key to load the endpoint&rsquo;s model list.</>
              )}
            </p>
          )}
        </Field>

        <Field label="Provider" pinnedBadge={pinnedBadge("provider")}>
          <Select
            value={form.provider || undefined}
            disabled={isEnvPinned("provider")}
            onValueChange={(v) => set("provider")(v)}
          >
            <SelectTrigger disabled={isEnvPinned("provider")}>
              <SelectValue placeholder="Select provider" />
            </SelectTrigger>
            <SelectContent>
              {PROVIDERS.map((p) => (
                <SelectItem key={p} value={p}>
                  {p}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>

        <Field label="API base URL" pinnedBadge={pinnedBadge("api_base_url")}>
          <Input
            value={form.api_base_url}
            disabled={isEnvPinned("api_base_url")}
            onChange={(e) => set("api_base_url")(e.target.value)}
            placeholder="https://api.openai.com/v1"
          />
        </Field>

        <Field label="API key" pinnedBadge={pinnedBadge("api_key")}>
          <div className="flex gap-2">
            <Input
              type="password"
              value={apiKeyInput}
              disabled={isEnvPinned("api_key") || clearApiKey || data.secret_mode === "env-only"}
              onChange={(e) => {
                setApiKeyInput(e.target.value);
                setClearApiKey(false);
                setFormError(null);
                setTestResult(null);
              }}
              placeholder={
                clearApiKey
                  ? "will be cleared"
                  : data.effective.api_key_set
                    ? "•••• (set)"
                    : "not set"
              }
            />
            {!isEnvPinned("api_key") && (data.effective.api_key_set || apiKeyInput) && (
              <Button
                type="button"
                variant="outline"
                size="md"
                disabled={clearApiKey}
                onClick={() => {
                  setClearApiKey(true);
                  setApiKeyInput("");
                  setFormError(null);
                  setTestResult(null);
                }}
              >
                Clear
              </Button>
            )}
          </div>
          <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
            {data.secret_mode === "env-only"
              ? "Database key storage is disabled (VESTIGO_AGENT_SECRET_MODE=env-only). Supply the key via VESTIGO_AGENT_API_KEY."
              : "Leave blank to keep the current key unchanged. Clearing removes any DB-stored key."}
          </p>
        </Field>

        <Field label="User agent" pinnedBadge={pinnedBadge("user_agent")}>
          <Input
            value={form.user_agent}
            disabled={isEnvPinned("user_agent")}
            onChange={(e) => set("user_agent")(e.target.value)}
          />
        </Field>

        <Field label="Extra headers (JSON)" pinnedBadge={pinnedBadge("extra_headers")}>
          <textarea
            value={form.extra_headers}
            disabled={isEnvPinned("extra_headers")}
            onChange={(e) => set("extra_headers")(e.target.value)}
            rows={4}
            spellCheck={false}
            placeholder={"{\n  \"X-Custom-Header\": \"value\"\n}"}
            className="w-full rounded border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] px-3 py-2 font-mono text-xs text-[var(--color-fg-primary)] placeholder:text-[var(--color-fg-muted)] transition-base focus:border-[var(--color-accent)] focus:outline-none disabled:opacity-40"
          />
        </Field>

        <Field label="Max turns" pinnedBadge={pinnedBadge("max_turns")}>
          <Input
            type="number"
            min={1}
            max={100}
            value={form.max_turns}
            disabled={isEnvPinned("max_turns")}
            onChange={(e) => set("max_turns")(e.target.value)}
          />
        </Field>

        <Field label="Reasoning effort" pinnedBadge={pinnedBadge("reasoning_effort")}>
          <Select
            value={form.reasoning_effort || undefined}
            disabled={isEnvPinned("reasoning_effort")}
            onValueChange={(v) => set("reasoning_effort")(unsetValue(v))}
          >
            <SelectTrigger disabled={isEnvPinned("reasoning_effort")}>
              <SelectValue placeholder="Select effort" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={UNSET}>(unset)</SelectItem>
              {EFFORTS.map((e) => (
                <SelectItem key={e} value={e}>
                  {e}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>

        <Field label="Context window (tokens)" pinnedBadge={pinnedBadge("context_window")}>
          <Input
            type="number"
            min={1024}
            value={form.context_window}
            disabled={isEnvPinned("context_window")}
            onChange={(e) => set("context_window")(e.target.value)}
            placeholder="e.g. 65536 — empty = reactive-only"
          />
          <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
            The model's context window. When set, older tool results are elided from what the
            model sees before a request can overflow it (the full transcript is always
            preserved). Empty = the window only engages after an overflow, costing one failed
            round trip.
          </p>
        </Field>

        <Field label="Tool result detail" pinnedBadge={pinnedBadge("tool_fidelity")}>
          <Select
            value={form.tool_fidelity || undefined}
            disabled={isEnvPinned("tool_fidelity")}
            onValueChange={(v) => set("tool_fidelity")(unsetValue(v))}
          >
            <SelectTrigger disabled={isEnvPinned("tool_fidelity")}>
              <SelectValue placeholder="Select detail level" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={UNSET}>(unset)</SelectItem>
              {FIDELITIES.map((f) => (
                <SelectItem key={f} value={f}>
                  {f}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
            How much of each event record the agent gets back from searches, similarity
            lookups and anomaly findings.
            <strong> full</strong> assumes a large window (the default) —{" "}
            <strong>message</strong> or <strong>auto</strong> suit a small local model.
            <strong> auto</strong> follows the context window above: 100k and up gets
            full, 32k and up message, anything smaller minimal — and message when no
            window is configured. On an overflow the agent retries one level down
            automatically, so this costs a slower turn rather than a failed one.
          </p>
        </Field>

        <Field label="Tools" pinnedBadge={pinnedBadge("disabled_tools")}>
          <div className="max-h-64 space-y-0.5 overflow-y-auto rounded border border-[var(--color-border)] p-1.5">
            {data.tools.map((tool) => {
              const enabled = !form.disabled_tools.includes(tool.name);
              return (
                <label
                  key={tool.name}
                  className={`flex items-start gap-2 rounded px-1.5 py-1 hover:bg-[var(--color-bg-elevated)] ${
                    isEnvPinned("disabled_tools") ? "cursor-not-allowed opacity-50" : "cursor-pointer"
                  }`}
                >
                  <Checkbox
                    checked={enabled}
                    disabled={isEnvPinned("disabled_tools")}
                    onCheckedChange={(v) => {
                      const next = new Set(form.disabled_tools);
                      if (v === true) next.delete(tool.name);
                      else next.add(tool.name);
                      setForm((f) => ({ ...f, disabled_tools: [...next].sort() }));
                      setFormError(null);
                      setTestResult(null);
                    }}
                    className="mt-0.5"
                  />
                  <span className="min-w-0 text-xs">
                    <span className="font-mono">{tool.name}</span>
                    <span className="block text-[11px] text-[var(--color-fg-muted)]">
                      {tool.description}
                    </span>
                  </span>
                </label>
              );
            })}
          </div>
          <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
            Unchecked tools are hard-disabled for everyone — the in-app agent AND the external
            /mcp endpoint. Users can only restrict further, never re-enable.
          </p>
        </Field>
      </div>

      {errorMessage && <p className="text-xs text-[var(--color-danger)]">{errorMessage}</p>}

      <div className="flex items-center gap-2">
        <Button
          variant="accent"
          size="md"
          disabled={saveMutation.isPending}
          onClick={handleSave}
        >
          {saveMutation.isPending ? "Saving…" : "Save"}
        </Button>
        <Button variant="outline" size="md" disabled={testMutation.isPending} onClick={handleTest}>
          {testMutation.isPending ? "Testing…" : "Test connection"}
        </Button>
        {saveMutation.isSuccess && !saveMutation.isPending && (
          <span className="text-xs text-[var(--color-fg-muted)]">Saved.</span>
        )}
        {testResult === "reachable" && (
          <span className="flex items-center gap-1 text-xs text-[var(--color-success)]">
            <CheckCircle2 size={14} /> Reachable
          </span>
        )}
        {testResult === "unreachable" && (
          <span className="flex items-center gap-1 text-xs text-[var(--color-danger)]">
            <XCircle size={14} /> Unreachable
          </span>
        )}
      </div>
    </div>
  );
}

function Field({
  label,
  pinnedBadge,
  children,
}: {
  label: string;
  pinnedBadge?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="mb-1 flex items-center text-xs text-[var(--color-fg-muted)]">
        {label}
        {pinnedBadge}
      </label>
      {children}
    </div>
  );
}
