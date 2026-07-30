/**
 * The design-system ratchet.
 *
 * The audit of 2026-07-30 found three CSS custom properties referenced but never
 * defined — `--color-error` in twelve files, `--color-bg-subtle` in two,
 * `--color-border-focus` in one. Every one of them compiled, typechecked, linted
 * and passed the whole suite, because Tailwind emits `text-[var(--color-error)]`
 * without complaint and CSS resolves an undefined custom property to `inherit`.
 * The detector views' red "wrong direction" arrows had been rendering in plain
 * body text for as long as anyone had been looking at them.
 *
 * So: three checks. The first is hard — it sits at zero and must stay there. The
 * other two are budgeted per file (see `designSystemBudget.ts`), because their
 * fixes do not exist yet: arbitrary font sizes need a type scale and raw buttons
 * need an `IconButton`, both `docs/ROADMAP.md` Milestone 3 items. Budgets may only
 * fall, and the test fails when one is beatable — otherwise the numbers rot and
 * the list stops shrinking.
 */
import { describe, it, expect } from "vitest";
// Vitest stubs CSS imports to "" unless the file is in `test.css.include` —
// vite.config.ts opts index.css in precisely so this stays readable.
import STYLESHEET from "@/index.css?raw";
import { BUDGET, type FileBudget } from "./designSystemBudget";

// Vite's raw glob rather than node:fs — the frontend tsconfig carries no node
// types, and this keeps the scan inside the bundler's module graph. Same
// constraint and same solution as `vizExplainers.test.ts`. Globbing components/
// and pages/ specifically rather than `../**` keeps this file and its siblings
// in src/test/ out of the scan without needing a filter.
const SOURCES = {
  ...(import.meta.glob("../components/**/*.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>),
  ...(import.meta.glob("../pages/**/*.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>),
};

/**
 * Custom properties supplied by something other than our stylesheet, so their
 * absence from `index.css` is expected rather than a bug: Radix sets its own on
 * the elements it positions (`--radix-select-content-available-height`), and
 * Tailwind generates `--tw-*` internally.
 */
const EXTERNAL_PREFIXES = ["--radix-", "--tw-"];

const DEFINITION = /^\s*(--[a-z0-9-]+)\s*:/gm;
const REFERENCE = /var\(\s*(--[a-z0-9-]+)/g;
const ARBITRARY_FONT_SIZE = /text-\[\s*\d+(?:\.\d+)?px\s*\]/g;
const RAW_BUTTON = /<button[\s>]/g;

/** `components/ui/` is the sanctioned home for raw elements — that is where the primitives live. */
const UI_PRIMITIVES = "../components/ui/";

function countMatches(source: string, pattern: RegExp): number {
  return (source.match(pattern) || []).length;
}

function actualBudget(path: string, source: string): FileBudget {
  const found: FileBudget = {};
  const fontSize = countMatches(source, ARBITRARY_FONT_SIZE);
  const rawButton = path.startsWith(UI_PRIMITIVES) ? 0 : countMatches(source, RAW_BUTTON);
  if (fontSize > 0) found.fontSize = fontSize;
  if (rawButton > 0) found.rawButton = rawButton;
  return found;
}

const CHECKS = [
  {
    key: "fontSize" as const,
    label: "arbitrary font size",
    fix: "use a font-size token from index.css `@theme`, not text-[Npx] — an arbitrary size also ignores compact density",
  },
  {
    key: "rawButton" as const,
    label: "raw <button>",
    fix: "use components/ui/Button, which carries the focus ring, disabled treatment and variants",
  },
];

describe("design system", () => {
  it("scans a plausible number of sources", () => {
    // A glob that silently matches nothing would make every assertion below pass.
    expect(Object.keys(SOURCES).length).toBeGreaterThan(100);
    expect(STYLESHEET).toContain("--color-fg-primary");
  });

  // The check that would have caught --color-error, --color-bg-subtle and
  // --color-border-focus. No budget: it is at zero, and a new one is always a bug.
  it("references no undefined CSS custom property", () => {
    const defined = new Set(
      [...STYLESHEET.matchAll(DEFINITION)].map((m) => m[1]),
    );

    const offences: string[] = [];
    // The stylesheet references its own tokens (--viz-grid aliases
    // --color-border-subtle), so it is scanned alongside the components.
    for (const [path, source] of [...Object.entries(SOURCES), ["../index.css", STYLESHEET]]) {
      for (const match of source.matchAll(REFERENCE)) {
        const name = match[1];
        if (defined.has(name)) continue;
        if (EXTERNAL_PREFIXES.some((p) => name.startsWith(p))) continue;
        offences.push(`${path}: var(${name}) is not defined in index.css`);
      }
    }

    expect(
      offences,
      `undefined custom properties resolve to inherit and fail silently:\n${offences.join("\n")}`,
    ).toEqual([]);
  });

  it("adds no violation beyond each file's budget", () => {
    const offences: string[] = [];
    for (const [path, source] of Object.entries(SOURCES)) {
      const found = actualBudget(path, source);
      for (const { key, label, fix } of CHECKS) {
        const allowed = BUDGET[path]?.[key] ?? 0;
        const count = found[key] ?? 0;
        if (count > allowed) {
          offences.push(`${path}: ${count} ${label}(s), budget ${allowed} — ${fix}`);
        }
      }
    }
    expect(offences, `\n${offences.join("\n")}`).toEqual([]);
  });

  // The teeth. Without this the budget is a floor nobody ever lowers, and the
  // list stops shrinking the moment someone cleans a file without editing it.
  it("has no budget entry that is now beatable", () => {
    const stale: string[] = [];
    for (const [path, budget] of Object.entries(BUDGET)) {
      const source = SOURCES[path];
      if (source === undefined) {
        stale.push(`${path}: budgeted but no such file — delete the entry`);
        continue;
      }
      const found = actualBudget(path, source);
      for (const { key, label } of CHECKS) {
        const allowed = budget[key] ?? 0;
        const count = found[key] ?? 0;
        if (count < allowed) {
          stale.push(
            `${path}: ${label} budget is ${allowed} but only ${count} remain — ` +
              (count === 0 ? `remove "${key}" from the entry` : `lower it to ${count}`),
          );
        }
      }
    }
    expect(stale, `\ndesignSystemBudget.ts is out of date:\n${stale.join("\n")}`).toEqual([]);
  });
});
