"""TraceVector command-line interface."""

from __future__ import annotations

from pathlib import Path

import typer

from tracevector import __version__
from tracevector.ingestion.pipeline import EmbeddingPipeline, IngestionPipeline

app = typer.Typer(
    name="tv",
    help="TraceVector — local-first forensic log investigation.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the TraceVector version."""
    typer.echo(__version__)


@app.command()
def ingest(
    path: str = typer.Argument(..., help="Path to log file or directory to ingest."),
    case: str = typer.Option(..., "--case", "-c", help="Target case name."),
    timeline: str = typer.Option(..., "--timeline", "-t", help="Timeline name."),
    format: str | None = typer.Option(
        None,
        "--format",
        "-f",
        help="Parser format (timesketch_csv, jsonl). Inferred from extension if omitted.",
    ),
    batch_size: int = typer.Option(
        64,
        "--batch-size",
        "-b",
        help="Number of events to insert per batch.",
    ),
) -> None:
    """Ingest timeline events into TraceVector (no embeddings)."""
    pipeline = IngestionPipeline(
        case_id=case,
        timeline_id=timeline,
        batch_size=batch_size,
    )
    result = pipeline.run(Path(path), format_name=format)
    typer.echo(result.summary())
    if result.errors:
        for error in result.errors:
            typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1)


@app.command()
def embed(
    case: str = typer.Option(..., "--case", "-c", help="Target case name."),
    timeline: str = typer.Option(..., "--timeline", "-t", help="Timeline name."),
    batch_size: int = typer.Option(
        64,
        "--batch-size",
        "-b",
        help="Number of events to embed per batch.",
    ),
) -> None:
    """Generate embeddings for an already-ingested timeline."""
    pipeline = EmbeddingPipeline(
        case_id=case,
        timeline_id=timeline,
        batch_size=batch_size,
    )
    result = pipeline.run()
    typer.echo(result.summary())
    if result.errors:
        for error in result.errors:
            typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
