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

export interface MethodKnob {
  /** Params-object key — must exist in METHOD_PARAMS in api/routers/analysis.py. */
  param: string;
  label: string;
  kind: "number" | "text" | "fields";
  placeholder: string;
}

export interface MethodMeta {
  id: MethodId;
  label: string;
  hint: string;
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

const FIELDS_KNOB: MethodKnob = {
  param: "fields",
  label: "Fields",
  kind: "fields",
  placeholder: "auto",
};
const SERIES_KNOB: MethodKnob = {
  param: "series_field",
  label: "Series field",
  kind: "text",
  placeholder: "artifact",
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
    icon: Hash,
    evidenceClass: "statistical",
    costClass: "cheap",
    scoreUnit: "surprise",
    what: "Ranks field values by −log(frequency): the rarer a value is in the scanned corpus, the higher it scores. Works immediately after ingestion, with no baseline.",
    querySketch: `SELECT <field> AS value, count() AS n\nFROM events\nWHERE case_id = {case} AND source_id IN {sources}\nGROUP BY value\nORDER BY n ASC\nLIMIT {limit}\n-- score = -log(n / total)`,
    knobs: [FIELDS_KNOB],
  },
  {
    id: "value_combo",
    label: "Value combos",
    hint: "Rare combinations of fields",
    icon: Layers,
    evidenceClass: "statistical",
    costClass: "cheap",
    scoreUnit: "surprise",
    what: "The multi-field extension of rare values: scores field pairs, catching the case where each value is common on its own but their co-occurrence is not.",
    querySketch: `SELECT <field_a>, <field_b>, count() AS n\nFROM events\nWHERE case_id = {case} AND source_id IN {sources}\nGROUP BY 1, 2\nORDER BY n ASC\nLIMIT {limit}\n-- score = -log(n / total)`,
    knobs: [FIELDS_KNOB],
  },
  {
    id: "numeric_range",
    label: "Numeric range",
    hint: "Values outside a learned band",
    icon: Ruler,
    evidenceClass: "statistical",
    costClass: "heavy",
    scoreUnit: "× band",
    railFloor: 2,
    what: "Learns a band per numeric field from the reference data and reports values outside it, scored by how many band widths out they sit.",
    querySketch: `SELECT toFloat64OrNull(<field>) AS num, count() AS n\nFROM events\nWHERE case_id = {case} AND num IS NOT NULL\nGROUP BY num\n-- band from the reference quantiles; score = excess / band width`,
    knobs: [FIELDS_KNOB],
  },
  {
    id: "charset",
    label: "Charset novelty",
    hint: "Never-seen characters",
    icon: Type,
    evidenceClass: "statistical",
    costClass: "heavy",
    scoreUnit: "surprise",
    what: "Learns the alphabet each field uses, then flags values containing characters that alphabet has never contained — a script change or an injected byte, regardless of what the value means.",
    querySketch: `SELECT <field> AS value, count() AS n\nFROM events\nWHERE case_id = {case}\nGROUP BY value\n-- alphabet learned from the reference values;\n-- reported when a value contains a character outside it`,
    knobs: [
      FIELDS_KNOB,
      { param: "group_field", label: "Group by", kind: "text", placeholder: "(none)" },
    ],
  },
  {
    id: "entropy",
    label: "Entropy outliers",
    hint: "Random or degenerate strings",
    icon: Shuffle,
    evidenceClass: "statistical",
    costClass: "heavy",
    scoreUnit: "× band",
    railFloor: 2,
    what: "Measures Shannon entropy per value against a learned per-field band, catching both random-looking payloads and degenerate repeats at the other extreme.",
    querySketch: `SELECT <field> AS value, count() AS n\nFROM events\nWHERE case_id = {case}\nGROUP BY value\n-- Shannon entropy per value against the learned per-field band`,
    knobs: [FIELDS_KNOB],
  },
  {
    id: "frequency",
    label: "Frequency",
    hint: "Count spikes and silences",
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
    icon: FileText,
    evidenceClass: "exploration",
    costClass: "heavy",
    scoreUnit: "cluster",
    what: "Clusters raw messages into templates by masking their variable tokens. A template with no earlier occurrence is a kind of log line this system has not produced before — a lead to read, not a scored finding.",
    querySketch: `SELECT template_id, any(template) AS template, count() AS n\nFROM (\n  SELECT <field> AS message, <masked tokens> AS template_id\n  FROM events WHERE case_id = {case}\n)\nGROUP BY template_id\nORDER BY n DESC\nLIMIT {limit}`,
    knobs: [{ param: "field", label: "Field", kind: "text", placeholder: "message" }],
  },
];

export const METHODS_BY_ID = Object.fromEntries(METHODS.map((m) => [m.id, m])) as Record<
  MethodId,
  MethodMeta
>;
