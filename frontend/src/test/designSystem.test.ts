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
 * So: three checks, over two scopes. The token check is hard — it sits at zero,
 * must stay there, and scans every `.ts`/`.tsx` under `src/` outside `src/test/`,
 * because a dead token is not a JSX-only problem (`components/viz/lib/colors.ts`
 * builds `var(--viz-*)` strings for the chart export path). The other two are
 * budgeted per file (see `designSystemBudget.ts`) and apply to components and
 * pages only, because their fixes do not exist yet: arbitrary font sizes need a
 * type scale and raw buttons need an `IconButton`, both `docs/ROADMAP.md`
 * Milestone 3 items. Budgets may only fall, and the test fails when one is
 * beatable — otherwise the numbers rot and the list stops shrinking.
 */
import { describe, it, expect } from "vitest";
// Vitest stubs CSS imports to "" unless the file is in `test.css.include` —
// vite.config.ts opts index.css in precisely so this stays readable.
import STYLESHEET from "@/index.css?raw";
import { BUDGET, type FileBudget } from "./designSystemBudget";

// Vite's raw glob rather than node:fs — the frontend tsconfig carries no node
// types, and this keeps the scan inside the bundler's module graph. Same
// constraint and same solution as `vizExplainers.test.ts`.

/**
 * Components and pages: the files the two budgeted checks apply to, since
 * `text-[Npx]` and `<button>` only occur in JSX. `designSystemBudget.ts` is keyed
 * by these paths.
 */
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
 * Everything for the token check, which is not a JSX concern: `lib/guidance.tsx`
 * holds JSX copy with token classes, and `components/viz/lib/colors.ts` returns
 * `var(--viz-*)` strings that feed the SVG export path, where a dead token
 * exports a blank fill rather than an obviously wrong colour. Scoping this to
 * `components/**\/*.tsx` was the first version's blind spot — a bad token in
 * either file passed silently.
 *
 * `src/test/` is excluded because this file and its siblings quote token names
 * in prose and fixtures (`var(--test-color)` in `vizColors.test.ts`).
 */
const TOKEN_SOURCES = {
  ...(import.meta.glob(["../**/*.ts", "../**/*.tsx", "!../test/**"], {
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

/**
 * Block comments are stripped before the token scan: doc comments name tokens as
 * prose and placeholders (`export.ts` explains that "every `var(--x)` is
 * resolved"), and a comment is not a style declaration. Line comments are left
 * alone deliberately — `//` also opens a URL, and eating to end of line could
 * swallow a real reference beside it.
 */
const BLOCK_COMMENT = /\/\*[\s\S]*?\*\//g;

/**
 * Definitions are matched at line start, which is how `index.css` is authored. A
 * token declared inline (`style={{ "--x": … }}`) would therefore read as
 * undefined — a false *failure*, never a false pass, so the check still only errs
 * toward being noticed.
 *
 * Both patterns accept the full custom-property character set rather than
 * lowercase-only: a `--colorError` would otherwise be invisible to the
 * definition and the reference scan alike, and so silently exempt.
 */
const DEFINITION = /^\s*(--[\w-]+)\s*:/gm;
const REFERENCE = /var\(\s*(--[\w-]+)/g;
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
    expect(Object.keys(TOKEN_SOURCES).length).toBeGreaterThan(Object.keys(SOURCES).length);
    // The token scan must reach past components/ and pages/, which is where the
    // first version of this file stopped.
    expect(TOKEN_SOURCES["../lib/guidance.tsx"]).toBeTruthy();
    expect(TOKEN_SOURCES["../components/viz/lib/colors.ts"]).toBeTruthy();
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
    for (const [path, source] of [
      ...Object.entries(TOKEN_SOURCES),
      ["../index.css", STYLESHEET],
    ]) {
      const code = source.replace(BLOCK_COMMENT, "");
      for (const match of code.matchAll(REFERENCE)) {
        const name = match[1];
        // `var(--viz-series-${slot})` — the name is computed, so there is no
        // literal token to look up. `colors.ts` is the only such site; the
        // family it indexes is asserted by `vizColors.test.ts` instead.
        if (code.startsWith("${", match.index + match[0].length)) continue;
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
