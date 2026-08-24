"""Building the demo case: generate the sources, ingest them, add the notes.

Runs at seed time rather than shipping a prebuilt archive. The generator is
deterministic (see ``scenario.rng``), so every user's copy is byte-identical
down to the source files' SHA-256 hashes — the provenance story is the same one
a real ingest tells, and the repository carries code instead of a 146 MiB
binary that would have to be regenerated and re-committed on every schema
change.

Cost is a few seconds of background CPU per user: roughly 2.5s to generate the
four files and another handful to ingest 250k events through the real
``IngestionPipeline``. Nothing here writes outside the case it creates.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.field_stats import refresh_source_field_stats
from vestigo.db.postgres import PostgresStore, generate_id
from vestigo.demo import metadata, scenario
from vestigo.demo.sources import linux, netflow, proxy, windows
from vestigo.ingestion.files import hash_file
from vestigo.ingestion.pipeline import IngestionPipeline
from vestigo.sigma.rules import parse_rule_yaml, rule_key_for
from vestigo.stories.refs import validate_block_scope
from vestigo.stories.schemas import validate_block_content

logger = logging.getLogger(__name__)

CASE_NAME = "Demo — contractor account compromise"
CASE_DESCRIPTION = (
    "A fabricated investigation, shipped with Vestigo as a worked example. "
    "Four sources over 30 days: an intrusion begins on 24 May against a quiet "
    "three-week baseline. Nothing here is real data; delete it whenever you like."
)

#: Source key, display name, filename, writer.
SOURCES = (
    (
        "windows",
        "Windows Security (workstations + DCs)",
        "winsec-workstations.csv",
        windows.write_windows_csv,
    ),
    ("linux", "Linux auth and syslog", "linux-auth.jsonl", linux.write_linux_jsonl),
    ("proxy", "Web proxy", "proxy.csv", proxy.write_proxy_csv),
    ("netflow", "Firewall netflow", "fw-netflow.csv", netflow.write_netflow_csv),
)

TIMELINES = (
    ("Endpoint", "Windows and Linux hosts.", ("windows", "linux")),
    ("Network", "Proxy and firewall.", ("proxy", "netflow")),
    ("Full incident", "Every source, one timeline.", ("windows", "linux", "proxy", "netflow")),
)


def _generate_sources(work: Path) -> dict[str, Path]:
    """Write the four source files and return their paths by key."""
    paths: dict[str, Path] = {}
    for key, _name, filename, writer in SOURCES:
        path = work / filename
        count = writer(path)
        logger.debug("demo source %s: %d rows", filename, count)
        paths[key] = path
    return paths


async def _ingest(
    store: PostgresStore,
    clickhouse: ClickHouseStore,
    case_id: str,
    user_id: str,
    paths: dict[str, Path],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, str]:
    """Ingest every source through the real pipeline; return source ids by key."""
    source_ids: dict[str, str] = {}
    for index, (key, name, _filename, _writer) in enumerate(SOURCES):
        path = paths[key]
        file_hash = hash_file(path)
        source_id = generate_id(f"{case_id}:{key}:{file_hash}")
        pipeline = IngestionPipeline(
            case_id=case_id,
            source_id=source_id,
            clickhouse=clickhouse,
            file_hash=file_hash,
            source_name=name,
        )
        result = await asyncio.to_thread(pipeline.run, path)
        if result.errors:
            raise RuntimeError(f"{key}: ingestion reported errors: {result.errors}")
        await store.create_source(
            case_id=case_id,
            source_id=source_id,
            name=name,
            file_hash=file_hash,
            size_bytes=path.stat().st_size,
            filename=path.name,
            parser="auto",
            event_count=result.events_inserted,
            created_by=user_id,
        )
        await refresh_source_field_stats(store, clickhouse, case_id, source_id)
        logger.debug("demo ingest %s: %d events", key, result.events_inserted)
        source_ids[key] = source_id
        if progress:
            progress({"processed": index + 1})
    return source_ids


async def _timelines(
    store: PostgresStore, case_id: str, source_ids: dict[str, str]
) -> dict[str, str]:
    """Create the three timelines and attach their sources."""
    timeline_ids: dict[str, str] = {}
    default = await store.get_default_timeline(case_id)
    if default is not None:
        for source_id in source_ids.values():
            await store.add_source_to_timeline(case_id, default.id, source_id)
    for name, description, keys in TIMELINES:
        timeline = await store.create_timeline(
            case_id=case_id,
            timeline_id=generate_id(name),
            name=name,
            description=description,
            source_ids=[source_ids[k] for k in keys],
        )
        timeline_ids[name] = timeline.id
    return timeline_ids


async def _suggest_columns(store: PostgresStore, clickhouse: ClickHouseStore, case_id: str) -> None:
    """Give every demo timeline its recommended columns (issue #213).

    The demo case never passes through the upload endpoint or the timeline
    router, so it would otherwise miss both scheduling hooks — and the very
    first timeline a new user opens is exactly the one the suggestion exists
    for. Awaited rather than spawned, so the case is complete when the seed
    job reports done. Best-effort — including the two calls outside the job
    itself, since a failure here must leave the seeded case usable rather than
    fail a first login over its column layout.

    The scorer runs locally (``use_llm`` defaults to False), so seeding never
    waits on a model endpoint and seeds no content the user has not opted into.
    """
    from vestigo.columns.jobs import JOB_KIND, run_column_recommendation_job
    from vestigo.core.jobs import JobStore

    try:
        job_store = JobStore()
        for timeline in await store.list_timelines(case_id):
            job = job_store.create(kind=JOB_KIND, case_id=case_id)
            await run_column_recommendation_job(
                job_id=job.id,
                case_id=case_id,
                timeline_id=timeline.id,
                job_store=job_store,
                store=store,
                ch_store=clickhouse,
            )
    except Exception:  # noqa: BLE001 — advisory step, never fails the seed
        logger.exception("Column suggestion skipped for demo case %s", case_id)


async def _artifacts(
    store: PostgresStore,
    clickhouse: ClickHouseStore,
    case_id: str,
    user_id: str,
    source_ids: dict[str, str],
    timeline_ids: dict[str, str],
) -> int:
    """Attach the analyst's own work; returns how many notes were placed."""
    resolved = await asyncio.to_thread(
        metadata.resolve_annotation_events, clickhouse, case_id, source_ids
    )
    rows = metadata.tag_annotation_rows(resolved, case_id, user_id)
    await store.bulk_create_annotations(rows)

    view_ids: dict[str, str] = {}
    for view in metadata.VIEWS:
        created = await store.create_view(
            case_id=case_id,
            view_id=generate_id(view.name),
            name=view.name,
            query=view.query,
            view_filter=view.payload,
        )
        view_ids[view.name] = created.id

    chart_ids: dict[str, str] = {}
    for chart in metadata.CHARTS:
        saved = await store.create_saved_chart(
            case_id=case_id,
            timeline_id=timeline_ids[chart.timeline],
            chart_id=generate_id(f"demo-chart:{chart.name}"),
            name=chart.name,
            config=chart.config,
        )
        chart_ids[chart.name] = saved.id

    full_timeline = timeline_ids["Full incident"]
    await store.create_baseline_definition(
        case_id=case_id,
        timeline_id=full_timeline,
        name="May 2026 — three quiet weeks vs the intrusion",
        baseline_start=scenario.SCENARIO_START,
        baseline_end=scenario.BASELINE_END,
        suspect_windows=metadata.baseline_windows(),
        created_by=user_id,
    )

    for title, yaml_text in metadata.SIGMA_RULES:
        content_hash = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
        parsed, error = parse_rule_yaml(yaml_text)
        if parsed is None:
            raise RuntimeError(f"demo Sigma rule {title!r} does not parse: {error}")
        await store.create_sigma_rule(
            case_id=case_id,
            rule_key=rule_key_for(str(parsed.id) if parsed.id else None, content_hash),
            title=title,
            yaml_content=yaml_text,
            content_hash=content_hash,
            rule_uuid=str(parsed.id) if parsed.id else None,
            level=str(parsed.level.name).lower() if parsed.level else None,
            logsource={"product": "windows", "service": "security"},
            created_by=user_id,
        )

    story = await store.create_story(
        case_id=case_id,
        story_id=generate_id("demo-story"),
        title=metadata.STORY_TITLE,
        description="How the intrusion unfolded, and what is not part of it.",
        user=user_id,
    )
    story_blocks = await asyncio.to_thread(
        metadata.resolve_story_blocks,
        clickhouse,
        case_id,
        source_ids,
        view_ids,
        chart_ids,
        timeline_ids,
    )
    for index, (kind, content) in enumerate(story_blocks):
        # Same two gates every write path applies (shape, then referent scope),
        # so a demo block that would render as "cannot be drawn" fails the
        # seed here instead of shipping broken.
        validated = validate_block_content(kind, content)
        await validate_block_scope(case_id, kind, validated, store=store)
        await store.create_story_block(
            story_id=story.id,
            block_id=generate_id(f"demo-block-{index:03d}"),
            kind=kind,
            content=validated,
            user=user_id,
        )
    return len(resolved)


@dataclass(frozen=True)
class DemoBuildResult:
    """What one seeded demo case ended up containing."""

    case_id: str
    events: int
    sources: int
    annotations: int


async def build_demo_case(
    store: PostgresStore,
    clickhouse: ClickHouseStore,
    owner_id: str,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> DemoBuildResult:
    """Create the demo case for ``owner_id`` and fill it.

    Args:
        store: The live metadata store.
        clickhouse: The live event store.
        owner_id: Who owns the resulting case.
        progress: Optional job-progress callback, called with the same
            ``{"phase", "processed", "total"}`` shape the transfer jobs use.

    Returns:
        The created case's id and its contents.

    On any failure the partially-built case is removed — Postgres rows cascade
    from the case, and each ingested source's ClickHouse partition is dropped
    explicitly — so a failed seed never leaves a half-populated case in
    someone's list.
    """

    def _phase(name: str, total: int | None = None) -> None:
        if progress:
            progress({"phase": name, "processed": 0, "total": total})

    work = Path(tempfile.mkdtemp(prefix="vestigo-demo-"))
    case_id = generate_id("demo-case")
    ingested: dict[str, str] = {}
    try:
        clickhouse.init_schema()
        _phase("generate", len(SOURCES))
        paths = await asyncio.to_thread(_generate_sources, work)

        await store.create_case(
            case_id=case_id,
            name=CASE_NAME,
            description=CASE_DESCRIPTION,
            owner_id=owner_id,
            is_demo=True,
        )
        _phase("ingest", len(SOURCES))
        ingested = await _ingest(store, clickhouse, case_id, owner_id, paths, progress)
        timeline_ids = await _timelines(store, case_id, ingested)
        _phase("columns")
        await _suggest_columns(store, clickhouse, case_id)

        _phase("annotate")
        annotations = await _artifacts(store, clickhouse, case_id, owner_id, ingested, timeline_ids)
        events = sum(source.event_count for source in await store.list_sources(case_id))
        return DemoBuildResult(
            case_id=case_id,
            events=events,
            sources=len(ingested),
            annotations=annotations,
        )
    except BaseException:
        # BaseException, not Exception: a seed cancelled at shutdown raises
        # CancelledError, and that is exactly the case where a half-populated
        # case would otherwise survive into the next boot.
        for source_id in ingested.values():
            try:
                clickhouse.delete_source_events(case_id, source_id)
            except Exception:  # cleanup must not mask the real error
                logger.exception("demo cleanup: could not drop events for %s", source_id)
        try:
            await store.delete_case(case_id)
        except Exception:  # same
            logger.exception("demo cleanup: could not delete case %s", case_id)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
