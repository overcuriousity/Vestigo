/**
 * Scenario presets — an investigation named in the analyst's language, mapped
 * onto one legal `ChartConfig`.
 *
 * The Visualize page's standing rule is that the core knows nothing about what
 * a field *is* (`docs/VISUALIZE.md` §1), and a scenario does not break it: a
 * scenario never names `src_ip`. It names **roles** — "the field holding the
 * client address" — and the analyst binds each role to one of their own
 * timeline's fields in the scenario modal. `suggest` is a hint over field
 * *tokens*, offered pre-filled and always overridable; a role it cannot fill
 * is reported, never guessed at. The domain knowledge lives in this table's
 * prose and in the suggested filter, both of which the analyst sees and can
 * drop before anything renders.
 *
 * Applying a scenario is the rail's own mechanism and nothing more:
 * `updateConfig(buildScenarioConfig(...))` plus, if the analyst leaves the
 * filter row checked, `updateFilters(merge(scenarioFilters(...)))`. The filter
 * therefore lands in the URL, shows as chips in `InheritedFiltersBar` and
 * reaches the caption by the existing path — disclosed, exportable, shareable.
 *
 * A scenario is never hidden. One whose roles find no candidate field still
 * opens and says which role it could not fill (same rule as the analysis
 * gate: advice plus a record, never a lock).
 */
import type { EventFilters, VizFieldInfo } from "@/api/types";
import type { ChartConfig, ChartOptions, ChartType, Scale } from "./chartConfig";
import type { Metric } from "./transforms";

/** Role key → the field token the analyst bound to it. */
export type RoleBinding = Record<string, string | undefined>;

export interface ScenarioRole {
  key: string;
  /** The role's name, in the analyst's language. */
  label: string;
  /** What to bind here, and why this scenario wants it. */
  hint: string;
  required: boolean;
  /** Which chart input this role fills; `filter` keys the suggested filter
   * only and never reaches the chart's field. */
  binds: "field" | "fieldY" | "filter";
  /** A hint over field tokens. A suggestion — the modal pre-fills it and the
   * analyst overrides it; nothing here decides anything on its own. */
  suggest?: RegExp;
}

export interface ScenarioFilterSpec {
  /** The checkbox label — what the analyst is agreeing to count. */
  label: string;
  /** What the filter does, in words, for the modal and the caption. */
  describe: string;
  build: (bindings: RoleBinding) => EventFilters;
}

export interface Scenario {
  id: string;
  label: string;
  /** The forensic question this scenario answers — the registry's vocabulary
   * (`CHART_META[c].question`), one level up. */
  question: string;
  chartType: ChartType;
  scale: Scale;
  metric: Metric;
  roles: ScenarioRole[];
  options: ChartOptions;
  /** A filter the scenario suggests; pre-checked in the modal, droppable. */
  filter?: ScenarioFilterSpec;
}

/** Substrings that read as "the address that connected". */
const ADDRESS = /(src|source|client|remote|peer|caller)[._-]?(ip|addr|address|host|name)|(^|[.:_])(ip|addr|address)([._-]|$)/i;
const TARGET = /(dst|dest|destination|target|server|computer|machine|workstation|host)[._-]?(ip|addr|address|host|name)?/i;
const ACCOUNT = /(user|username|account|subject|principal|logon|samaccountname|upn)/i;
const BYTES = /(bytes|octets|size|length|volume|transferred|sent|received|payload)/i;
const REQUEST = /(url|uri|path|query|querystring|request|referer|body|message|cmd|command|args)/i;
const EVENT_ID = /(event[._-]?id|eventcode|event[._-]?code|record[._-]?id)/i;

