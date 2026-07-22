"""Pure-Python statistical inference helpers for visualization aggregations.

ClickHouse computes the descriptive side natively (``corr``, ``rankCorr``,
``simpleLinearRegression``, ``skewPop``, quantiles); this module supplies only
what ClickHouse has no aggregate for — significance (p-values), Kendall's
tau-b, the Shapiro–Wilk normality test, and the Freedman–Diaconis bin rule.

scipy is deliberately not a dependency (airgapped installs stay slim), so the
special functions are implemented directly:

- Regularized incomplete beta via the modified Lentz continued fraction
  (Numerical Recipes §6.4), which gives the Student-t survival function in
  closed form.
- Shapiro–Wilk after Royston (1995), Applied Statistics algorithm AS R94 —
  the same approximation scipy wraps — valid for 3 <= n <= 5000.
- Normal quantiles via the Acklam rational approximation (relative error
  < 1.15e-9), normal tails via :func:`math.erfc`.

Every function returns ``None`` (or ``(None, None)``) instead of raising when
the input is degenerate (too few points, zero variance), so callers can embed
results directly into nullable JSON response fields.

Tests pin these implementations against scipy-computed reference constants
(see ``tests/test_stats.py``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = [
    "fd_bin_count",
    "kendall_tau",
    "normal_ppf",
    "normal_sf",
    "pearson_p",
    "regularized_incomplete_beta",
    "shapiro_wilk",
    "spearman_p",
    "student_t_sf",
]

_MAX_CF_ITERATIONS = 300
_CF_EPSILON = 3.0e-14


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b).

    Modified Lentz continued-fraction evaluation (Numerical Recipes §6.4),
    using the symmetry transform when x is past the distribution bulk so the
    fraction converges quickly on either side.
    """
    if not 0.0 <= x <= 1.0:
        raise ValueError(f"x must be in [0, 1], got {x}")
    if x == 0.0 or x == 1.0:
        return x

    ln_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(ln_front)

    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_cf(a, b, x) / a
    return 1.0 - front * _beta_cf(b, a, 1.0 - x) / b


def _beta_cf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz method)."""
    tiny = 1.0e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_CF_ITERATIONS + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _CF_EPSILON:
            break
    return h


def student_t_sf(t: float, df: float) -> float:
    """One-sided survival function P(T > t) of Student's t with *df* dof."""
    if df <= 0:
        raise ValueError(f"df must be positive, got {df}")
    if math.isnan(t):
        return math.nan
    if math.isinf(t):
        return 0.0 if t > 0 else 1.0
    x = df / (df + t * t)
    half_tail = 0.5 * regularized_incomplete_beta(df / 2.0, 0.5, x)
    return half_tail if t > 0 else 1.0 - half_tail


def normal_sf(z: float) -> float:
    """One-sided survival function P(Z > z) of the standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


# Acklam's rational approximation coefficients for the normal quantile.
_PPF_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_PPF_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_PPF_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_PPF_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)


def normal_ppf(p: float) -> float:
    """Standard normal quantile function (Acklam approximation)."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p}")
    p_low, p_high = 0.02425, 1.0 - 0.02425
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        num = (
            (((_PPF_C[0] * q + _PPF_C[1]) * q + _PPF_C[2]) * q + _PPF_C[3]) * q + _PPF_C[4]
        ) * q + _PPF_C[5]
        den = (((_PPF_D[0] * q + _PPF_D[1]) * q + _PPF_D[2]) * q + _PPF_D[3]) * q + 1.0
        return num / den
    if p > p_high:
        q = math.sqrt(-2.0 * math.log1p(-p))
        num = (
            (((_PPF_C[0] * q + _PPF_C[1]) * q + _PPF_C[2]) * q + _PPF_C[3]) * q + _PPF_C[4]
        ) * q + _PPF_C[5]
        den = (((_PPF_D[0] * q + _PPF_D[1]) * q + _PPF_D[2]) * q + _PPF_D[3]) * q + 1.0
        return -num / den
    q = p - 0.5
    r = q * q
    num = (
        (((_PPF_A[0] * r + _PPF_A[1]) * r + _PPF_A[2]) * r + _PPF_A[3]) * r + _PPF_A[4]
    ) * r + _PPF_A[5]
    den = (
        (((_PPF_B[0] * r + _PPF_B[1]) * r + _PPF_B[2]) * r + _PPF_B[3]) * r + _PPF_B[4]
    ) * r + 1.0
    return q * num / den


