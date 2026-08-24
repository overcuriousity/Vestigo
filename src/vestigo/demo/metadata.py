"""What the analyst left behind: notes, tags, views, a baseline, and a story.

None of this names a detector or explains the product. It reads as one
investigator's working notes on a real-looking intrusion, because a demo case
that narrates its own tooling teaches the tooling instead of the work.

Every annotation is anchored to an actual ingested event: the selectors here
are resolved against ClickHouse at build time, so a note about the spray hangs
off a spray record rather than off a row number nobody can check.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from vestigo.db.postgres import generate_id
from vestigo.demo import scenario
from vestigo.demo.sources import netflow, proxy, windows

#: The closed tag vocabulary. A demo with fifty ad-hoc tags teaches nothing
#: about how tags are meant to be used.
TAGS = (
    "initial-access",
    "persistence",
    "lateral-movement",
    "exfil",
    "to-verify",
    "benign-explained",
)


@dataclass(frozen=True)
class DemoAnnotation:
    """One note, plus how to find the event it belongs on.

    Attributes:
        source_key: Which source file the event came from.
        note: The analyst's text.
        tags: Tags to apply, from ``TAGS``.
        after: Only consider events at or after this instant.
        attribute: Attribute key to match on, if any.
        value: Exact value ``attribute`` must equal.
        contains: Substring to require. Applies to ``attribute`` when one is
            named, and to the message otherwise — a Windows command line lives
            in an attribute, a syslog line lives in the message.
    """

    source_key: str
    note: str
    tags: tuple[str, ...]
    after: datetime
    attribute: str | None = None
    value: str | None = None
    contains: str | None = None


_RECON, _FOOTHOLD, _LATERAL, _EXFIL = scenario.PHASES
_START = scenario.SCENARIO_START

ANNOTATIONS: tuple[DemoAnnotation, ...] = (
    DemoAnnotation(
        "windows",
        "Failed logons against DC-01 climb from a handful an hour to hundreds a minute, "
        "walking the whole account list alphabetically. Automated, not a person mistyping.",
        ("initial-access",),
        _RECON.start,
        attribute="event_id",
        value="4625",
    ),
    DemoAnnotation(
        "windows",
        "Every account in the directory is tried, including the two service accounts. "
        "Whoever ran this had a user list, so treat the directory dump as already done.",
        ("initial-access", "to-verify"),
        _RECON.start + timedelta(minutes=6),
        attribute="event_id",
        value="4625",
    ),
    DemoAnnotation(
        "windows",
        "m.okonkwo is the only success. Contractor account — check whether that password "
        "was reused from the supplier portal breach we were notified about in March.",
        ("initial-access",),
        _RECON.start + timedelta(hours=2),
        attribute="event_id",
        value="4624",
    ),
    DemoAnnotation(
        "windows",
        "First interactive logon for this account on JUMP-01. Their contract covers the "
        "finance file share only; nothing in it explains a jump host session.",
        ("initial-access", "to-verify"),
        _FOOTHOLD.start,
        attribute="computer_name",
        value=scenario.JUMP_HOST,
    ),
    DemoAnnotation(
        "windows",
        "Base64 in the command line, hidden window, no profile. Decoded it starts a "
        "download from an internal-looking address. Grab the full string before the "
        "host gets rebuilt.",
        ("initial-access", "persistence"),
        _FOOTHOLD.start,
        attribute="command_line",
        contains="-enc ",
    ),
    DemoAnnotation(
        "windows",
        "Service installed three minutes after that PowerShell ran. Name mimics a "
        "Windows health service but does not exist on any other host in the estate.",
        ("persistence",),
        _FOOTHOLD.start,
        attribute="service_name",
        value=windows.PERSISTENCE_SERVICE,
    ),
    DemoAnnotation(
        "windows",
        "Same service name appears on FILE-01 two days later. Same operator, same "
        "playbook — this is the persistence mechanism to hunt for estate-wide.",
        ("persistence", "lateral-movement"),
        _LATERAL.start,
        attribute="service_name",
        value=windows.PERSISTENCE_SERVICE,
    ),
    DemoAnnotation(
        "windows",
        "wmic remote process creation against FILE-01. Nobody in this environment "
        "administers anything that way; the platform team uses Ansible.",
        ("lateral-movement",),
        _LATERAL.start,
        attribute="command_line",
        contains="wmic /node:",
    ),
    DemoAnnotation(
        "windows",
        "The contractor account now logs on to hosts it has never touched in the "
        "months we have logs for. WKS-007 and WKS-009 belong to the finance team.",
        ("lateral-movement",),
        _LATERAL.start,
        attribute="computer_name",
        value="WKS-007",
    ),
    DemoAnnotation(
        "windows",
        "7-Zip run against the finance share. Look closely at the archive name — the "
        "'o' in 'report' is Cyrillic. Copy-pasted from somewhere, and it means the "
        "filename will not match a naive string search.",
        ("exfil", "to-verify"),
        _EXFIL.start,
        attribute="command_line",
        contains="7z.exe a",
    ),
    DemoAnnotation(
        "linux",
        "sshd on FILE-01 accepts a password for m.okonkwo. Password auth is supposed "
        "to be off on that host — the config drifted, and that is how they got in.",
        ("lateral-movement", "to-verify"),
        _LATERAL.start,
        contains="Accepted password for",
    ),
    DemoAnnotation(
        "linux",
        "sudo on FILE-01 shifts from routine service checks to tar, find and rsync "
        "over the finance share. That is someone collecting, not administering.",
        ("exfil",),
        _FOOTHOLD.start,
        contains="tar -czf",
    ),
    DemoAnnotation(
        "linux",
        "An 'unattended-upgrade' line that does not match the real unattended-upgrades "
        "format, installing a package called telemetry-agent from a local file. "
        "Nothing else in the month logs this shape.",
        ("persistence",),
        _LATERAL.start,
        contains="unattended-upgrade shim",
    ),
    DemoAnnotation(
        "linux",
        "APP-01's clock drifts against NTP and chronyd corrects it repeatedly, so its "
        "records land slightly out of order all month. Benign — but worth knowing "
        "before anyone builds a timeline argument on this host.",
        ("benign-explained",),
        _START + timedelta(days=2),
        contains="System clock wrong by",
    ),
    DemoAnnotation(
        "linux",
        "The nightly backup moves from 02:15 to 03:40 on the 25th. Platform team "
        "confirmed the change window — unrelated to the intrusion, do not chase it.",
        ("benign-explained",),
        _FOOTHOLD.start,
        contains="nightly backup run started",
    ),
    DemoAnnotation(
        "proxy",
        "Requests to this host land every five minutes almost to the second, from "
        "JUMP-01 only, with a tiny fixed payload. Machines are punctual; people are not.",
        ("persistence",),
        _FOOTHOLD.start,
        attribute="host",
        value=scenario.C2_HOST,
    ),
    DemoAnnotation(
        "proxy",
        "The hostname is sixteen random characters under a plausible-looking domain. "
        "Registered eleven days ago per the passive DNS lookup.",
        ("persistence", "to-verify"),
        _FOOTHOLD.start + timedelta(hours=6),
        attribute="host",
        value=scenario.C2_HOST,
    ),
    DemoAnnotation(
        "proxy",
        "A user agent nobody else in the estate sends, carrying guillemets — a "
        "copy-paste artifact from whatever tooling built it. It only ever appears on "
        "this operator's traffic, which makes it a usable pivot.",
        ("persistence", "exfil"),
        _FOOTHOLD.start,
        attribute="user_agent",
        value=proxy.ODD_USER_AGENT,
    ),
    DemoAnnotation(
        "proxy",
        "Uploads to a file-sharing host in tens of megabytes per request, hundreds of "
        "requests, overnight. Baseline outbound for this population is under 10 KB.",
        ("exfil",),
        _EXFIL.start,
        attribute="host",
        value=scenario.EXFIL_HOST,
    ),
    DemoAnnotation(
        "proxy",
        "Chunked and paced — small enough per request to stay under a per-request cap, "
        "steady enough to finish before the morning. They expected us to be watching "
        "single large transfers.",
        ("exfil",),
        _EXFIL.start + timedelta(hours=3),
        attribute="host",
        value=scenario.EXFIL_HOST,
    ),
    DemoAnnotation(
        "netflow",
        "Firewall allows the five-minute callback every time. It is 443 to a host that "
        "resolves fine, so nothing in the ruleset was ever going to stop it.",
        ("persistence",),
        _FOOTHOLD.start,
        attribute="dst_ip",
        value=netflow.C2_IP,
    ),
    DemoAnnotation(
        "netflow",
        "Burst of denied SMB from JUMP-01 to FILE-01 — share enumeration that mostly "
        "failed. The handful that succeeded is where they found the finance share.",
        ("lateral-movement",),
        _LATERAL.start,
        attribute="action",
        value="deny",
    ),
    DemoAnnotation(
        "netflow",
        "Long sessions to 45.153.160.204 that line up one-to-one with the proxy "
        "uploads. Two sources, same story — good enough to put in the report.",
        ("exfil",),
        _EXFIL.start,
        attribute="dst_ip",
        value=netflow.EXFIL_IP,
    ),
    DemoAnnotation(
        "netflow",
        "Nothing before the 24th looks like any of this. Whatever else is true, the "
        "operator was not in the environment during the first three weeks.",
        ("to-verify",),
        _START + timedelta(days=3),
        attribute="action",
        value="allow",
    ),
    DemoAnnotation(
        "windows",
        "Scoping note: the account was disabled at 08:10 on the 30th and JUMP-01 and "
        "FILE-01 were isolated. Everything above predates containment.",
        ("to-verify",),
        _EXFIL.start,
        attribute="event_id",
        value="4624",
    ),
)


@dataclass(frozen=True)
class DemoView:
    """A saved filter set, in the payload shape the Explorer writes."""

    name: str
    query: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def _view_payload(**overrides: Any) -> dict[str, Any]:
    """Every key the frontend writes, so a demo View round-trips like a real one."""
    base: dict[str, Any] = {
        "q": None,
        "qMode": None,
        "qRegex": False,
        "artifact": None,
        "artifacts": [],
        "sourceId": None,
        "tag": None,
        "excludeTag": None,
        "tagsInclude": [],
        "tagsExclude": [],
        "start": None,
        "end": None,
        "filters": {},
        "exclusions": {},
        "filterModes": {},
        "exclusionModes": {},
        "annotated": [],
        "annotationTagValue": None,
    }
    base.update(overrides)
    return base


VIEWS: tuple[DemoView, ...] = (
    DemoView("Failed logons, all hosts", payload=_view_payload(filters={"event_id": ["4625"]})),
    DemoView(
        "Contractor account activity",
        payload=_view_payload(filters={"user": [scenario.COMPROMISED_USER]}),
    ),
    DemoView(
        "Service installs",
        payload=_view_payload(filters={"event_id": ["7045"]}),
    ),
    DemoView(
        "Callbacks and uploads",
        payload=_view_payload(filters={"host": [scenario.C2_HOST, scenario.EXFIL_HOST]}),
    ),
    DemoView(
        "File server sudo",
        query="sudo",
        payload=_view_payload(q="sudo", filters={"hostname": [scenario.FILE_SERVER]}),
    ),
)


@dataclass(frozen=True)
class DemoChart:
    """A saved Visualize-page chart, in the config shape the frontend writes.

    ``config`` is a full versioned ``ChartConfig`` (camelCase, ``v: 1``) —
    the backend round-trips it verbatim, so writing one here means writing
    exactly what the Visualize page would have saved. ``tests/test_demo.py``
    parses every one of these through the export path's
    ``_stored_chart_to_spec``, because a config in the wrong shape is
    silently undrawable rather than an error.
    """

    name: str
    timeline: str
    config: dict[str, Any]


def _chart_config(
    chart_type: str,
    scale: str,
    *,
    field: str | None = None,
    field_y: str | None = None,
    metric: str = "count",
    options: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A stored ``ChartConfig``, every key the frontend writes present."""
    return {
        "v": 1,
        "field": field,
        "fieldY": field_y,
        "fields": None,
        "scale": scale,
        "chartType": chart_type,
        "metric": metric,
        "compare": {"mode": "off"},
        "options": options or {},
        **({"filters": filters} if filters else {}),
    }


