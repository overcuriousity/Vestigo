/**
 * All teaching copy for statistics and chart concepts in one place, so
 * wording is reviewable centrally (same rationale as `lib/guidance.ts`).
 *
 * Audience: an analyst with no statistics background and little patience.
 * Every entry is three short blocks — what it is, how to read it, when to
 * distrust it — plus the formula the number actually comes from. Keep each
 * block to at most three short sentences; jargon only when the term itself
 * is the thing being explained.
 *
 * Rendered by `viz/primitives/ExplainerPopover.tsx`. `CHART_HOW_TO_READ`
 * feeds the one-line "how to read this chart" hint on chart headers.
 */
import type { ChartType } from "./chartConfig";

export interface Explainer {
  title: string;
  /** What is this number/concept? */
  what: string;
  /** How do I read the value I'm looking at? */
  howToRead: string;
  /** When is it misleading? */
  distrust: string;
  /** The formula behind the number, as plain Unicode text. */
  formula?: string;
}

export type ExplainerId =
  | "mean"
  | "median"
  | "quartiles"
  | "iqr"
  | "whiskers"
  | "skewness"
  | "kde"
  | "fdRule"
  | "pearson"
  | "spearman"
  | "kendall"
  | "pValue"
  | "shapiroWilk"
  | "regression"
  | "rSquared"
  | "correlationMatrix"
  | "waffle"
  | "sampledPoints";

