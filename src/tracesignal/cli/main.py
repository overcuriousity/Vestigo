"""TraceSignal command-line interface."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from tracesignal import __version__
from tracesignal.db.postgres import PostgresStore, generate_id
from tracesignal.ingestion.files import hash_file
from tracesignal.ingestion.pipeline import EmbeddingPipeline, IngestionPipeline

app = typer.Typer(
    name="tsig",
    help="TraceSignal — local-first forensic log investigation.",
    no_args_is_help=True,
)


def _get_store() -> PostgresStore:
    """Return a PostgresStore instance for CLI operations."""
    return PostgresStore()


@app.command()
def version() -> None:
    """Print the TraceSignal version."""
    typer.echo(__version__)


@app.command()
def ingest(
    path: str = typer.Argument(..., help="Path to log file or directory to ingest."),
    case: str = typer.Option(..., "--case", "-c", help="Target case name."),
    source: str = typer.Option(..., "--source", "-s", help="Source name."),
    format: str | None = typer.Option(
        None,
        "--format",
        "-f",
        help="Parser format (timesketch_csv, jsonl). Inferred from extension if omitted.",
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        "-b",
        help="Number of events to insert per batch (default: TS_INGEST_BATCH_SIZE).",
    ),
) -> None:
    """Ingest a source file into TraceSignal (no embeddings)."""
    path_obj = Path(path).resolve()
    if not path_obj.exists():
        typer.echo(f"ERROR: Path not found: {path}", err=True)
        raise typer.Exit(code=1)

    file_hash = hash_file(path_obj)
    source_id = generate_id(f"{case}:{source}:{file_hash}")

    pipeline = IngestionPipeline(
        case_id=case,
        source_id=source_id,
        batch_size=batch_size,
        file_hash=file_hash,
        source_name=source,
    )
    result = pipeline.run(path_obj, format_name=format)
    typer.echo(result.summary())

    # Persist the Source record and add it to the case default timeline.
    store = _get_store()
    asyncio.run(store.init_schema())

    async def _persist() -> None:
        await store.create_source(
            case_id=case,
            source_id=source_id,
            name=source,
            file_hash=file_hash,
            size_bytes=path_obj.stat().st_size,
            filename=path_obj.name,
            parser=format or "auto",
            event_count=result.events_inserted,
        )
        default_timeline = await store.get_default_timeline(case)
        if default_timeline is not None:
            await store.add_source_to_timeline(case, default_timeline.id, source_id)

    asyncio.run(_persist())

    if result.errors:
        for error in result.errors:
            typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1)


@app.command()
def embed(
    case: str = typer.Option(..., "--case", "-c", help="Target case name."),
    source: str = typer.Option(..., "--source", "-s", help="Source name or ID."),
    batch_size: int = typer.Option(
        64,
        "--batch-size",
        "-b",
        help="Number of events to embed per batch.",
    ),
) -> None:
    """Generate embeddings for an already-ingested source."""
    pipeline = EmbeddingPipeline(
        case_id=case,
        source_ids=[source],
        batch_size=batch_size,
    )
    result = pipeline.run()
    typer.echo(result.summary())

    # Update vector count on the source record.
    store = _get_store()

    async def _update() -> None:
        await store.update_source_counts(
            case_id=case,
            source_id=source,
            vector_count=result.vectors_inserted,
        )

    asyncio.run(_update())

    if result.errors:
        for error in result.errors:
            typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