#: The charts the analyst saved while working the case. They exist so the
#: Visualization page is populated on a first login, and so the story has
#: something real to embed — a ``chart_ref`` block re-runs the saved config
#: at export time, so these are live queries, not pictures.
CHARTS: tuple[DemoChart, ...] = (
    DemoChart(
        "Failed logons over the month",
        "Endpoint",
        _chart_config(
            "time",
            "interval",
            options={"buckets": 120},
            filters=_view_payload(filters={"event_id": ["4625"]}),
        ),
    ),
    DemoChart(
        "Where the traffic goes",
        "Network",
        _chart_config("bar", "nominal", field="host", options={"topN": 12, "sort": "count"}),
    ),
    DemoChart(
        "Upload sizes",
        "Network",
        _chart_config("histogram", "ratio", field="bytes_out", options={"bins": 40}),
    ),
    DemoChart(
        "Activity by hour and weekday",
        "Full incident",
        _chart_config("punchcard", "nominal"),
    ),
)


SIGMA_RULES: tuple[tuple[str, str], ...] = (
    (
        "Encoded PowerShell command line",
        """
title: Encoded PowerShell command line
id: 8f1c0a92-3d5e-4b77-9a41-1c0d2f6b5e10
status: experimental
description: Base64-encoded PowerShell hides what is actually being run.
logsource:
  product: windows
  service: security
detection:
  selection:
    event_id: '4688'
    command_line|contains: ' -enc '
  condition: selection
level: high
""".strip(),
    ),
    (
        "Suspicious service installation",
        """
title: Suspicious service installation
id: 2b7d5f10-6e28-4c93-8f0a-77b1c4a9d332
status: experimental
description: A service install whose name imitates a Windows component.
logsource:
  product: windows
  service: security
detection:
  selection:
    event_id: '7045'
    service_name|contains: 'WinSysHealth'
  condition: selection
level: high
""".strip(),
    ),
    (
        "Remote process creation via wmic",
        """
title: Remote process creation via wmic
id: c4a0e6b1-9f32-4d15-b7e8-5a2c1d904f6b
status: experimental
description: "wmic /node: creates a process on another host — classic lateral movement."
logsource:
  product: windows
  service: security
detection:
  selection:
    command_line|contains: 'wmic /node:'
  condition: selection
level: high
""".strip(),
    ),
    (
        "Failed logon burst against a domain controller",
        """
title: Failed logon burst against a domain controller
id: 5d9b2c74-1a86-4e50-9c3f-8b6e0a7d2145
status: experimental
description: Repeated 4625s on a DC, the shape a password spray leaves behind.
logsource:
  product: windows
  service: security
detection:
  selection:
    event_id: '4625'
    computer_name: 'DC-01'
  condition: selection
level: medium
""".strip(),
    ),
)


