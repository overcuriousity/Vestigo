"""Vestigo command-line interface."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from vestigo import __version__
from vestigo.cli.progress import BytesProgressPrinter
from vestigo.db.postgres import PostgresStore, User, generate_id
from vestigo.ingestion.files import hash_file
from vestigo.ingestion.pipeline import EmbeddingPipeline, IngestionPipeline

app = typer.Typer(
    name="vestigo",
    help="Vestigo — local-first forensic log investigation.",
    no_args_is_help=True,
)

cases_app = typer.Typer(help="Inspect cases (admin/CLI use — unscoped, no RBAC gate).")
app.add_typer(cases_app, name="cases")


def _get_store() -> PostgresStore:
    """Return a PostgresStore instance for CLI operations."""
    return PostgresStore()


@app.command()
def version() -> None:
    """Print the Vestigo version."""
    typer.echo(__version__)


@cases_app.command("list")
def cases_list() -> None:
    """List every case with its owner, team, and source count.

    Unscoped — the CLI runs on a trusted admin host (see README), so this
    intentionally bypasses the web UI's per-user RBAC filtering the same way
    ``PostgresStore.list_cases()`` documents itself as "admin/CLI use only".
    """
    store = _get_store()

    async def _run() -> None:
        await store.init_schema()
        cases = await store.list_cases()
        users = {u.id: u for u in await store.list_users()}
        teams = {t.id: t for t in await store.list_teams()}

        if not cases:
            typer.echo("No cases found.")
            return

        rows = []
        for case in cases:
            owner = users.get(case.owner_id or "")
            team = teams.get(case.team_id or "")
            sources = await store.list_sources(case.id)
            rows.append(
                (
                    case.id,
                    case.name,
                    owner.username if owner else "—",
                    team.name if team else "— (personal)",
                    str(len(sources)),
                )
            )

        headers = ("CASE ID", "NAME", "OWNER", "TEAM", "SOURCES")
        widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
        typer.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
        typer.echo("  ".join("-" * w for w in widths))
        for row in rows:
            typer.echo("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)))

    asyncio.run(_run())


async def _resolve_actor(store: PostgresStore, username: str | None) -> User:
    """Resolve and validate the user to attribute a CLI ingest/embed run to.

    If ``username`` is given, it must name an active user. Otherwise, exactly
    one active admin must exist on the system to default to — anything else
    (zero or multiple admins) requires an explicit ``--user``, since guessing
    provenance wrong would corrupt the forensic chain-of-custody record.
    """
    if username is not None:
        user = await store.get_user_by_username(username)
        if user is None or not user.is_active:
            typer.echo(f"ERROR: No active user named '{username}'.", err=True)
            raise typer.Exit(code=1)
        return user

    admins = [u for u in await store.list_users() if u.is_admin and u.is_active]
    if len(admins) == 1:
        return admins[0]
    typer.echo(
        f"ERROR: --user required (found {len(admins)} active admins; "
        "cannot default unambiguously).",
        err=True,
    )
    raise typer.Exit(code=1)


@app.command()
def ingest(
    path: str = typer.Argument(..., help="Path to log file or directory to ingest."),
    case: str = typer.Option(
        ..., "--case", "-c", help="Target case ID (see 'vestigo cases list')."
    ),
    source: str = typer.Option(..., "--source", "-s", help="Source name."),
    format: str | None = typer.Option(
        None,
        "--format",
        "-f",
        help=(
            "Parser format (timesketch_csv, jsonl, vestigo_parquet). "
            "Inferred from extension if omitted."
        ),
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        "-b",
        help="Number of events to insert per batch (default: VESTIGO_INGEST_BATCH_SIZE).",
    ),
    user: str | None = typer.Option(
        None,
        "--user",
        "-u",
        help="Username to attribute this ingest to (default: the sole active admin, if unambiguous).",
    ),
) -> None:
    """Ingest a source file into Vestigo (no embeddings)."""
    path_obj = Path(path).resolve()
    if not path_obj.exists():
        typer.echo(f"ERROR: Path not found: {path}", err=True)
        raise typer.Exit(code=1)

    store = _get_store()

    async def _run() -> None:
        await store.init_schema()
        resolved_user = await _resolve_actor(store, user)
        case_obj = await store.get_case(case)
        if case_obj is None:
            typer.echo(
                f"ERROR: No case with id '{case}'. Run 'vestigo cases list' to see valid IDs.",
                err=True,
            )
            raise typer.Exit(code=1)

        file_hash = hash_file(path_obj)
        source_id = generate_id(f"{case_obj.id}:{source}:{file_hash}")

        typer.echo(
            f"Ingesting {path_obj.name} into case '{case_obj.name}' [{case_obj.id}] "
            f"as user '{resolved_user.username}'"
        )
        printer = BytesProgressPrinter()

        pipeline = IngestionPipeline(
            case_id=case_obj.id,
            source_id=source_id,
            batch_size=batch_size,
            file_hash=file_hash,
            source_name=source,
            progress_callback=printer.on_progress,
        )
        try:
            result = pipeline.run(path_obj, format_name=format)
        except RuntimeError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(result.summary())

        await store.create_source(
            case_id=case_obj.id,
            source_id=source_id,
            name=source,
            file_hash=file_hash,
            size_bytes=path_obj.stat().st_size,
            filename=path_obj.name,
            parser=format or "auto",
            event_count=result.events_inserted,
            created_by=resolved_user.id,
        )
        default_timeline = await store.get_default_timeline(case_obj.id)
        if default_timeline is not None:
            await store.add_source_to_timeline(case_obj.id, default_timeline.id, source_id)
        await store.record_audit(
            action="cli.ingest.source",
            actor=resolved_user,
            case_id=case_obj.id,
            target_type="source",
            target_id=source_id,
            detail={
                "events_inserted": result.events_inserted,
                "events_parsed": result.events_parsed,
                "file_hash": file_hash,
                "filename": path_obj.name,
                "via": "cli",
            },
        )

        if result.errors:
            for error in result.errors:
                typer.echo(f"ERROR: {error}", err=True)
            raise typer.Exit(code=1)

    asyncio.run(_run())


@app.command()
def embed(
    case: str = typer.Option(
        ..., "--case", "-c", help="Target case ID (see 'vestigo cases list')."
    ),
    source: str = typer.Option(..., "--source", "-s", help="Source name or ID."),
    batch_size: int = typer.Option(
        64,
        "--batch-size",
        "-b",
        help="Number of events to embed per batch.",
    ),
    user: str | None = typer.Option(
        None,
        "--user",
        "-u",
        help="Username to attribute this embed run to (default: the sole active admin, if unambiguous).",
    ),
) -> None:
    """Generate embeddings for an already-ingested source."""
    store = _get_store()

    async def _run() -> None:
        await store.init_schema()
        resolved_user = await _resolve_actor(store, user)
        case_obj = await store.get_case(case)
        if case_obj is None:
            typer.echo(
                f"ERROR: No case with id '{case}'. Run 'vestigo cases list' to see valid IDs.",
                err=True,
            )
            raise typer.Exit(code=1)

        pipeline = EmbeddingPipeline(
            case_id=case_obj.id,
            source_ids=[source],
            batch_size=batch_size,
        )
        try:
            result = pipeline.run()
        except RuntimeError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(result.summary())

        await store.update_source_counts(
            case_id=case_obj.id,
            source_id=source,
            vector_count=result.vectors_inserted,
        )
        await store.record_audit(
            action="cli.embed.source",
            actor=resolved_user,
            case_id=case_obj.id,
            target_type="source",
            target_id=source,
            detail={
                "vectors_inserted": result.vectors_inserted,
                "via": "cli",
            },
        )

        if result.errors:
            for error in result.errors:
                typer.echo(f"ERROR: {error}", err=True)
            raise typer.Exit(code=1)

    asyncio.run(_run())


if __name__ == "__main__":
    app()
