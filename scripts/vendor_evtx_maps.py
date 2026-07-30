#!/usr/bin/env python3
"""Vendor the EvtxECmd map corpus into ``evtx2vestigo.py`` as a compressed blob.

Reads a local checkout of https://github.com/EricZimmerman/evtx (MIT) and compiles
``evtx/Maps/*.map`` — the community-maintained per-(Channel, Provider, EventId) field
extraction rules — into a single compact JSON document, zlib-compressed and base64-encoded
into the generated region of ``src/vestigo/assets/converters/evtx2vestigo.py``.

The converter is a *single-file script* an analyst downloads from the web UI, so the maps
cannot ship as 468 loose YAML files and the converter cannot depend on PyYAML at runtime:
all YAML parsing, validation and regex compilation happens here, at vendor time. Whatever
the converter ships with is known-good.

The outputs are committed. Re-run this script to re-sync with upstream (the checkout path
may also come from ``$EVTX_UPSTREAM``):

    uv run python scripts/vendor_evtx_maps.py --upstream /path/to/EricZimmerman-evtx

``--check`` recompiles and exits non-zero if the committed blob or the manifest hash has
drifted, without writing anything.

``--manifest-only`` skips the corpus compile and refreshes just ``manifest.json``'s
size/sha256 for the converter — what an edit *outside* the generated map region needs, and
the only mode that runs without an upstream checkout. Combine with ``--check`` to assert
the manifest is current.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "src" / "vestigo" / "assets" / "converters"
CONVERTER_PATH = ASSETS_DIR / "evtx2vestigo.py"
MANIFEST_PATH = ASSETS_DIR / "manifest.json"
UPSTREAM_URL = "https://github.com/EricZimmerman/evtx"

BEGIN_MARK = "# --- BEGIN GENERATED EVTXECMD MAPS (scripts/vendor_evtx_maps.py) ---"
END_MARK = "# --- END GENERATED EVTXECMD MAPS ---"

# The XPath dialect the maps actually use. Every ``Value`` expression in the corpus is
# matched against these; anything else is dropped at vendor time so the converter never
# needs a runtime fallback branch. Keep in sync with ``_resolve_xpath`` in the converter.
_GRAMMAR = (
    re.compile(r'^/Event/EventData/Data\[@Name="(?P<name>[^"]+)"\]$'),
    re.compile(r"^/Event/EventData/Data$"),
    re.compile(r"^/Event/EventData/Data\[(?P<index>\d+)\]$"),
    re.compile(r"^/Event/EventData$"),
    re.compile(r"^/Event/System/(?P<elem>[\w.-]+)/@(?P<attr>[\w.-]+)$"),
    re.compile(r"^/Event/System/(?P<selem>[\w.-]+)$"),
    re.compile(r"^/Event/UserData(?P<path>(?:/[\w.-]+)+)$"),
)

# EvtxECmd's own column order; the converter renders `message` in it.
_KNOWN_PROPERTIES = (
    "UserName",
    "RemoteHost",
    "ExecutableInfo",
    "PayloadData1",
    "PayloadData2",
    "PayloadData3",
    "PayloadData4",
    "PayloadData5",
    "PayloadData6",
)


class Stats:
    """Counters reported at the end of a vendor run."""

    def __init__(self) -> None:
        self.maps = 0
        self.skipped_maps = 0
        self.dropped_values = 0
        self.dropped_refines = 0
        self.dropped_entries = 0
        self.description_only = 0
        self.alias_keys = 0
        self.duplicate_keys = 0


def _upstream_commit(upstream: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _map_key(channel: str, provider: str, event_id: Any) -> str:
    return f"{str(channel).strip().lower()}|{str(provider).strip().lower()}|{event_id}"


def _compile_value(entry: dict[str, Any], stats: Stats) -> dict[str, Any] | None:
    """Validate one ``Values[]`` entry, returning its compact form or None to drop it."""
    expr = str(entry.get("Value", "")).strip()
    if not any(rx.match(expr) for rx in _GRAMMAR):
        stats.dropped_values += 1
        return None
    compact: dict[str, Any] = {"n": str(entry.get("Name", "")), "e": expr}
    refine = entry.get("Refine")
    if refine:
        try:
            re.compile(str(refine))
        except re.error:
            # A handful of upstream refines use .NET-only constructs (variable-width
            # lookbehind, unbalanced groups). Drop the refine, keep the raw value.
            stats.dropped_refines += 1
        else:
            compact["r"] = str(refine)
    return compact


def _compile_map(path: Path, stats: Stats) -> tuple[str, dict[str, Any]] | None:
    """Compile one ``.map`` file into its ``(key, compact document)`` pair."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(doc, dict) or "EventId" not in doc:
        stats.skipped_maps += 1
        return None

    entries: list[dict[str, Any]] = []
    for raw in doc.get("Maps") or []:
        values = [v for v in (raw.get("Values") or []) if isinstance(v, dict)]
        compiled = [c for c in (_compile_value(v, stats) for v in values) if c is not None]
        if not compiled:
            stats.dropped_entries += 1
            continue
        # Four maps spell the property `Username`; EvtxECmd treats it as `UserName`.
        prop = str(raw.get("Property", "")).strip()
        prop = "UserName" if prop.lower() == "username" else prop
        entries.append({"p": prop, "t": str(raw.get("PropertyValue", "")), "v": compiled})

    description = str(doc.get("Description", "")).strip()
    if not entries and not description:
        stats.skipped_maps += 1
        return None
    if not entries:
        # Every extraction was dropped, but the map still names the event; keeping it
        # description-only is what lets such records get a real message instead of the
        # generic "EventID N (provider)" fallback.
        stats.description_only += 1

    compact: dict[str, Any] = {"d": description, "m": entries}

    lookups: dict[str, Any] = {}
    for raw in doc.get("Lookups") or []:
        if not isinstance(raw, dict):
            continue
        # A lookup's Name matches the *variable* name in Values[], not the Property.
        name = str(raw.get("Name", "")).strip()
        if not name:
            continue
        # `Values` is a YAML mapping of raw-value -> human text. YAML resolves an
        # unquoted 0xC000005E to an int, so hex and decimal spellings of the same
        # status code already collapse to one key here; the converter normalizes a
        # record's raw value the same way before looking it up, so only the one
        # canonical spelling needs shipping.
        values = raw.get("Values")
        if not isinstance(values, dict):
            continue
        table = {str(key): str(text) for key, text in values.items()}
        lookups[name] = {"D": str(raw.get("Default", "")), "V": table}
    if lookups:
        compact["l"] = lookups

    stats.maps += 1
    return _map_key(doc.get("Channel", ""), doc.get("Provider", ""), doc.get("EventId")), compact