@dataclass(frozen=True)
class DemoEventPick:
    """How to find the one event a story block freezes.

    Same selector vocabulary as ``DemoAnnotation`` and resolved by the same
    query — an ``event_ref`` block that pointed at a row number nobody can
    check would undercut the point of the block.
    """

    source_key: str
    after: datetime
    attribute: str | None = None
    value: str | None = None
    contains: str | None = None


@dataclass(frozen=True)
class DemoBlock:
    """One story block, with its referents named rather than by id.

    Ids for views, charts and timelines only exist once the case is built, so
    a block names them and ``resolve_story_blocks`` swaps in the real ids.

    Attributes:
        kind: One of ``stories.schemas.BLOCK_KINDS``.
        text: Markdown body, for ``kind="markdown"``.
        view: A ``VIEWS`` entry's name, for ``kind="view_ref"``.
        chart: A ``CHARTS`` entry's name, for ``kind="chart_ref"``.
        event: The selector, for ``kind="event_ref"``.
        caption: One-line label under a frozen event.
        timeline: Which timeline the embed resolves against, by name.
        limit: Row cap for a view block.
    """

    kind: str
    text: str | None = None
    view: str | None = None
    chart: str | None = None
    event: DemoEventPick | None = None
    caption: str | None = None
    timeline: str = "Full incident"
    limit: int = 25


