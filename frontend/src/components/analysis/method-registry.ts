/**
 * method-registry — the single client-side description of every analysis
 * method: identity, evidence class, cost class, score unit, the prose
 * explaining what it does, and its tunable knobs.
 *
 * Supersedes detector-registry.ts as the rail's source of truth. The
 * methodology prose lives here rather than in a separate Method tab because
 * "what does this method actually do" is only ever asked while looking at one
 * of its findings — a tab away is the wrong distance.
 *
 * Evidence class is the rail's grouping, and encodes the one thing the old
 * panel never said: a Sigma hit and a rare value are not the same kind of
 * claim. One names a technique somebody asserted is malicious; the other says
 * a value is unusual here, which is not the same as saying it is bad.
 *
 * Two invariants are held by tests rather than by types, because both cross a
 * language boundary: the method ids must match METHOD_IDS in
 * db/analysis_plan.py, and every knob's `param` must appear in METHOD_PARAMS
 * in api/routers/analysis.py (which rejects unknown keys rather than dropping
 * them, so a drifted knob 422s loudly instead of silently rerunning defaults).
 */
import {
  Activity,
  FileText,
  Hash,
  Layers,
  ListOrdered,
  Percent,
  Replace,
  Rewind,
  Ruler,
  Shuffle,
  Timer,
  Type,
} from "lucide-react";

export type MethodId =
  | "value_novelty"
  | "value_combo"
  | "numeric_range"
  | "charset"
  | "entropy"
  | "frequency"
  | "proportion_shift"
  | "value_distribution_drift"
  | "interval_periodicity"
  | "timestamp_order"
  | "sequence_novelty"
  | "log_template";

export type EvidenceClass = "named" | "statistical" | "exploration";

/** Groups in rail order — strongest claim first. */
export const EVIDENCE_CLASSES: { id: EvidenceClass; label: string; note: string }[] = [
  { id: "named", label: "Named techniques", note: "a rule author named this" },
  { id: "statistical", label: "Statistical outliers", note: "odd, not necessarily bad" },
  { id: "exploration", label: "Exploration", note: "leads, not verdicts" },
];

/**
 * How a `kind: "fields"` knob configures `AnomalyFieldPicker`.
 *
 * These are not presentation preferences: each one mirrors what the backend
 * actually scans for that method, so the picker's checked set is a preview of
 * the run rather than a guess. Restored verbatim from the per-detector views
 * deleted in the rail-plus-overlay refactor.
 */
export interface FieldPickerConfig {
  /** Floor an explicit selection must keep — value_combo needs two to combine. */
  minSelected?: number;
  /** Ceiling an explicit selection may hold. */
  maxSelected?: number;
  /** How many recommended fields auto mode really scans, so the preview matches. */
  autoCount?: number;
  /** Name for the auto default in the picker's footer. */
  autoLabel?: string;
  /** Auto mode also scans identifier-kind fields (charset/entropy's target). */
  autoIncludesIdentifiers?: boolean;
  /** Offer numeric-parseable candidates instead of the cardinality inventory. */
  numeric?: boolean;
}

export interface MethodKnob {
  /** Params-object key — must exist in METHOD_PARAMS in api/routers/analysis.py. */
  param: string;
  label: string;
  /**
   * `fields` renders `AnomalyFieldPicker`, `field` renders `MethodFieldSelect`.
   * Neither is a text box: a field name is one of a fixed set of columns plus
   * this timeline's `attr:` keys, which no analyst can be asked to spell from
   * memory. `text` is for the knobs that really are free-form.
   */
  kind: "number" | "text" | "field" | "fields";
  placeholder: string;
  /** `kind: "fields"` only. */
  picker?: FieldPickerConfig;
  /** `kind: "field"` only — the standard (non-attribute) choices, in order. */
  fieldOptions?: { value: string; label: string }[];
  /** `kind: "field"` only — names the empty choice, i.e. the method's default. */
  noneLabel?: string;
}