def pearson_p(r: float, n: int) -> float | None:
    """Two-sided p-value for a Pearson correlation of *r* over *n* pairs.

    t-transform: t = r * sqrt((n-2) / (1-r²)), df = n-2. Exact under
    bivariate normality of the underlying data.
    """
    return _corr_p(r, n)


def spearman_p(rho: float, n: int) -> float | None:
    """Two-sided p-value for a Spearman rank correlation of *rho* over *n* pairs.

    Same t-approximation as Pearson's; adequate for n >= 10 or so, stated in
    the UI explainer rather than hidden here.
    """
    return _corr_p(rho, n)


def _corr_p(coef: float, n: int) -> float | None:
    if n < 3 or coef is None or math.isnan(coef):
        return None
    c = max(-1.0, min(1.0, coef))
    if abs(c) >= 1.0:
        return 0.0
    t = c * math.sqrt((n - 2) / (1.0 - c * c))
    return min(1.0, 2.0 * student_t_sf(abs(t), n - 2))


def kendall_tau(xs: Sequence[float], ys: Sequence[float]) -> tuple[float | None, float | None]:
    """Kendall's tau-b with two-sided p-value (normal approximation).

    Tie-corrected tau-b over all pairs — O(n²), acceptable at the <= 1000
    point sample cap this is used with. The p-value uses the tie-corrected
    variance of the concordance statistic S (Kendall 1970) with no continuity
    correction, matching scipy's asymptotic method.
    """
    n = min(len(xs), len(ys))
    if n < 3:
        return None, None
    concordant_minus_discordant = 0
    for i in range(n):
        xi, yi = xs[i], ys[i]
        for j in range(i + 1, n):
            dx = (xi > xs[j]) - (xi < xs[j])
            dy = (yi > ys[j]) - (yi < ys[j])
            concordant_minus_discordant += dx * dy
    n0 = n * (n - 1) // 2
    n1 = _tie_pair_count(xs)
    n2 = _tie_pair_count(ys)
    denom = math.sqrt(float(n0 - n1)) * math.sqrt(float(n0 - n2))
    if denom == 0.0:
        return None, None
    tau = concordant_minus_discordant / denom

    var_s = _kendall_var_s(xs, ys, n)
    if var_s <= 0.0:
        return tau, None
    z = abs(concordant_minus_discordant) / math.sqrt(var_s)
    p = min(1.0, 2.0 * normal_sf(z))
    return tau, p


def _tie_counts(values: Sequence[float]) -> list[int]:
    counts: dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return [c for c in counts.values() if c > 1]