def compile_corpus(upstream: Path) -> tuple[dict[str, Any], Stats]:
    """Compile ``<upstream>/evtx/Maps/*.map`` into the document the converter embeds."""
    maps_dir = upstream / "evtx" / "Maps"
    if not maps_dir.is_dir():
        raise SystemExit(f"error: no evtx/Maps directory under {upstream}")

    stats = Stats()
    compiled: dict[str, dict[str, Any]] = {}
    for path in sorted(p for p in maps_dir.iterdir() if p.suffix.lower() == ".map"):
        result = _compile_map(path, stats)
        if result is None:
            continue
        key, doc = result
        if key in compiled:
            # Two .map files claiming the same (channel, provider, event id). The first
            # wins, but never silently — upstream adding a competing map is worth seeing.
            stats.duplicate_keys += 1
            print(f"warning: duplicate map key {key} from {path.name}", file=sys.stderr)
            continue
        compiled[key] = doc

    # Provider-agnostic tier: only for (channel, eventid) pairs claimed by exactly one
    # provider, so a renamed or differently-cased provider still resolves. Ambiguous
    # pairs are deliberately left without an alias rather than picking a winner.
    by_pair: dict[tuple[str, str], list[str]] = {}
    for key in compiled:
        channel, _provider, event_id = key.split("|", 2)
        by_pair.setdefault((channel, event_id), []).append(key)
    aliases = {
        f"{channel}|*|{event_id}": keys[0]
        for (channel, event_id), keys in by_pair.items()
        if len(keys) == 1
    }
    stats.alias_keys = len(aliases)

    document = {
        "_meta": {
            "repo": UPSTREAM_URL,
            "commit": _upstream_commit(upstream),
            "map_count": len(compiled),
            "alias_count": len(aliases),
            "dropped_values": stats.dropped_values,
            "dropped_refines": stats.dropped_refines,
            "dropped_entries": stats.dropped_entries,
            "properties": list(_KNOWN_PROPERTIES),
        },
        "maps": compiled,
        "aliases": aliases,
    }
    return document, stats


