/**
 * Merge-suggestion heuristics for the timeline wizard's field-aggregation
 * step (issue #10). Combines name-token similarity with value-shape
 * classification of the sampled values — deliberately deterministic and
 * explainable (forensic requirement): every suggestion carries the reason it
 * was made. No embeddings involved; embeddings are usually absent at
 * timeline-creation time.
 */

export type ValueShape =
  | "ip"
  | "number"
  | "timestamp"
  | "email"
  | "uuid"
  | "hash"
  | "url"
  | "text"
  | "unknown";

const SHAPE_PATTERNS: [ValueShape, RegExp][] = [
  ["ip", /^(?:\d{1,3}\.){3}\d{1,3}$|^[0-9a-f:]+:[0-9a-f:]+$/i],
  ["uuid", /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i],
  ["hash", /^[0-9a-f]{32}$|^[0-9a-f]{40}$|^[0-9a-f]{64}$/i],
  ["email", /^[^\s@]+@[^\s@]+\.[^\s@]+$/],
  ["url", /^https?:\/\//i],
  ["timestamp", /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|^\d{10,16}$/],
  ["number", /^-?\d+(\.\d+)?$/],
];

/** Classify one value; "unknown" for empty input. */
export function classifyValue(value: string): ValueShape {
  const v = value.trim();
  if (!v) return "unknown";
  for (const [shape, re] of SHAPE_PATTERNS) {
    if (re.test(v)) return shape;
  }
  return "text";
}

/** Dominant shape across samples ("unknown" when empty or tied to nothing). */
export function classifySamples(samples: string[]): ValueShape {
  const counts = new Map<ValueShape, number>();
  for (const s of samples) {
    const shape = classifyValue(s);
    if (shape === "unknown") continue;
    counts.set(shape, (counts.get(shape) ?? 0) + 1);
  }
  let best: ValueShape = "unknown";
  let bestCount = 0;
  for (const [shape, count] of counts) {
    if (count > bestCount) {
      best = shape;
      bestCount = count;
    }
  }
  return best;
}

// Token normalization: common abbreviations expand to one canonical stem so
// `src_ip` and `source_ip` share tokens.
const TOKEN_SYNONYMS: Record<string, string> = {
  addr: "address",
  adr: "address",
  src: "source",
  dst: "destination",
  dest: "destination",
  usr: "user",
  uname: "user",
  username: "user",
  login: "user",
  msg: "message",
  num: "number",
  no: "number",
  ts: "time",
  tstamp: "time",
  timestamp: "time",
  datetime: "time",
  date: "time",
  proto: "protocol",
  pw: "password",
  passwd: "password",
  hostname: "host",
};

// Tokens that indicate opposite directionality — never merge across these.
const DIRECTIONAL = new Set(["source", "destination", "in", "out", "inbound", "outbound"]);

// Tokens too generic to justify a merge on their own.
const GENERIC = new Set(["id", "name", "value", "data", "field", "info", "type"]);

/** Split a field name into normalized tokens (snake/kebab/camelCase aware). */
export function nameTokens(name: string): string[] {
  return name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean)
    .map((t) => TOKEN_SYNONYMS[t] ?? t);
}

export interface SuggestInput {
  key: string;
  samples: string[];
  /** Source ids the field occurs in — shown in the UI, not used for scoring. */
  sourceIds: string[];
}

export interface SuggestedGroup {
  /** Proposed canonical name, e.g. "ip_address". */
  name: string;
  fields: string[];
  reason: string;
}

function directionOf(tokens: string[]): string | null {
  for (const t of tokens) if (DIRECTIONAL.has(t)) return t;
  return null;
}

function sharedMeaningfulTokens(a: string[], b: string[]): string[] {
  const setB = new Set(b);
  return a.filter((t) => setB.has(t) && !GENERIC.has(t) && !DIRECTIONAL.has(t));
}

/**
 * Suggest canonical merge groups. Two fields are grouped when they share at
 * least one meaningful (non-generic, non-directional) name token, their
 * sampled value shapes agree (or one is unknown), and they don't carry
 * *conflicting* directional tokens (`src_ip` never merges with `dst_ip`).
 */
export function suggestGroups(fields: SuggestInput[]): SuggestedGroup[] {
  const meta = fields.map((f) => ({
    key: f.key,
    tokens: nameTokens(f.key),
    shape: classifySamples(f.samples),
  }));

  // Union-find over pairwise-compatible fields.
  const parent = new Map<string, string>(meta.map((m) => [m.key, m.key]));
  const find = (k: string): string => {
    const p = parent.get(k)!;
    if (p === k) return k;
    const root = find(p);
    parent.set(k, root);
    return root;
  };
  const union = (a: string, b: string) => parent.set(find(a), find(b));

  const pairShared = new Map<string, string[]>();
  for (let i = 0; i < meta.length; i++) {
    for (let j = i + 1; j < meta.length; j++) {
      const a = meta[i];
      const b = meta[j];
      const dirA = directionOf(a.tokens);
      const dirB = directionOf(b.tokens);
      if (dirA && dirB && dirA !== dirB) continue;
      if (a.shape !== "unknown" && b.shape !== "unknown" && a.shape !== b.shape) continue;
      const shared = sharedMeaningfulTokens(a.tokens, b.tokens);
      if (shared.length === 0) continue;
      union(a.key, b.key);
      pairShared.set(find(a.key), shared);
    }
  }

  const groups = new Map<string, string[]>();
  for (const m of meta) {
    const root = find(m.key);
    groups.set(root, [...(groups.get(root) ?? []), m.key]);
  }

  const suggestions: SuggestedGroup[] = [];
  for (const [root, members] of groups) {
    if (members.length < 2) continue;
    const shared = pairShared.get(root) ?? [];
    const shapes = new Set(
      members
        .map((k) => meta.find((m) => m.key === k)!.shape)
        .filter((s) => s !== "unknown"),
    );
    const dir = directionOf(nameTokens(members[0]));
    const stem = shared[0] ?? nameTokens(members[0])[0] ?? members[0];
    const shape = [...shapes][0];
    // e.g. shared "ip" + shape "ip" -> ip_address; shared "user" -> user_name.
    const name =
      (dir ? `${dir}_` : "") +
      (shape === "ip" && stem === "ip"
        ? "ip_address"
        : stem === "user"
          ? "user_name"
          : stem);
    suggestions.push({
      name,
      fields: members.sort(),
      reason:
        `shared name token "${shared.join('", "') || stem}"` +
        (shape ? `, matching value shape "${shape}"` : ""),
    });
  }
  return suggestions.sort((a, b) => a.name.localeCompare(b.name));
}
