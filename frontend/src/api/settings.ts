import { get, put } from "./client";
import type { AdminAgentTool } from "./admin";

/** How the admin console renders one setting's editor. Derived server-side from
 * the pydantic annotation, so it never drifts from what validation accepts. */
export type SettingKind =
  "bool" | "int" | "float" | "str" | "secret" | "choice" | "json";

/** One configurable setting: metadata, effective value, and where it came from.
 * Mirrors `_settings_payload()` in `src/vestigo/api/routers/admin.py`. */
export interface InstanceSetting {
  field: string;
  group: string;
  label: string;
  help: string;
  kind: SettingKind;
  /** Whether the field accepts `null`. Decides what an emptied text box means:
   * "unset" for a nullable field, a literal empty value otherwise (an empty
   * `sigma_rules_path` disables the global ruleset). */
  nullable: boolean;
  constraints: {
    ge?: number;
    gt?: number;
    le?: number;
    lt?: number;
    pattern?: string;
  };
  choices: string[] | null;
  default: unknown;
  /** "env" = pinned by the deployment (read-only), "db" = an admin override is
   * applied, "default" = the built-in value. */
  source: "env" | "db" | "default";
  env_var: string;
  env_only: boolean;
  /** The value is stored but the running process keeps the old one until restart. */
  restart_required: boolean;
  /** Optional subsystem this field configures; unconfigured ones are hidden
   * from the analyst UI (see `useCapabilities`). */
  subsystem: string | null;
  editable: boolean;
  /** Null for secrets — the backend never returns their plaintext. */
  value: unknown;
  /** Secrets only: whether a value is currently set. */
  value_set?: boolean;
}

export interface SettingGroup {
  key: string;
  label: string;
  description: string;
}

/** The two things the agent group needs that no single field carries: the tool
 * catalogue `agent_disabled_tools` toggles against, and advisory warnings about
 * the *combination* of resolved values (full fidelity, underpowered window). */
export interface InstanceAgentMeta {
  tools: AdminAgentTool[];
  warnings: string[];
}

export interface InstanceSettingsResponse {
  groups: SettingGroup[];
  settings: InstanceSetting[];
  /** "env-only" means the backend refuses to store any secret in the database. */
  secrets_mode: "db" | "env-only";
  agent: InstanceAgentMeta;
  /** Present on a PUT response: the fields whose overrides are now applied. */
  applied?: string[];
}

export const settingsApi = {
  get: () => get<InstanceSettingsResponse>("/admin/settings"),

  /** Send only what changed. `null` clears a field's override, falling back to
   * the environment and then the built-in default. */
  update: (values: Record<string, unknown>) =>
    put<InstanceSettingsResponse>("/admin/settings", { values }),
};