export const EXPLAINERS: Record<ExplainerId, Explainer> = {
  mean: {
    title: "Mean (average)",
    what: "All values added up, divided by how many there are.",
    howToRead:
      "The balance point of the data. If mean and median sit far apart, a few extreme values are dragging the mean toward them.",
    distrust:
      "One huge outlier moves the mean a lot. For lopsided data (bytes, durations), the median usually describes 'typical' better.",
    formula: "x̄ = (x₁ + x₂ + … + xₙ) / n",
  },
  median: {
    title: "Median",
    what: "The middle value: half the events are below it, half above.",
    howToRead: "A robust 'typical value' — unlike the mean, outliers barely move it.",
    distrust:
      "It says nothing about spread or shape. Two very different distributions can share a median.",
    formula: "sort the values; take the middle one (or the mean of the two middle ones)",
  },
  quartiles: {
    title: "Quartiles (Q1 / Q3)",
    what: "Q1 is the value a quarter of the data sits below; Q3 the value three quarters sit below.",
    howToRead:
      "The box in a box plot spans Q1 to Q3 — the middle 50% of all values live inside it.",
    distrust:
      "Quartiles hide how values are arranged inside the box. A two-humped distribution looks the same as a flat one.",
    formula: "Q1 = 25th percentile, Q3 = 75th percentile",
  },
  iqr: {
    title: "Interquartile range (IQR)",
    what: "The width of the middle 50% of the data: Q3 minus Q1.",
    howToRead: "Bigger IQR = more spread in the bulk of the data. Immune to outliers.",
    distrust: "An IQR of 0 (many identical values) makes IQR-based rules like whiskers meaningless.",
    formula: "IQR = Q3 − Q1",
  },
  whiskers: {
    title: "Whiskers & outliers",
    what: "The whiskers reach out to the last value within 1.5 × IQR beyond the box; points past them are drawn as potential outliers.",
    howToRead:
      "Dots beyond the whiskers are unusually small or large values — in logs, often the interesting events.",
    distrust:
      "1.5 × IQR is a convention, not a law. In heavy-tailed data (bytes, durations) many perfectly normal values fall outside it.",
    formula: "whisker limit = Q1 − 1.5·IQR (low), Q3 + 1.5·IQR (high)",
  },
  skewness: {
    title: "Skewness (g₁)",
    what: "A number describing lopsidedness. 0 = symmetric; positive = long tail to the right; negative = long tail to the left.",
    howToRead:
      "Right-skewed (g₁ > 0): mode < median < mean — most values small, a few huge. Typical for bytes and durations. |g₁| < 0.5 counts as roughly symmetric.",
    distrust:
      "One extreme outlier can dominate g₁ completely. Look at the histogram shape, not just the number.",
    formula: "g₁ = (1/n) Σ ((xᵢ − x̄) / s)³",
  },
  kde: {
    title: "Density curve (KDE)",
    what: "A smoothed version of the histogram — an estimate of the value distribution without hard bin edges.",
    howToRead:
      "Humps are clusters of common values. Two humps (bimodal) usually mean two different behaviors mixed together — worth splitting by another field.",
    distrust:
      "Smoothing invents detail between bins and can smear sharp spikes. The bars are the data; the curve is an interpretation.",
    formula: "smoothed from the histogram bins with a small moving window",
  },
  fdRule: {
    title: "Automatic bins (Freedman–Diaconis)",
    what: "A rule that picks the histogram bin width from the data's spread (IQR) and count, instead of a fixed number of bins.",
    howToRead:
      "Fewer, wider bins for small/noisy data; more, narrower bins when there is enough data to support the detail.",
    distrust:
      "With extreme outliers the span gets huge and even the 'right' width can bunch everything into a few bins. Switch to manual bins or filter the outliers.",
    formula: "bin width = 2·IQR·n^(−1/3); bin count = span / width (clamped to 5…60)",
  },
  pearson: {
    title: "Pearson correlation (r)",
    what: "How close the points lie to a straight line. +1 = perfect rising line, −1 = perfect falling line, 0 = no linear relationship.",
    howToRead:
      "|r| above ~0.7 is a strong linear relationship; below ~0.3 weak. The sign tells the direction.",
    distrust:
      "Only measures STRAIGHT-line relationships; a perfect U-shape can score r ≈ 0. Sensitive to outliers. And correlation is not causation.",
    formula: "r = Σ(xᵢ−x̄)(yᵢ−ȳ) / √(Σ(xᵢ−x̄)² · Σ(yᵢ−ȳ)²)",
  },
  spearman: {
    title: "Spearman rank correlation (ρ)",
    what: "Pearson's r computed on ranks instead of raw values — measures whether y consistently rises (or falls) with x, straight line or not.",
    howToRead:
      "Same scale as r: ±1 = perfectly monotonic, 0 = none. Prefer it when data is skewed or has outliers.",
    distrust:
      "Ignores the size of changes, only their order. Many tied values weaken its meaning.",
    formula: "ρ = 1 − 6·Σdᵢ² / (n(n²−1)), dᵢ = rank difference of pair i",
  },
  kendall: {
    title: "Kendall's tau (τ)",
    what: "Compares every pair of points: do x and y move in the same direction? τ = (agreeing pairs − disagreeing pairs) / all pairs.",
    howToRead:
      "±1 = perfectly consistent direction, 0 = none. More robust than Spearman for small samples; its values run closer to 0 — that's normal.",
    distrust:
      "Computed here on the drawn sample, not all events. Don't compare its magnitude directly against r or ρ.",
    formula: "τ_b = (concordant − discordant) / √((n₀−ties_x)(n₀−ties_y))",
  },
  pValue: {
    title: "p-value",
    what: "The probability of seeing a correlation at least this strong if in truth there were NO relationship — just random chance.",
    howToRead:
      "Small p (< 0.05 by convention) = the pattern would be surprising under pure chance. It does NOT measure how strong or important the effect is.",
    distrust:
      "With millions of events, even a meaningless r = 0.01 gets a tiny p-value. Judge strength from r itself, p only tells you it isn't noise.",
    formula: "from t = r·√((n−2)/(1−r²)) against a Student-t distribution",
  },
  shapiroWilk: {
    title: "Shapiro–Wilk normality test",
    what: "Tests whether the values look like they came from a normal (bell-curve) distribution. W near 1 = bell-like.",
    howToRead:
      "p ≥ 0.05: no evidence against normality → Pearson's r is trustworthy. p < 0.05: data is not bell-shaped → prefer Spearman's ρ.",
    distrust:
      "Computed on the drawn sample. With large samples it flags even harmless deviations; with tiny samples it misses real ones.",
    formula: "W = (Σ aᵢ·x₍ᵢ₎)² / Σ(xᵢ−x̄)², coefficients aᵢ from expected normal order statistics",
  },
  regression: {
    title: "Regression line",
    what: "The straight line through the points that minimizes the summed squared vertical distances — the best linear description of 'y per x'.",
    howToRead:
      "The slope is the estimated change in y per one unit of x. The line only summarizes; individual points scatter around it.",
    distrust:
      "A line always fits, even through a cloud with no relationship — check r and R² first. Outliers pull the line hard. Never extrapolate beyond the data range.",
    formula: "y ≈ β₀ + β₁·x, β₁ = slope, β₀ = intercept (least squares)",
  },
  rSquared: {
    title: "R² (explained variance)",
    what: "The share of the variation in y that the straight line accounts for, from 0 (none) to 1 (all).",
    howToRead: "R² = 0.4 means the line explains 40% of y's variability; 60% is scatter it cannot explain.",
    distrust:
      "High R² does not mean the relationship is causal, and low R² can still hide a strong non-linear pattern.",
    formula: "R² = r² (for a simple linear regression)",
  },
  correlationMatrix: {
    title: "Correlation matrix",
    what: "Every pair of the selected numeric fields gets one cell holding its correlation coefficient.",
    howToRead:
      "Strong colors (near +1 or −1) = strongly related pair — click the cell to see the actual scatter plot. Pale cells ≈ unrelated.",
    distrust:
      "Each cell compresses a whole scatter plot into one number — always inspect interesting cells as scatter before concluding anything. Correlation is not causation.",
    formula: "one Pearson r (or Spearman ρ) per field pair",
  },
  waffle: {
    title: "Waffle chart",
    what: "A 10×10 grid where every cell is one percent of the total; each category owns a block of cells.",
    howToRead:
      "Count cells to read a share — 12 cells means 12%. Every category present gets at least one cell, even a tiny one.",
    distrust:
      "Cell counts are rounded to whole percent, so they approximate the exact figures in the legend. Only meaningful when the categories are parts of one whole.",
    formula: "cells = round(share × 100), allocated by largest remainder so the total is exactly 100",
  },
  sampledPoints: {
    title: "Sampled points",
    what: "Only a uniform random sample of events is drawn — plotting every event would be unreadable and slow.",
    howToRead:
      "The sample preserves the overall shape. Axes and summary statistics still cover the FULL data unless labeled otherwise.",
    distrust:
      "Rare extremes may be missing from the drawn points. The caption states exactly how many of how many are shown.",
  },
};