def render_blob(document: dict[str, Any]) -> str:
    """Serialize to the wrapped base64 literal the converter embeds.

    Deterministic by construction — no timestamps, sorted keys — so re-running against the
    same upstream commit reproduces the file byte for byte and the manifest hash is stable.
    """
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    packed = base64.b64encode(zlib.compress(payload.encode("utf-8"), 9)).decode("ascii")
    lines = [packed[i : i + 76] for i in range(0, len(packed), 76)]
    return "\n".join(lines)


def render_region(document: dict[str, Any]) -> str:
    """Render the full generated region, markers included."""
    meta = document["_meta"]
    blob = render_blob(document)
    return (
        f"{BEGIN_MARK}\n"
        f"# EvtxECmd maps vendored from {meta['repo']} (MIT, Copyright (c) 2019 Eric\n"
        f"# Zimmerman). {meta['map_count']} maps + {meta['alias_count']} provider-agnostic\n"
        f"# aliases, compiled at commit {meta['commit']}.\n"
        f"# Do not edit by hand — re-run scripts/vendor_evtx_maps.py to re-sync.\n"
        f'MAPS_SOURCE_COMMIT = "{meta["commit"]}"\n'
        f'_MAPS_BLOB = """\\\n{blob}\n"""\n'
        f"{END_MARK}"
    )


def _splice(text: str, region: str) -> str:
    start = text.find(BEGIN_MARK)
    end = text.find(END_MARK)
    if start < 0 or end < 0:
        raise SystemExit(
            f"error: {CONVERTER_PATH.name} is missing the generated-region markers "
            f"({BEGIN_MARK!r} ... {END_MARK!r})"
        )
    return text[:start] + region + text[end + len(END_MARK) :]


def _refresh_manifest(check: bool) -> bool:
    """Refresh evtx2vestigo's size/sha256 in the manifest. Returns True if it changed."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    content = CONVERTER_PATH.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    for entry in manifest["converters"]:
        if entry["name"] != "evtx2vestigo":
            continue
        if entry.get("size_bytes") == len(content) and entry.get("sha256") == digest:
            return False
        if not check:
            entry["size_bytes"] = len(content)
            entry["sha256"] = digest
            MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return True
    raise SystemExit("error: manifest.json has no evtx2vestigo entry — add it first")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--upstream",
        type=Path,
        default=os.environ.get("EVTX_UPSTREAM"),
        required="EVTX_UPSTREAM" not in os.environ and "--manifest-only" not in sys.argv,
        help="path to a local EricZimmerman/evtx checkout (default: $EVTX_UPSTREAM)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed blob and manifest hash are in sync; write nothing",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="refresh only manifest.json's size/sha256 for evtx2vestigo (no upstream "
        "checkout needed) — for edits to the script outside the generated map region",
    )
    args = parser.parse_args()

    if args.manifest_only:
        changed = _refresh_manifest(check=args.check)
        if args.check:
            if changed:
                print("drift: manifest.json size/sha256 is stale", file=sys.stderr)
                return 1
            print("ok: manifest is in sync")
            return 0
        print("manifest refreshed" if changed else "manifest already in sync")
        return 0

    upstream = args.upstream.expanduser().resolve()
    if not upstream.is_dir():
        raise SystemExit(f"error: upstream checkout not found: {upstream}")

    document, stats = compile_corpus(upstream)
    region = render_region(document)
    current = CONVERTER_PATH.read_text(encoding="utf-8")
    updated = _splice(current, region)

    if args.check:
        drifted = updated != current
        manifest_drifted = _refresh_manifest(check=True)
        if drifted:
            print("drift: the embedded map blob differs from the committed one", file=sys.stderr)
        if manifest_drifted:
            print("drift: manifest.json size/sha256 is stale", file=sys.stderr)
        if drifted or manifest_drifted:
            print("run: uv run python scripts/vendor_evtx_maps.py", file=sys.stderr)
            return 1
        print("ok: embedded maps and manifest are in sync")
        return 0

    if updated != current:
        CONVERTER_PATH.write_text(updated, encoding="utf-8")
    _refresh_manifest(check=False)

    blob_bytes = len(render_blob(document).encode("ascii"))
    print(
        f"vendored {stats.maps} maps (+{stats.alias_keys} aliases) from {upstream}\n"
        f"  dropped: {stats.dropped_values} values, {stats.dropped_refines} refines, "
        f"{stats.dropped_entries} entries, {stats.skipped_maps} maps "
        f"({stats.description_only} kept description-only), "
        f"{stats.duplicate_keys} duplicate keys\n"
        f"  blob: {blob_bytes:,} B base64  |  {CONVERTER_PATH.name}: "
        f"{CONVERTER_PATH.stat().st_size:,} B"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