export interface MethodMeta {
  id: MethodId;
  label: string;
  hint: string;
  /**
   * When to configure it, in one sentence for the wizard's card. Starts with
   * "Use this when" — a test enforces it — so the twelve cards read as one list.
   */
  useWhen: string;
  icon: React.ElementType;
  evidenceClass: EvidenceClass;
  costClass: "cheap" | "heavy";
  /** Unit for the raw score. Scores are NOT comparable across methods. */
  scoreUnit: string;
  /**
   * Minimum raw score for a finding of this method to enter the rail's ranked
   * feed. Presentation only — the method still returns everything, the sheet
   * still shows everything, and the rail discloses the held-back count with a
   * control that reveals them.
   *
   * Set only where the score is continuous and the method has no threshold
   * knob of its own to express "worth looking at" with. `frequency` has
   * `z_threshold`, the four two-window methods have their q-value cut, and
   * `charset`'s finding is binary — for those the floor belongs in the run,
   * not in the rail. What is left is the two band methods, where a value one
   * band width outside is arithmetically real and, in a feed sorted by
   * method rotation, sits above findings tens of band widths out.
   */
  railFloor?: number;
  /** Replaces MethodologyPanel: what this method does, in the analyst's terms. */
  what: string;
  /**
   * The *shape* of the statement this method runs — not a transcript of it.
   *
   * The detectors do not return their compiled SQL the way the Sigma runner
   * does, so there is no executed statement to show. Presenting one anyway,
   * under a heading like "the query it ran", would be a claim we cannot point
   * at code for. This is a teaching aid, labeled as such in the UI: it names
   * the tables, the grouping and the test, while fields, windows and
   * thresholds are bound from the parameters beside it.
   */
  querySketch: string;
  knobs: MethodKnob[];
}

/** The plain multi-field knob: recommended set, no floor, no ceiling. */
const FIELDS_KNOB: MethodKnob = {
  param: "fields",
  label: "Fields",
  kind: "fields",
  placeholder: "auto",
};
const fieldsKnob = (picker: FieldPickerConfig): MethodKnob => ({ ...FIELDS_KNOB, picker });

/**
 * Series fields: the standard columns an event can be partitioned by. Attribute
 * keys are appended per timeline by `MethodFieldSelect`.
 *
 * `source_file` is deliberately not among them, though it is a real event
 * column: partitioning by it yields one series per ingested file, which is a
 * property of how the case was loaded rather than of the logs, and gives the
 * cadence and n-gram methods series that cannot be compared to each other.
 */
const SERIES_FIELD_OPTIONS = [
  { value: "artifact", label: "Artifact type" },
  { value: "timestamp_desc", label: "Event category" },
  { value: "display_name", label: "Display name" },
  { value: "parser_name", label: "Parser" },
];
const SERIES_KNOB: MethodKnob = {
  param: "series_field",
  label: "Series field",
  kind: "field",
  placeholder: "artifact",
  fieldOptions: SERIES_FIELD_OPTIONS,
  noneLabel: "Artifact type (default)",
};
const FDR_KNOB: MethodKnob = {
  param: "fdr_q",
  label: "FDR q",
  kind: "number",
  placeholder: "0.05",
};
const RATIO_KNOB: MethodKnob = {
  param: "min_ratio",
  label: "Min ratio",
  kind: "number",
  placeholder: "2.0",
};