#: The story. Headings are markdown ``##`` lines rather than a block kind of
#: their own — see stories/schemas.py. The narrative deliberately uses all
#: four block kinds: prose carries the argument, and each claim that rests on
#: evidence is followed by the evidence itself — the event that shows it, the
#: filter set that finds the rest, or the chart that shows its shape.
STORY_TITLE = "Contractor account compromise — 24–30 May"
STORY_BLOCKS: tuple[DemoBlock, ...] = (
    DemoBlock(
        "markdown",
        text="## Contractor account compromise — 24–30 May",
    ),
    DemoBlock(
        "markdown",
        text="A contractor account was compromised by password spraying, used to establish a "
        "foothold on the jump host, and then to stage and upload roughly 6 GB from the "
        "finance file share. Containment happened on the morning of the 30th.\n\n"
        "The first three weeks of May are included deliberately: nothing in them looks "
        "like this, and being able to say that with evidence is half the report.",
    ),
    DemoBlock(
        "chart_ref",
        chart="Failed logons over the month",
        timeline="Endpoint",
    ),
    DemoBlock(
        "markdown",
        text="Three flat weeks and then a wall on the 24th. Everything below is an "
        "attempt to account for that shape.",
    ),
    DemoBlock(
        "markdown",
        text="## 24 May — credential spray",
    ),
    DemoBlock(
        "markdown",
        text="Just after 02:00, DC-01 logs several thousand failed logons in under an hour, "
        "cycling every account in the directory from a workstation on the contractor "
        "VPN pool. One account succeeds: `m.okonkwo`.\n\n"
        "The account list argues for prior reconnaissance. The single success argues "
        "for a reused password rather than a guessed one.",
    ),
    DemoBlock(
        "event_ref",
        event=DemoEventPick(
            "windows",
            _RECON.start,
            attribute="event_id",
            value="4625",
        ),
        caption="The first failed logon of the spray.",
    ),
    DemoBlock(
        "view_ref",
        view="Failed logons, all hosts",
        timeline="Endpoint",
        limit=25,
    ),
    DemoBlock(
        "markdown",
        text="## 25 May — foothold and persistence",
    ),
    DemoBlock(
        "markdown",
        text="The account logs on interactively to JUMP-01, runs a base64-encoded PowerShell "
        "command, and three minutes later a service named `WinSysHealthSvc` is "
        "installed. That name appears nowhere else in the estate before this moment.\n\n"
        "From here on, JUMP-01 calls out to a sixteen-character hostname every five "
        "minutes, to the second.",
    ),
    DemoBlock(
        "event_ref",
        event=DemoEventPick(
            "windows",
            _FOOTHOLD.start,
            attribute="command_line",
            contains="-enc ",
        ),
        caption="The encoded PowerShell command that preceded the service install.",
    ),
    DemoBlock(
        "event_ref",
        event=DemoEventPick(
            "windows",
            _FOOTHOLD.start,
            attribute="service_name",
            value=windows.PERSISTENCE_SERVICE,
        ),
        caption="The persistence service, three minutes later.",
    ),
    DemoBlock(
        "view_ref",
        view="Service installs",
        timeline="Endpoint",
        limit=25,
    ),
    DemoBlock(
        "markdown",
        text="## 27–28 May — lateral movement",
    ),
    DemoBlock(
        "markdown",
        text="SMB enumeration from JUMP-01 against FILE-01 is mostly denied, but enough "
        "succeeds to locate the finance share. `wmic /node:` creates processes "
        "remotely, and the same persistence service is installed on FILE-01.\n\n"
        "The contractor account also appears on WKS-007 and WKS-009 — finance "
        "workstations it has no history with.",
    ),
    DemoBlock(
        "view_ref",
        view="Contractor account activity",
        timeline="Full incident",
        limit=50,
    ),
    DemoBlock(
        "markdown",
        text="## 29–30 May — staging and exfiltration",
    ),
    DemoBlock(
        "markdown",
        text="7-Zip archives the finance share into `Q4_repоrt_archive.7z` — note the "
        "Cyrillic character in the filename. The archive leaves in a few hundred "
        "chunked uploads to a file-sharing host overnight, paced to look unremarkable.",
    ),
    DemoBlock(
        "chart_ref",
        chart="Where the traffic goes",
        timeline="Network",
    ),
    DemoBlock(
        "chart_ref",
        chart="Upload sizes",
        timeline="Network",
    ),
    DemoBlock(
        "markdown",
        text="Each upload is unremarkable on its own; the distribution is what gives the "
        "transfer away, and the destination has no business relationship behind it.",
    ),
    DemoBlock(
        "event_ref",
        event=DemoEventPick(
            "proxy",
            _EXFIL.start,
            attribute="host",
            value=scenario.EXFIL_HOST,
        ),
        caption="The first upload chunk to the file-sharing host.",
    ),
    DemoBlock(
        "view_ref",
        view="Callbacks and uploads",
        timeline="Network",
        limit=50,
    ),
    DemoBlock(
        "markdown",
        text="## What is not part of this",
    ),
    DemoBlock(
        "markdown",
        text="Two things in the same window are unrelated and worth stating so nobody "
        "re-investigates them:\n\n"
        "- APP-01's clock drifts against NTP, so its records land slightly out of "
        "order. It has done this all month.\n"
        "- The nightly backup moved from 02:15 to 03:40 on the 25th, in an approved "
        "change window.",
    ),
    DemoBlock(
        "chart_ref",
        chart="Activity by hour and weekday",
        timeline="Full incident",
    ),
    DemoBlock(
        "markdown",
        text="Office hours dominate the whole estate, which is why the 02:00 and "
        "overnight blocks above stand out at all.",
    ),
    DemoBlock(
        "markdown",
        text="## Recommendations",
    ),
    DemoBlock(
        "markdown",
        text="1. Force a password reset for every contractor account and disable password "
        "auth on FILE-01, which had drifted from the standard.\n"
        "2. Hunt estate-wide for `WinSysHealthSvc` and for the guillemet user agent — "
        "both are specific enough to be reliable pivots.\n"
        "3. Treat the finance share contents as disclosed and start the notification "
        "process on that basis.",
    ),
)


