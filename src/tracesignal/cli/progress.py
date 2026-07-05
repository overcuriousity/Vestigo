"""CLI progress reporting for large ingestion runs.

The block-bar renderer and duration formatter below are ported near-verbatim
from ScalarForensic (https://github.com/ScalarForensic/ScalarForensic,
``src/scalar_forensic/cli.py``) at the user's request, so the CLI's progress
display matches that tool's look and feel. The Kalman ETA estimator itself
lives in ``core/eta.py`` so the web ingest job (``api/routers/cases.py``) shows
the same figures. ``BytesProgressPrinter`` is TraceSignal-specific glue that
adapts ``IngestionPipeline``'s byte-based ``progress_callback(total, processed)``
signal (see ``ingestion/pipeline.py``) into that widget.
"""

from __future__ import annotations

import sys
import time

import typer

from tracesignal.core.eta import ProgressMetrics, ThroughputMeter


def _progress_bar(pct: float, width: int = 28) -> str:
    """Unicode block-element progress bar."""
    filled = round(width * min(max(pct, 0.0), 100.0) / 100)
    return "█" * filled + "░" * (width - filled)


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


class BytesProgressPrinter:
    """Adapts ``IngestionPipeline``'s byte-based progress callback to the
    ScalarForensic-style Kalman progress box.

    Call ``on_progress(total=..., processed=...)`` — the exact signature
    ``IngestionPipeline``/``EmbeddingPipeline`` invoke their callback with.
    """

    _REFRESH_INTERVAL_S = 0.5
    _BAR_WIDTH = 28
    _SEP = "─" * 68

    def __init__(self, label: str = "") -> None:
        self.label = label
        self._meter = ThroughputMeter()
        self._latest: ProgressMetrics | None = None
        self._last_render_t = 0.0
        self._is_tty = sys.stdout.isatty()
        self._started = False

    def on_progress(self, total: int, processed: int) -> None:
        self._latest = self._meter.observe(total, processed)
        now = time.perf_counter()
        if not self._started:
            self._started = True
            self._render(total, processed, force=True)
            return

        done = total > 0 and processed >= total
        if done or (now - self._last_render_t) >= self._REFRESH_INTERVAL_S:
            self._render(total, processed, force=done)

    def _render(self, total: int, processed: int, force: bool = False) -> None:
        self._last_render_t = time.perf_counter()
        pct = (processed / total * 100) if total > 0 else 0.0
        bar = _progress_bar(pct, width=self._BAR_WIDTH)
        processed_mb = processed / 1e6
        total_mb = total / 1e6

        eta_part = ""
        metrics = self._latest
        if metrics is not None and metrics.rate_bps is not None:
            eta_s, sigma_s = metrics.eta_s, metrics.eta_sigma_s
            eta_str = f"~ {_fmt_duration(eta_s)}" if eta_s is not None else "~ —"
            sigma_str = f"± {_fmt_duration(sigma_s)}" if sigma_s is not None else "± —"
            eta_part = (
                f"  x̂ = {metrics.rate_bps / 1e6:.1f} MB/s"
                f"  √P = {metrics.rate_std_bps / 1e6:.1f}"
                f"  K = {metrics.kalman_gain:.3f}"
                f"  ·  η̂ {eta_str}"
                f"  σ_η {sigma_str}"
            )

        line1 = f"  [{bar}]  {processed_mb:,.1f} / {total_mb:,.1f} MB  ({pct:.1f}%)"

        if self._is_tty and not force:
            typer.echo(f"\r{line1}{eta_part}" + " " * 8, nl=False)
        elif self._is_tty and force:
            typer.echo(f"\r{line1}{eta_part}" + " " * 8)
        else:
            # Non-TTY (piped/redirected): emit stable multi-line boxes instead
            # of carriage-return redraws.
            typer.echo(f"  {self._SEP}")
            typer.echo(line1)
            if eta_part:
                typer.echo(eta_part.strip())
            typer.echo(f"  {self._SEP}")