/** One-line reading aid per chart type, shown on the chart header. */
export const CHART_HOW_TO_READ: Record<ChartType, string> = {
  time: "Each bar counts events in one time slice — spikes are bursts of activity, gaps are silence.",
  bar: "Bar length = event count per value. Compare lengths, not colors.",
  pie: "Slice angle = share of the whole. All slices together are 100% of the filtered events.",
  waffle:
    "100 cells = 100% of the filtered events; count cells to read a share. Easier to read than a pie once there are several categories.",
  heatmap: "Rows are values, columns are time — darker cells mean more events then and there.",
  line: "Height = how often that value occurred per time slice; the line connects measurements, it does not measure between them.",
  histogram:
    "Bar height = how many events fall in that value range. The shape shows typical values, spread, and outliers.",
  box: "The box is the middle 50% of values, the line inside is the median, dots beyond the whiskers are potential outliers.",
  violin:
    "Width = how common values are at that height — a smoothed, mirrored histogram. Humps are clusters.",
  ecdf: "Height at x = fraction of events with a value ≤ x. Steep sections are where most values sit.",
  punchcard: "Day-of-week × hour-of-day grid — recurring dark cells reveal rhythms, off-hours activity stands out.",
  pivot: "Cell darkness = events with that combination of the two field values.",
  sankey: "Ribbon width = events flowing from the left value to the right value.",
  corr: "One cell per field pair; colour and number give the correlation (−1…+1). Click a cell to see the scatter behind it.",
  scatter: "One dot per event pair — patterns (lines, clusters, outliers) mean the two fields are related.",
};