def resolve_annotation_events(
    clickhouse: Any, case_id: str, source_ids: dict[str, str]
) -> list[tuple[DemoAnnotation, str, str]]:
    """Pair every annotation with a real event id.

    Args:
        clickhouse: A live ``ClickHouseStore``.
        case_id: The case being built.
        source_ids: Source key (``windows``, ``linux``, ``proxy``, ``netflow``)
            to the created source's id.

    Returns:
        ``(annotation, source_id, event_id)`` for every annotation that matched.

    Raises:
        LookupError: If a selector matches nothing. A note hanging off no event
            is a broken demo, so the build fails rather than shipping it.
    """
    resolved: list[tuple[DemoAnnotation, str, str]] = []
    for annotation in ANNOTATIONS:
        source_id = source_ids[annotation.source_key]
        event_id = first_event_id(
            clickhouse,
            case_id,
            source_id,
            annotation.after,
            attribute=annotation.attribute,
            value=annotation.value,
            contains=annotation.contains,
        )
        if event_id is None:
            raise LookupError(f"demo annotation matched no event: {annotation.note[:60]}…")
        resolved.append((annotation, source_id, event_id))
    return resolved


def first_event_id(
    clickhouse: Any,
    case_id: str,
    source_id: str,
    after: datetime,
    *,
    attribute: str | None = None,
    value: str | None = None,
    contains: str | None = None,
) -> str | None:
    """The earliest event in *source_id* at or after *after* matching the selector.

    Shared by the annotations and the story's ``event_ref`` blocks so both
    anchor the same way. Returns None when nothing matches; each caller
    decides how loud that is.
    """
    conditions = [
        "case_id = %(case_id)s",
        "source_id = %(source_id)s",
        "timestamp >= %(after)s",
    ]
    params: dict[str, Any] = {
        "case_id": case_id,
        "source_id": source_id,
        "after": after.replace(tzinfo=None),
    }
    if attribute is not None:
        params["attribute"] = attribute
        if value is not None:
            conditions.append("attributes[%(attribute)s] = %(value)s")
            params["value"] = value
    if contains is not None:
        target = "attributes[%(attribute)s]" if attribute else "message"
        conditions.append(f"position({target}, %(contains)s) > 0")
        params["contains"] = contains
    sql = (
        f"SELECT toString(event_id) FROM {clickhouse.database}.events"
        f" WHERE {' AND '.join(conditions)} ORDER BY timestamp ASC LIMIT 1"
    )
    rows = clickhouse.client.query(sql, parameters=params).result_rows
    return rows[0][0] if rows else None