export const METHODS: MethodMeta[] = [
  {
    id: "value_novelty",
    label: "Rare values",
    hint: "Rare or first-seen field values",
    useWhen:
      "Use this when you want the rarest values in a field surfaced first — a user, host or process that almost never appears. Works with no baseline.",
    icon: Hash,
    evidenceClass: "statistical",
    costClass: "cheap",
    scoreUnit: "surprise",
    what: "Ranks field values by −log(frequency): the rarer a value is in the scanned corpus, the higher it scores. Works immediately after ingestion, with no baseline.",
    querySketch: `SELECT <field> AS value, count() AS n\nFROM events\nWHERE case_id = {case} AND source_id IN {sources}\nGROUP BY value\nORDER BY n ASC\nLIMIT {limit}\n-- score = -log(n / total)`,
    knobs: [fieldsKnob({ autoCount: 15 })],
  },
  {
    id: "value_combo",
    label: "Value combos",
    hint: "Rare combinations of fields",
    useWhen:
      "Use this when two fields are each ordinary on their own but their pairing might not be — an account on a host it never logs into.",
    icon: Layers,
    evidenceClass: "statistical",
    costClass: "cheap",
    scoreUnit: "surprise",
    what: "The multi-field extension of rare values: scores field pairs, catching the case where each value is common on its own but their co-occurrence is not.",
    querySketch: `SELECT <field_a>, <field_b>, count() AS n\nFROM events\nWHERE case_id = {case} AND source_id IN {sources}\nGROUP BY 1, 2\nORDER BY n ASC\nLIMIT {limit}\n-- score = -log(n / total)`,
    // Two to four fields, of which auto combines the top two.
    knobs: [fieldsKnob({ minSelected: 2, maxSelected: 4, autoCount: 2, autoLabel: "top 2" })],
  },
  {
    id: "numeric_range",
    label: "Numeric range",
    hint: "Values outside a learned band",
    useWhen:
      "Use this when a numeric field has a normal band and you want values far outside it — bytes transferred, durations, ports.",
    icon: Ruler,
    evidenceClass: "statistical",
    costClass: "heavy",
    scoreUnit: "× band",
    railFloor: 2,
    what: "Learns a band per numeric field from the reference data and reports values outside it, scored by how many band widths out they sit.",
    querySketch: `SELECT toFloat64OrNull(<field>) AS num, count() AS n\nFROM events\nWHERE case_id = {case} AND num IS NOT NULL\nGROUP BY num\n-- band from the reference quantiles; score = excess / band width`,
    knobs: [fieldsKnob({ autoCount: 15, numeric: true })],
  },
  {
    id: "charset",
    label: "Charset novelty",
    hint: "Never-seen characters",
    useWhen:
      "Use this when a field should only ever contain a fixed alphabet and a stray script or injected byte would matter — usernames, hostnames, paths.",
    icon: Type,
    evidenceClass: "statistical",
    costClass: "heavy",
    scoreUnit: "surprise",
    what: "Learns the alphabet each field uses, then flags values containing characters that alphabet has never contained — a script change or an injected byte, regardless of what the value means.",
    querySketch: `SELECT <field> AS value, count() AS n\nFROM events\nWHERE case_id = {case}\nGROUP BY value\n-- alphabet learned from the reference values;\n-- reported when a value contains a character outside it`,
    knobs: [
      fieldsKnob({ autoIncludesIdentifiers: true }),
      {
        param: "group_field",
        label: "Group by",
        kind: "field",
        placeholder: "(none)",
        // Learn one alphabet per value of this field (per host, say) instead of
        // one merged alphabet over the whole scope.
        fieldOptions: SERIES_FIELD_OPTIONS,
        noneLabel: "Whole scope",
      },
    ],
  },
  {
    id: "entropy",
    label: "Entropy outliers",
    hint: "Random or degenerate strings",
    useWhen:
      "Use this when random-looking strings would be a lead — encoded payloads, generated domains, packed command lines.",
    icon: Shuffle,
    evidenceClass: "statistical",
    costClass: "heavy",
    scoreUnit: "× band",
    railFloor: 2,
    what: "Measures Shannon entropy per value against a learned per-field band, catching both random-looking payloads and degenerate repeats at the other extreme.",
    querySketch: `SELECT <field> AS value, count() AS n\nFROM events\nWHERE case_id = {case}\nGROUP BY value\n-- Shannon entropy per value against the learned per-field band`,
    knobs: [fieldsKnob({ autoIncludesIdentifiers: true })],
  },
  {
    id: "frequency",
    label: "Frequency",
    hint: "Count spikes and silences",
    useWhen:
      "Use this when a change in volume over time is the question — a burst, a silence, a series that spiked in one window. Best with a baseline.",
    icon: Activity,
    evidenceClass: "statistical",
    costClass: "heavy",
    scoreUnit: "|z|",
    what: "Buckets events per series value and flags buckets whose count sits more than z standard deviations from that series' own mean — spikes and silences alike.",
    querySketch: `SELECT toStartOfInterval(<effective_ts>, INTERVAL {bucket} SECOND) AS b,\n       <series_field> AS series, count() AS n\nFROM events\nWHERE case_id = {case} AND source_id IN {sources}\nGROUP BY b, series\n-- z = (n - mean(n)) / stddevPop(n), per series`,
    knobs: [
      SERIES_KNOB,
      { param: "z_threshold", label: "|z| ≥", kind: "number", placeholder: "3.0" },
    ],
  },
  {
    id: "proportion_shift",
    label: "Proportion shift",
    hint: "Value shares that change between windows",
    useWhen:
      "Use this when you have a baseline and want the values whose share of events changed most in the suspect window. Needs a baseline.",
    icon: Percent,
    evidenceClass: "statistical",
    costClass: "heavy",
    scoreUnit: "G",
    what: "G-tests each value's share of events in the suspect window against its share in the baseline window, so a value can be flagged for changing its proportion even when total volume is flat.",
    querySketch: `SELECT <field> AS value,\n       countIf(<effective_ts> BETWEEN {b0} AND {b1}) AS base_n,\n       countIf(<effective_ts> BETWEEN {s0} AND {s1}) AS susp_n\nFROM events\nWHERE case_id = {case}\nGROUP BY value\n-- G-test per value, Benjamini-Hochberg across the run`,
    knobs: [FIELDS_KNOB, FDR_KNOB, RATIO_KNOB],
  },
  {
    id: "value_distribution_drift",
    label: "Distribution drift",
    hint: "Whole-field value-mix changes",
    useWhen:
      "Use this when whole fields, not single values, may have changed shape between the baseline and the suspect window. Needs a baseline.",
    icon: Replace,
    evidenceClass: "statistical",
    costClass: "heavy",
    scoreUnit: "−log₁₀ p",
    what: "Tests whether a field's entire value mix differs between baseline and suspect windows — the field-level counterpart to proportion shift's per-value test.",
    querySketch: `SELECT <field> AS value,\n       countIf(<effective_ts> BETWEEN {b0} AND {b1}) AS base_n,\n       countIf(<effective_ts> BETWEEN {s0} AND {s1}) AS susp_n\nFROM events\nWHERE case_id = {case}\nGROUP BY value\n-- KS (numeric) or 2xk G-test (categorical) over the whole mix`,
    knobs: [FIELDS_KNOB, FDR_KNOB],
  },
  {
    id: "interval_periodicity",
    label: "Interval cadence",
    hint: "Broken heartbeats and new beaconing",
    useWhen:
      "Use this when beaconing or a missed heartbeat is the question — a value that arrives on a new rhythm, or stopped arriving on its old one. Needs a baseline.",
    icon: Timer,
    evidenceClass: "statistical",
    costClass: "heavy",
    scoreUnit: "−log₁₀ p",
    what: "Fits an inter-arrival distribution per series value and tests whether the spacing is more regular than chance allows — new beaconing, or a heartbeat that stopped.",
    querySketch: `SELECT series, gap FROM (\n  SELECT <series_field> AS series,\n         dateDiff('second', lagInFrame(<effective_ts>) OVER w, <effective_ts>) AS gap\n  FROM events WHERE case_id = {case}\n  WINDOW w AS (PARTITION BY series ORDER BY <effective_ts>)\n)\n-- Poisson-rate G (cadence break) or Greenwood G (new regularity)`,
    knobs: [SERIES_KNOB, FDR_KNOB, RATIO_KNOB],
  },
  {
    id: "timestamp_order",
    label: "Timestamp order",
    hint: "Timestamps running backwards",
    useWhen:
      "Use this when you need to know whether the records themselves are sound — timestamps running backwards inside a source. No baseline, cheap.",
    icon: Rewind,
    evidenceClass: "statistical",
    costClass: "cheap",
    scoreUnit: "s skew",
    what: "Reports timestamps running backwards within a source: a clock-integrity check on the evidence itself rather than on the behavior it records.",
    querySketch: `SELECT source_id, event_id, timestamp,\n       lagInFrame(timestamp) OVER w AS prev\nFROM events\nWHERE case_id = {case}\nWINDOW w AS (PARTITION BY source_id ORDER BY line_number)\n-- reported when prev - timestamp >= {min_skew_seconds}`,
    knobs: [{ param: "min_skew_seconds", label: "Min skew", kind: "number", placeholder: "1.0" }],
  },
  {
    id: "sequence_novelty",
    label: "Event sequences",
    hint: "Never-seen event orderings",
    useWhen:
      "Use this when the order things happened in matters — a host sequence, a command chain — and you want orderings never seen in the baseline. Needs a baseline.",
    icon: ListOrdered,
    evidenceClass: "statistical",
    costClass: "heavy",
    scoreUnit: "surprise",
    what: "Builds n-grams of consecutive artifact types per series and flags orderings that never occur in the reference set — the order is the finding, not any single event in it.",
    querySketch: `SELECT ngram, count() AS n FROM (\n  SELECT arrayStringConcat([v1, v2, v3], ' -> ') AS ngram\n  FROM (SELECT <series_field> AS v1,\n               leadInFrame(...) AS v2, leadInFrame(...) AS v3\n        FROM events WHERE case_id = {case}\n        WINDOW w AS (PARTITION BY series ORDER BY <effective_ts>))\n)\nGROUP BY ngram\n-- reported when the n-gram has no occurrence in the baseline window`,
    knobs: [
      SERIES_KNOB,
      { param: "ngram_size", label: "n", kind: "number", placeholder: "3" },
      { param: "max_gap_seconds", label: "Max gap", kind: "number", placeholder: "300" },
    ],
  },
  {
    id: "log_template",
    label: "Log templates",
    hint: "New kinds of log line",
    useWhen:
      "Use this when the logs are unstructured and you want to see their shapes first — rare line structures surface without naming a field.",
    icon: FileText,
    evidenceClass: "exploration",
    costClass: "heavy",
    scoreUnit: "cluster",
    what: "Clusters raw messages into templates by masking their variable tokens. A template with no earlier occurrence is a kind of log line this system has not produced before — a lead to read, not a scored finding.",
    querySketch: `SELECT template_id, any(template) AS template, count() AS n\nFROM (\n  SELECT <field> AS message, <masked tokens> AS template_id\n  FROM events WHERE case_id = {case}\n)\nGROUP BY template_id\nORDER BY n DESC\nLIMIT {limit}`,
    knobs: [
      {
        param: "field",
        label: "Field",
        kind: "field",
        placeholder: "message",
        fieldOptions: [{ value: "message", label: "Message" }],
        noneLabel: "Message (default)",
      },
    ],
  },
];

export const METHODS_BY_ID = Object.fromEntries(METHODS.map((m) => [m.id, m])) as Record<
  MethodId,
  MethodMeta
>;