export const SCENARIOS: Scenario[] = [
  {
    id: "ddos-flood",
    label: "DDoS / flood",
    question:
      "Which client addresses were hammering the target, and were they active at the same moment or in shifts?",
    chartType: "heatmap",
    scale: "nominal",
    metric: "count",
    options: { topN: 20 },
    roles: [
      {
        key: "client",
        label: "Client address",
        hint: "The field holding who connected — one row per address, so a flood shows as a solid band.",
        required: true,
        binds: "field",
        suggest: ADDRESS,
      },
    ],
  },
  {
    id: "data-exfiltration",
    label: "Data exfiltration",
    question:
      "How did the volume leaving the environment accumulate — steadily, or all at once in one window?",
    chartType: "cumulative",
    scale: "ratio",
    metric: "count",
    options: { quantity: "sum" },
    roles: [
      {
        key: "bytes",
        label: "Transferred volume",
        hint: "The numeric field holding bytes (or another measure of size). The chart runs a total of it over time.",
        required: true,
        binds: "field",
        suggest: BYTES,
      },
    ],
  },
  {
    id: "sql-injection",
    label: "SQL injection",
    question:
      "Which requests carry SQL-injection syntax, and which payloads were tried most?",
    chartType: "bar",
    scale: "nominal",
    metric: "count",
    options: { topN: 20, sort: "count", orientation: "horizontal" },
    roles: [
      {
        key: "target",
        label: "Request or message field",
        hint: "The field holding the request text — a URL, a query string, a logged statement.",
        required: true,
        binds: "field",
        suggest: REQUEST,
      },
    ],
    filter: {
      label: "Only requests containing SQL-injection syntax",
      describe:
        "case-insensitive wildcard match for union/select, tautologies, comment terminators and information_schema",
      build: (b) => {
        const field = b.target;
        if (!field) return {};
        return {
          filters: {
            [field]: [
              "*union*select*",
              "*' or 1*=1*",
              "*\" or 1*=1*",
              "*information_schema*",
              "*--*",
              "*;--*",
              "*sleep(*",
              "*benchmark(*",
            ],
          },
          filterModes: { [field]: "wildcard" },
        };
      },
    },
  },
  {
    id: "rdp-interaction",
    label: "RDP interaction (event logs)",
    question:
      "Which accounts opened, reconnected to or failed remote-desktop sessions on these hosts?",
    chartType: "bar",
    scale: "nominal",
    metric: "count",
    options: { topN: 20, sort: "count", orientation: "horizontal" },
    roles: [
      {
        key: "account",
        label: "Account",
        hint: "The field holding who logged on — the bars count sessions per account.",
        required: true,
        binds: "field",
        suggest: ACCOUNT,
      },
      {
        key: "eventId",
        label: "Event ID field",
        hint: "The field holding the Windows event ID. Used only to narrow to the RDP events — it is not charted.",
        required: true,
        binds: "filter",
        suggest: EVENT_ID,
      },
    ],
    filter: {
      label: "Only remote-desktop event IDs",
      describe:
        "Security 4624/4625/4778/4779, RDP-Core 1149, LocalSessionManager 21/25 — logon, failure, reconnect and session events",
      build: (b) => {
        const field = b.eventId;
        if (!field) return {};
        return {
          filters: { [field]: ["4624", "4625", "4778", "4779", "1149", "21", "25"] },
        };
      },
    },
  },
  {
    id: "lateral-movement",
    label: "Lateral movement",
    question: "Which accounts touched which hosts — and which account spans more of them than it should?",
    chartType: "sankey",
    scale: "nominal",
    metric: "count",
    options: { limitX: 15, limitY: 15 },
    roles: [
      {
        key: "account",
        label: "Account",
        hint: "The field holding who acted — the left-hand side of the flow.",
        required: true,
        binds: "field",
        suggest: ACCOUNT,
      },
      {
        key: "target",
        label: "Target host",
        hint: "The field holding what was reached — the right-hand side of the flow.",
        required: true,
        binds: "fieldY",
        suggest: TARGET,
      },
    ],
  },
  {
    id: "off-hours-activity",
    label: "Off-hours activity",
    question:
      "Does activity keep office hours, or is something running nights and weekends (UTC)?",
    chartType: "punchcard",
    scale: "nominal",
    metric: "count",
    options: {},
    roles: [],
  },
];

/**
 * Pre-fill each role from the timeline's own field tokens.
 *
 * Fields arrive sorted by coverage descending, so the first token a role's
 * hint matches is also the best-covered one. A role whose hint matches nothing
 * is left out of the result — an unbound role is reported to the analyst, not
 * filled with whatever was closest.
 */
export function suggestBindings(scenario: Scenario, fields: VizFieldInfo[]): RoleBinding {
  const bindings: RoleBinding = {};
  const taken = new Set<string>();
  for (const role of scenario.roles) {
    if (!role.suggest) continue;
    const hit = fields.find((f) => !taken.has(f.token) && role.suggest!.test(f.token));
    if (hit) {
      bindings[role.key] = hit.token;
      taken.add(hit.token);
    }
  }
  return bindings;
}

/** The required roles still unbound — what the modal names, and what keeps
 * its confirm button disabled. */
export function missingRoles(scenario: Scenario, bindings: RoleBinding): ScenarioRole[] {
  return scenario.roles.filter((r) => r.required && !bindings[r.key]);
}

/**
 * The scenario as a chart-config patch.
 *
 * Every member is written, including the ones the scenario does not use:
 * applying a scenario over a chart the analyst had already built must not
 * leave that chart's derivation, mark list or figure-specific inputs behind to
 * be read by a figure that never declared them.
 */
export function buildScenarioConfig(
  scenario: Scenario,
  bindings: RoleBinding,
): Partial<ChartConfig> {
  const roleFor = (binds: ScenarioRole["binds"]) =>
    scenario.roles.find((r) => r.binds === binds);
  const fieldRole = roleFor("field");
  const fieldYRole = roleFor("fieldY");
  return {
    field: (fieldRole && bindings[fieldRole.key]) ?? null,
    fieldY: (fieldYRole && bindings[fieldYRole.key]) ?? null,
    fields: null,
    scale: scenario.scale,
    chartType: scenario.chartType,
    metric: scenario.metric,
    compare: { mode: "off" },
    options: { ...scenario.options },
    derive: null,
    inputs: {},
    marks: [],
  };
}

/** The filter this scenario suggests for these bindings, or null when it
 * suggests none (or cannot key one yet). */
export function scenarioFilters(
  scenario: Scenario,
  bindings: RoleBinding,
): EventFilters | null {
  if (!scenario.filter) return null;
  const filters = scenario.filter.build(bindings);
  return Object.keys(filters).length > 0 ? filters : null;
}