def resolve_story_blocks(
    clickhouse: Any,
    case_id: str,
    source_ids: dict[str, str],
    view_ids: dict[str, str],
    chart_ids: dict[str, str],
    timeline_ids: dict[str, str],
) -> list[tuple[str, dict[str, Any]]]:
    """Turn ``STORY_BLOCKS`` into ``(kind, content)`` pairs with real ids.

    Args:
        clickhouse: A live ``ClickHouseStore``.
        case_id: The case being built.
        source_ids: Source key to source id.
        view_ids: View name to view id.
        chart_ids: Chart name to saved-chart id.
        timeline_ids: Timeline name to timeline id.

    Returns:
        One ``(kind, content)`` pair per block, content in the shape
        ``stories.schemas.validate_block_content`` accepts.

    Raises:
        LookupError: If an ``event_ref`` selector matches nothing — a story
            block pointing at no event is a broken demo, same rule as the
            annotations.
        KeyError: If a block names a view, chart or timeline that was not
            created, which can only be a typo in this module.
    """
    blocks: list[tuple[str, dict[str, Any]]] = []
    for block in STORY_BLOCKS:
        if block.kind == "markdown":
            blocks.append(("markdown", {"text": block.text or ""}))
        elif block.kind == "view_ref":
            blocks.append(
                (
                    "view_ref",
                    {
                        "view_id": view_ids[block.view or ""],
                        "timeline_id": timeline_ids[block.timeline],
                        "display": {"limit": block.limit, "columns": None},
                    },
                )
            )
        elif block.kind == "chart_ref":
            blocks.append(
                (
                    "chart_ref",
                    {
                        "chart_id": chart_ids[block.chart or ""],
                        "timeline_id": timeline_ids[block.timeline],
                    },
                )
            )
        elif block.kind == "event_ref":
            pick = block.event
            if pick is None:
                raise ValueError("event_ref demo block carries no selector")
            source_id = source_ids[pick.source_key]
            event_id = first_event_id(
                clickhouse,
                case_id,
                source_id,
                pick.after,
                attribute=pick.attribute,
                value=pick.value,
                contains=pick.contains,
            )
            if event_id is None:
                raise LookupError(f"demo story block matched no event: {block.caption!r}")
            blocks.append(
                (
                    "event_ref",
                    {
                        "event_id": event_id,
                        "source_id": source_id,
                        "caption": block.caption,
                    },
                )
            )
        else:
            raise ValueError(f"unknown demo block kind {block.kind!r}")
    return blocks