def _tie_pair_count(values: Sequence[float]) -> int:
    return sum(c * (c - 1) // 2 for c in _tie_counts(values))


def _kendall_var_s(xs: Sequence[float], ys: Sequence[float], n: int) -> float:
    """Tie-corrected variance of Kendall's S (Kendall 1970, eq. 4.5)."""
    tx = _tie_counts(xs)
    ty = _tie_counts(ys)

    def s1(ties: list[int]) -> float:
        return float(sum(t * (t - 1) * (2 * t + 5) for t in ties))

    def s2(ties: list[int]) -> float:
        return float(sum(t * (t - 1) * (t - 2) for t in ties))

    def s3(ties: list[int]) -> float:
        return float(sum(t * (t - 1) for t in ties))

    var = (n * (n - 1) * (2 * n + 5) - s1(tx) - s1(ty)) / 18.0
    var += s2(tx) * s2(ty) / (9.0 * n * (n - 1) * (n - 2)) if n > 2 else 0.0
    var += s3(tx) * s3(ty) / (2.0 * n * (n - 1))
    return var


# Royston (1995) AS R94 polynomial coefficients.
_SW_C1 = (0.0, 0.221157, -0.147981, -2.071190, 4.434685, -2.706056)
_SW_C2 = (0.0, 0.042981, -0.293762, -1.752461, 5.682633, -3.582633)
_SW_C3 = (0.5440, -0.39978, 0.025054, -6.714e-4)
_SW_C4 = (1.3822, -0.77857, 0.062767, -0.0020322)
_SW_C5 = (-1.5861, -0.31082, -0.083751, 0.0038915)
_SW_C6 = (-0.4803, -0.082676, 0.0030302)


def _poly(coeffs: Sequence[float], x: float) -> float:
    result = 0.0
    for c in reversed(coeffs):
        result = result * x + c
    return result


def shapiro_wilk(values: Sequence[float]) -> tuple[float | None, float | None]:
    """Shapiro–Wilk W and two-sided p-value, after Royston (1995), AS R94.

    Valid for 3 <= n <= 5000 (Royston's stated range). Returns (None, None)
    outside that range or for zero-variance samples. Matches scipy's
    ``shapiro`` (which wraps the same algorithm) to ~1e-4 on W.
    """
    n = len(values)
    if n < 3 or n > 5000:
        return None, None
    xs = sorted(float(v) for v in values)
    if xs[0] == xs[-1]:
        return None, None

    # Expected normal order statistics (Blom scores) and their normalization.
    m = [normal_ppf((i - 0.375) / (n + 0.25)) for i in range(1, n + 1)]
    m_sumsq = sum(v * v for v in m)
    rsn = 1.0 / math.sqrt(n)

    weights = [0.0] * n
    if n == 3:
        weights[0] = -math.sqrt(0.5)
        weights[2] = math.sqrt(0.5)
    else:
        a_n = _poly(_SW_C1, rsn) + m[-1] / math.sqrt(m_sumsq)
        if n > 5:
            a_n1 = _poly(_SW_C2, rsn) + m[-2] / math.sqrt(m_sumsq)
            phi = (m_sumsq - 2.0 * m[-1] ** 2 - 2.0 * m[-2] ** 2) / (
                1.0 - 2.0 * a_n**2 - 2.0 * a_n1**2
            )
            weights[-1], weights[0] = a_n, -a_n
            weights[-2], weights[1] = a_n1, -a_n1
            core = range(2, n - 2)
        else:
            phi = (m_sumsq - 2.0 * m[-1] ** 2) / (1.0 - 2.0 * a_n**2)
            weights[-1], weights[0] = a_n, -a_n
            core = range(1, n - 1)
        sqrt_phi = math.sqrt(phi)
        for i in core:
            weights[i] = m[i] / sqrt_phi

    mean = sum(xs) / n
    ssq = sum((v - mean) ** 2 for v in xs)
    if ssq <= 0.0:
        return None, None
    w_num = sum(w * v for w, v in zip(weights, xs, strict=True)) ** 2
    w = w_num / ssq
    w = min(1.0, w)

    # Significance (Royston 1995).
    if n == 3:
        p = 6.0 / math.pi * (math.asin(math.sqrt(w)) - math.asin(math.sqrt(0.75)))
        return w, max(0.0, min(1.0, p))
    one_minus_w = max(1.0 - w, 1e-300)
    if n <= 11:
        gamma = -2.273 + 0.459 * n
        if gamma - math.log(one_minus_w) <= 0.0:
            return w, 0.0
        mu = _poly(_SW_C3, float(n))
        sigma = math.exp(_poly(_SW_C4, float(n)))
        z = (-math.log(gamma - math.log(one_minus_w)) - mu) / sigma
    else:
        ln_n = math.log(float(n))
        mu = _poly(_SW_C5, ln_n)
        sigma = math.exp(_poly(_SW_C6, ln_n))
        z = (math.log(one_minus_w) - mu) / sigma
    return w, max(0.0, min(1.0, normal_sf(z)))


def fd_bin_count(iqr: float, n: int, span: float) -> int | None:
    """Freedman–Diaconis bin count: span / (2·IQR·n^(-1/3)).

    Returns None when the rule is undefined (no spread, no interquartile
    range, or too few points) — the caller falls back to its manual default.
    The result is NOT clamped here; the caller owns its [min, max] policy.
    """
    if n < 2 or iqr <= 0.0 or span <= 0.0:
        return None
    width = 2.0 * iqr * n ** (-1.0 / 3.0)
    if width <= 0.0:
        return None
    return max(1, math.ceil(span / width))
