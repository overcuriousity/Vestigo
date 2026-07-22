import { format as formatNum } from "d3-format";
import { ExplainerPopover } from "@/components/viz/primitives/ExplainerPopover";
import type { ScatterStats } from "@/api/types";

const fmtCoef = formatNum(",.3~f");
const fmtInt = formatNum(",d");

function fmtP(p: number | null): string {
  if (p == null) return "—";
  if (p === 0) return "< 1e-300";
  if (p < 0.001) return p.toExponential(1);
  return p.toFixed(3);
}

/**
 * Teaching stat panel under the scatter chart — the server-computed
 * correlation/regression block, one row per statistic, each with its
 * explainer. The recommendation line spells out the Shapiro–Wilk verdict in
 * words so a novice knows *which* coefficient to quote and why.
 */
export function ScatterStatsPanel({ stats }: { stats: ScatterStats }) {
  const rows: {
    label: string;
    explainer: Parameters<typeof ExplainerPopover>[0]["id"];
    value: string;
    p: string | null;
    note?: string;
    highlight?: boolean;
  }[] = [
    {
      label: "Pearson r",
      explainer: "pearson",
      value: stats.pearson.r != null ? fmtCoef(stats.pearson.r) : "—",
      p: fmtP(stats.pearson.p),
      note: `all ${fmtInt(stats.n)} pairs`,
      highlight: stats.recommendation === "pearson",
    },
    {
      label: "Spearman ρ",
      explainer: "spearman",
      value: stats.spearman.rho != null ? fmtCoef(stats.spearman.rho) : "—",
      p: fmtP(stats.spearman.p),
      note: `all ${fmtInt(stats.n)} pairs`,
      highlight: stats.recommendation === "spearman",
    },
  ];
  if (stats.kendall) {
    rows.push({
      label: "Kendall τ",
      explainer: "kendall",
      value: stats.kendall.tau != null ? fmtCoef(stats.kendall.tau) : "—",
      p: fmtP(stats.kendall.p),
      note: `${fmtInt(stats.kendall.n)}-point sample`,
    });
  }

  const swX = stats.shapiro.x;
  const swY = stats.shapiro.y;
  const swReject =
    (swX?.p != null && swX.p < 0.05) || (swY?.p != null && swY.p < 0.05);

  return (
    <div className="mt-1 flex flex-col gap-1.5 text-xs text-[var(--color-fg-secondary)]">
      <table className="w-fit border-separate border-spacing-x-3 border-spacing-y-0.5">
        <thead>
          <tr className="text-left text-[var(--color-fg-muted)]">
            <th className="font-medium">Statistic</th>
            <th className="font-medium">Value</th>
            <th className="font-medium">
              p-value <ExplainerPopover id="pValue" />
            </th>
            <th className="font-medium">Basis</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.label}
              className={r.highlight ? "font-medium text-[var(--color-fg-primary)]" : undefined}
            >
              <td>
                {r.label} <ExplainerPopover id={r.explainer} />
                {r.highlight && (
                  <span className="ml-1 rounded bg-[var(--color-bg-hover)] px-1 py-0.5 text-[10px] uppercase tracking-wide text-[var(--color-accent)]">
                    recommended
                  </span>
                )}
              </td>
              <td>{r.value}</td>
              <td>{r.p}</td>
              <td className="text-[var(--color-fg-muted)]">{r.note}</td>
            </tr>
          ))}
          {stats.regression && stats.regression.slope != null && (
            <tr>
              <td>
                Regression <ExplainerPopover id="regression" />
              </td>
              <td colSpan={2}>
                y ≈ {fmtCoef(stats.regression.slope)}·x
                {stats.regression.intercept != null &&
                  ` ${stats.regression.intercept >= 0 ? "+" : "−"} ${fmtCoef(Math.abs(stats.regression.intercept))}`}
                {stats.regression.r_squared != null && (
                  <>
                    {" "}
                    · R² = {fmtCoef(stats.regression.r_squared)}{" "}
                    <ExplainerPopover id="rSquared" />
                  </>
                )}
              </td>
              <td className="text-[var(--color-fg-muted)]">all {fmtInt(stats.n)} pairs</td>
            </tr>
          )}
        </tbody>
      </table>
      <div className="flex items-center gap-1">
        <span>
          {swReject
            ? `Normality rejected on ${
                swX?.p != null && swX.p < 0.05 && swY?.p != null && swY.p < 0.05
                  ? "both axes"
                  : swX?.p != null && swX.p < 0.05
                    ? "the x axis"
                    : "the y axis"
              } (Shapiro–Wilk, ${fmtInt(stats.shapiro.n)}-point sample) — quote Spearman's ρ.`
            : swX && swY
              ? `No evidence against normality on either axis (Shapiro–Wilk, ${fmtInt(stats.shapiro.n)}-point sample) — Pearson's r is appropriate.`
              : "Normality check unavailable (sample too small or degenerate)."}
        </span>
        <ExplainerPopover id="shapiroWilk" />
      </div>
    </div>
  );
}