def baseline_windows() -> list[dict[str, str]]:
    """The four suspect windows, in the JSON shape the baselines router writes."""
    return [
        {
            "id": f"w{i}",
            "label": phase.label,
            "start": phase.start.isoformat(),
            "end": phase.end.isoformat(),
        }
        for i, phase in enumerate(scenario.PHASES)
    ]


def tag_annotation_rows(
    resolved: Sequence[tuple[DemoAnnotation, str, str]], case_id: str, user_id: str
) -> list[dict[str, Any]]:
    """Expand resolved notes into annotation rows: one comment plus its tags.

    Tags are annotations too (``annotation_type="tag"``), so the demo's tags
    are filterable in the Explorer exactly like an analyst's own.
    """
    rows: list[dict[str, Any]] = []
    for index, (annotation, source_id, event_id) in enumerate(resolved):
        # Ids must be globally unique, not merely unique within this case:
        # every user gets their own copy of the demo, seeded from the same
        # constants, and annotation ids are the primary key.
        rows.append(
            {
                "annotation_id": generate_id(f"demo-note-{index:03d}"),
                "case_id": case_id,
                "source_id": source_id,
                "event_id": event_id,
                "annotation_type": "comment",
                "content": annotation.note,
                "created_by": user_id,
                "origin": "user",
            }
        )
        for tag_index, tag in enumerate(annotation.tags):
            rows.append(
                {
                    "annotation_id": generate_id(f"demo-tag-{index:03d}-{tag_index}"),
                    "case_id": case_id,
                    "source_id": source_id,
                    "event_id": event_id,
                    "annotation_type": "tag",
                    "content": tag,
                    "created_by": user_id,
                    "origin": "user",
                }
            )
    return rows
