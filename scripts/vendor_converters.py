#!/usr/bin/env python3
"""Vendor the 2timesketch converter suite into self-contained single-file scripts.

Reads a local checkout of https://github.com/overcuriousity/2timesketch and, for each
converter, inlines the shared ``timesketch_converters.common`` module, the source-specific
module, and the CLI entry script into one stdlib-only ``.py`` file under
``src/vestigo/assets/converters/``. A ``manifest.json`` with per-file metadata
(description, inputs, upstream commit, sha256) is written alongside; the API serves both.

The outputs are committed. Re-run this script to re-sync with upstream:

    uv run python scripts/vendor_converters.py [--upstream /path/to/2timesketch]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "src" / "vestigo" / "assets" / "converters"
UPSTREAM_URL = "https://github.com/overcuriousity/2timesketch"

# name -> (module basename, entry script, description, supported inputs)
CONVERTERS = {
    "apache2timesketch": (
        "apache",
        "apache2timesketch.py",
        "Apache HTTP Server access (combined/common, other_vhosts) and error logs "
        "(2.4 and 2.2 formats) to Timesketch timeline.",
        ["access.log*", "other_vhosts_access.log*", "error.log*"],
    ),
    "browser2timesketch": (
        "browser",
        "browser2timesketch.py",
        "Browser history (Firefox/Chrome/Chromium/Edge SQLite databases) to Timesketch timeline.",
        ["places.sqlite", "History (Chromium SQLite)"],
    ),
    "cloudtrail2timesketch": (
        "cloudtrail",
        "cloudtrail2timesketch.py",
        "AWS CloudTrail JSON/JSON.gz exports to Timesketch timeline.",
        ["CloudTrail .json / .json.gz"],
    ),
    "cowrie2timesketch": (
        "cowrie",
        "cowrie2timesketch.py",
        "Cowrie SSH/Telnet honeypot JSON logs (plain, rotated, or gzip) to Timesketch timeline.",
        ["cowrie.json", "cowrie.json.YYYY-MM-DD", "cowrie.json*.gz"],
    ),
    "evtx2timesketch": (
        "evtx",
        "evtx2timesketch.py",
        "Windows Event Log text exports (wevtutil XML, evtx_dump XML/JSONL) to "
        "Timesketch timeline.",
        ["wevtutil qe /f:xml export", "evtx_dump --format xml", "evtx_dump -o jsonl"],
    ),
    "filterlog2timesketch": (
        "filterlog",
        "filterlog2timesketch.py",
        "pfSense/OPNsense filterlog firewall logs to Timesketch timeline.",
        ["filter.log (syslog filterlog lines)"],
    ),
    "journal2timesketch": (
        "journal",
        "journal2timesketch.py",
        "systemd journal (via journalctl JSON export) to Timesketch timeline.",
        ["journal directory / journalctl -o json"],
    ),
    "nginx2timesketch": (
        "nginx",
        "nginx2timesketch.py",
        "nginx access/error/redirect logs to Timesketch timeline.",
        ["access.log*", "error.log*"],
    ),
    "pcap2timesketch": (
        "pcap",
        "pcap2timesketch.py",
        "Packet captures (pcap/pcapng) to Timesketch timeline, decoded to "
        "Ethernet/IPv4/IPv6/TCP/UDP/ICMP/ARP headers.",
        ["*.pcap", "*.pcapng"],
    ),
    "syslog2timesketch": (
        "syslog",
        "syslog2timesketch.py",
        "Linux syslog/auth.log (RFC 3164 plain text, rotated or gzip) to Timesketch timeline.",
        ["auth.log*", "secure*", "syslog*", "messages*", "cron.log*"],
    ),
    "suricata2timesketch": (
        "suricata",
        "suricata2timesketch.py",
        "Suricata IDS/IPS logs (EVE JSON, fast.log, OPNsense syslog export) to Timesketch timeline.",
        ["eve.json", "fast.log", "OPNsense suricata syslog export"],
    ),
    "webhoneypot2timesketch": (
        "webhoneypot",
        "webhoneypot2timesketch.py",
        "DShield webhoneypot (isc-agent) HTTP request logs to Timesketch timeline, incl. "
        "reverse-proxy X-Forwarded-For/X-Real-Ip client resolution and matched signature metadata.",
        ["webhoneypot_YYYY-MM-DD.json"],
    ),
}

# Sibling converter modules a module imports from (beyond common/terminal).
# Dependency bodies are inlined *before* the module body, so the module's own
# top-level definitions shadow same-named dependency ones; `X as Y` imports
# become alias assignments that still capture the dependency's originals.
MODULE_DEPS: dict[str, list[str]] = {
    "apache": ["nginx"],
}

_FUTURE_RE = re.compile(r"^from __future__ import.*\n", re.MULTILINE)
_SHEBANG_RE = re.compile(r"^#!.*\n")
# `from .common import (...)` / `from timesketch_converters.common import (...)`,
# single-line or parenthesized multi-line, and `from timesketch_converters.<mod> import ...`.
_PKG_IMPORT_RE = re.compile(
    r"^from (?:\.|timesketch_converters)[.\w]* import (?:\([^)]*\)|[^\n]*)\n",
    re.MULTILINE,
)
_VERSION_IMPORT_RE = re.compile(r"^\s*from \. import __version__\n", re.MULTILINE)


def _replace_pkg_import(match: re.Match[str]) -> str:
    """Drop an inlined-package import, keeping `X as Y` aliases as `Y = X`."""
    names = match.group(0).split("import", 1)[1].strip().strip("()")
    aliases = [
        f"{alias.strip()} = {orig.strip()}"
        for part in names.split(",")
        if " as " in part
        for orig, alias in [part.strip().split(" as ")]
    ]
    return "\n".join(aliases) + "\n" if aliases else ""


def _strip(text: str, *, drop_shebang: bool = True) -> str:
    if drop_shebang:
        text = _SHEBANG_RE.sub("", text, count=1)
    text = _FUTURE_RE.sub("", text)
    text = _PKG_IMPORT_RE.sub(_replace_pkg_import, text)
    text = _VERSION_IMPORT_RE.sub("", text)
    return text.strip("\n") + "\n"


def _rewrite_browser(body: str) -> str:
    """Un-shadow the ``datetime`` module name (common.py needs it at call time)."""
    body = body.replace(
        "from datetime import datetime, timezone",
        "from datetime import datetime as _datetime, timezone as _timezone",
    )
    body = re.sub(r"\bdatetime\.fromtimestamp\b", "_datetime.fromtimestamp", body)
    body = re.sub(r"\bdatetime\((\d{4}),", r"_datetime(\1,", body)
    body = re.sub(r"\btimezone\.utc\b", "_timezone.utc", body)
    return body


def _upstream_commit(upstream: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _upstream_version(upstream: Path) -> str:
    init = (upstream / "timesketch_converters" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    return m.group(1) if m else "unknown"


def vendor(upstream: Path) -> None:
    commit = _upstream_commit(upstream)
    version = _upstream_version(upstream)
    license_text = (upstream / "LICENSE").read_text(encoding="utf-8").splitlines()[0].strip()

    # `terminal.py` (stdlib-only UI helpers) is imported by common.py, every
    # converter module, and every entry script — inline it first so the
    # stripped package imports resolve against the inlined definitions.
    terminal_body = _strip((upstream / "timesketch_converters" / "terminal.py").read_text("utf-8"))
    common_body = _strip((upstream / "timesketch_converters" / "common.py").read_text("utf-8"))

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "upstream": UPSTREAM_URL,
        "commit": commit,
        "version": version,
        "license": license_text,
        "converters": [],
    }

    for name, (module, entry, description, inputs) in CONVERTERS.items():
        module_body = _strip(
            (upstream / "timesketch_converters" / f"{module}.py").read_text("utf-8")
        )
        if module == "browser":
            module_body = _rewrite_browser(module_body)
        dep_bodies = [
            _strip((upstream / "timesketch_converters" / f"{dep}.py").read_text("utf-8"))
            for dep in MODULE_DEPS.get(module, [])
        ]
        entry_body = _strip((upstream / entry).read_text("utf-8"))

        header = (
            "#!/usr/bin/env python3\n"
            f"# {name}.py — self-contained converter, vendored from {UPSTREAM_URL}\n"
            f"# Upstream commit: {commit}\n"
            f"# Upstream version: {version} | License: {license_text}\n"
            "# Generated by scripts/vendor_converters.py — do not edit by hand;\n"
            "# re-run the script to re-sync with upstream.\n"
            "#\n"
            f"# {description}\n"
            "# Requires only the Python 3.10+ standard library.\n"
            "\n"
            "from __future__ import annotations\n"
            "\n"
            f'__version__ = "{version}+vendored.{commit[:12]}"\n'
        )
        content = "\n".join(
            [header, terminal_body, common_body, *dep_bodies, module_body, entry_body]
        )
        out = ASSETS_DIR / f"{name}.py"
        out.write_text(content, encoding="utf-8")

        manifest["converters"].append(
            {
                "name": name,
                "filename": f"{name}.py",
                "description": description,
                "inputs": inputs,
                "size_bytes": len(content.encode("utf-8")),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
        )
        print(f"vendored {out.relative_to(REPO_ROOT)} ({len(content.splitlines())} lines)")

    manifest_path = ASSETS_DIR / "manifest.json"

    # Preserve native (in-repo, non-vendored) converter entries across
    # re-vendor runs, refreshing their size/hash from the committed file.
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in existing.get("converters", []):
            if not entry.get("native"):
                continue
            content = (ASSETS_DIR / entry["filename"]).read_bytes()
            entry["size_bytes"] = len(content)
            entry["sha256"] = hashlib.sha256(content).hexdigest()
            manifest["converters"].append(entry)
    manifest["converters"].sort(key=lambda entry: entry["name"])

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {manifest_path.relative_to(REPO_ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream",
        default=str(Path.home() / "Projekte" / "2timesketch"),
        help="Path to a local checkout of overcuriousity/2timesketch.",
    )
    args = parser.parse_args()
    upstream = Path(args.upstream)
    if not (upstream / "timesketch_converters" / "common.py").is_file():
        print(f"error: {upstream} is not a 2timesketch checkout", file=sys.stderr)
        return 1
    vendor(upstream)
    return 0


if __name__ == "__main__":
    sys.exit(main())
