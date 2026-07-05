"""Kalman-filtered throughput / ETA estimation for progress reporting.

Shared by the CLI progress box (``cli/progress.py``) and the web ingest job
progress (``api/routers/cases.py``) so both surfaces report the same
forensically-reproducible rate/ETA figures from the same byte-based
``progress_callback(total, processed)`` signal.

The ``ETATracker`` estimator is ported near-verbatim from ScalarForensic
(https://github.com/ScalarForensic/ScalarForensic, ``src/scalar_forensic/cli.py``)
at the user's request. ``ThroughputMeter`` is TraceSignal-specific glue that
turns the monotonic ``(total, processed)`` byte stream into wall-clock throughput
observations and exposes the derived metrics as a serializable dict.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


class ETATracker:
    """Kalman-filtered throughput estimator — Θ(1) time and space per update.

    State space: x ∈ ℝ₊ (throughput, bytes/s), A = H = 1 (scalar random walk):

        Predict:  x̂ₜ⁻  = x̂ₜ₋₁                         Φ := 1
                  Pₜ⁻  = Pₜ₋₁ + Q,    Q ∈ ℝ₊

        Update:   Kₜ   = Pₜ⁻ (Pₜ⁻ + R)⁻¹               Kₜ ∈ (0, 1)
                  x̂ₜ   = x̂ₜ⁻ + Kₜ(zₜ − x̂ₜ⁻)
                  Pₜ   = (1 − Kₜ)Pₜ⁻                   (Joseph form, H = 1)

        DARE (t → ∞, unique ℝ₊ root of P∞² + QP∞ − QR = 0):
                  P∞   = ½(√(Q² + 4QR) − Q)
                  K∞   = Q / (Q + √(Q² + 4QR))
          ∀ Q = R/2 :  K∞ = ½                           equal-weight equilibrium ✓

        δ-method (first-order error propagation, η̂ := N_rem / x̂):
                  Var[η̂] ≈ (∂η/∂x)²|_{x=x̂} · Pₜ
                           = (N_rem · x̂⁻²)² · Pₜ
                  σ_η    = N_rem · √Pₜ / x̂²            ±1σ confidence band
    """

    _Q: float = 50.0  # process-noise variance  (bytes/s)²
    _R: float = 100.0  # measurement-noise variance (bytes/s)²

    def __init__(self) -> None:
        self._x: float | None = None  # x̂: current rate estimate (bytes/s)
        self._P: float = 1e8  # P: estimate error variance (diffuse prior)
        self._k: float = 1.0  # Kₜ: Kalman gain at last update (1 = full trust)
        self._n: int = 0  # number of updates applied

    def update(self, n_bytes: int, elapsed_s: float) -> None:
        """Incorporate a new observation.  Θ(1) — scalar predict-update cycle."""
        if elapsed_s <= 0 or n_bytes <= 0:
            return
        z = n_bytes / elapsed_s  # zₜ: observed throughput
        self._n += 1
        if self._x is None:
            self._x = z
            self._P = self._R  # P₁ = R: certainty = measurement quality
            return
        p_pred = self._P + self._Q  # Pₜ⁻ = Pₜ₋₁ + Q
        k = p_pred / (p_pred + self._R)  # Kₜ = Pₜ⁻(Pₜ⁻ + R)⁻¹
        self._x = self._x + k * (z - self._x)  # x̂ₜ = x̂ₜ⁻ + Kₜ(zₜ − x̂ₜ⁻)
        self._P = (1.0 - k) * p_pred  # Pₜ = (1 − Kₜ)Pₜ⁻
        self._k = k

    @property
    def rate(self) -> float | None:
        """x̂ₜ — current optimal rate estimate (bytes/s)."""
        return self._x

    @property
    def rate_std(self) -> float:
        """√Pₜ — 1σ uncertainty on the rate estimate (bytes/s)."""
        return self._P**0.5

    @property
    def kalman_gain(self) -> float:
        """Kₜ — Kalman gain at the most recent update.

        Converges toward K∞ = ½ at steady state (Q = R/2).
        """
        return self._k

    def eta(self, remaining: int) -> tuple[float, float] | None:
        """Return (η̂, σ_η) in seconds, or None if not enough data.

        Θ(1) — closed-form δ-method propagation:
            η̂   = N_rem / x̂
            σ_η = N_rem · √Pₜ / x̂²
        """
        if self._x is None or self._x <= 0 or self._n < 2:
            return None
        eta_s = remaining / self._x  # η̂
        sigma_s = remaining * self.rate_std / self._x**2  # σ_η
        return eta_s, sigma_s


@dataclass(frozen=True)
class ProgressMetrics:
    """Derived Kalman progress metrics for one ``(total, processed)`` snapshot.

    All rates are bytes/s and all durations seconds; ``None`` where there is not
    yet enough data (before the second observation).
    """

    rate_bps: float | None
    rate_std_bps: float
    kalman_gain: float
    eta_s: float | None
    eta_sigma_s: float | None

    def to_dict(self) -> dict[str, float | None]:
        """Serializable form merged into a job's ``progress`` dict for the web UI."""
        return {
            "rate_bps": self.rate_bps,
            "rate_std_bps": self.rate_std_bps,
            "kalman_gain": self.kalman_gain,
            "eta_s": self.eta_s,
            "eta_sigma_s": self.eta_sigma_s,
        }


class ThroughputMeter:
    """Stateful adapter from a monotonic ``(total, processed)`` byte stream to
    Kalman throughput/ETA metrics.

    One instance per ingest run/job. Feed it every ``progress_callback`` value
    via :meth:`observe`; the first observation only seeds the clock (no rate yet)
    and each subsequent one folds the wall-clock delta into the filter.
    """

    def __init__(self) -> None:
        self._tracker = ETATracker()
        self._last_processed = 0
        self._last_t = 0.0
        self._started = False

    def observe(self, total: int, processed: int) -> ProgressMetrics:
        """Fold one progress snapshot into the filter and return current metrics."""
        now = time.perf_counter()
        if not self._started:
            self._started = True
            self._last_processed = processed
            self._last_t = now
            return self._metrics(total, processed)

        self._tracker.update(processed - self._last_processed, now - self._last_t)
        self._last_processed = processed
        self._last_t = now
        return self._metrics(total, processed)

    def _metrics(self, total: int, processed: int) -> ProgressMetrics:
        rate = self._tracker.rate
        eta_s: float | None = None
        eta_sigma_s: float | None = None
        if rate is not None:
            result = self._tracker.eta(max(total - processed, 0))
            if result is not None:
                eta_s, eta_sigma_s = result
        return ProgressMetrics(
            rate_bps=rate,
            rate_std_bps=self._tracker.rate_std,
            kalman_gain=self._tracker.kalman_gain,
            eta_s=eta_s,
            eta_sigma_s=eta_sigma_s,
        )
