"""The ClickHouse log-cap recovery script, asserted where it actually breaks.

The failure this script exists for is not obvious from the outside: ClickHouse's
stock image logs at `trace` into the container's writable layer with an ~11 GB
ceiling, and when a log write finally fails the server does not degrade — the
ofstream latches its failbit, every subsequent rotation check throws, and the
logging thread spins forever trying to log that it cannot log. It reads as a
crash. The cure is a capped logger and a container recreate.

Running the script for real means recreating a container, so that stays out of
the suite. What is checked here is everything that can rot silently: the drop-in
XML it emits stays well-formed and keeps saying what it means, and the line it
splices into docker-compose.yml still lands in the clickhouse service and still
parses. The anchor it splices against lives in a file nobody edits with this
script in mind, which is exactly why it is asserted rather than trusted.
"""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "clickhouse-log-recovery.sh"
COMPOSE = REPO / "docker-compose.yml"


def _script_text() -> str:
    return SCRIPT.read_text()


def _embedded_dropin() -> str:
    """The logger XML the script writes, lifted out of its heredoc."""
    match = re.search(
        r'^cat > "\$DROPIN" <<\'EOF\'\n(.*?)^EOF$',
        _script_text(),
        re.MULTILINE | re.DOTALL,
    )
    assert match, "the drop-in heredoc moved; this test can no longer see what is written"
    return match.group(1)


def test_script_is_executable() -> None:
    assert SCRIPT.exists(), f"{SCRIPT} is missing"
    assert SCRIPT.stat().st_mode & 0o111, "script is not executable"


def test_script_parses_as_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)


def test_help_needs_no_container_engine() -> None:
    """--help must work on a machine with nothing installed, or it cannot be read."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "Cap ClickHouse's own logging" in result.stdout


def test_dropin_is_well_formed_xml() -> None:
    ET.fromstring(_embedded_dropin())


def test_dropin_caps_the_logger() -> None:
    """The whole point: not `trace`, and bounded."""
    root = ET.fromstring(_embedded_dropin())
    logger = root.find("logger")
    assert logger is not None, "no <logger> section — the cap is gone"
    assert logger.findtext("level") == "information"
    assert logger.findtext("size") == "100M"
    assert logger.findtext("count") == "3"


@pytest.mark.parametrize(
    "table",
    ["trace_log", "text_log", "metric_log", "asynchronous_metric_log", "query_metric_log"],
)
def test_unbounded_telemetry_tables_are_disabled(table: str) -> None:
    """These grow without limit on the data volume and nothing in Vestigo reads them."""
    element = ET.fromstring(_embedded_dropin()).find(table)
    assert element is not None, f"<{table}> is no longer disabled"
    assert element.get("remove") == "1"


@pytest.mark.parametrize("table", ["query_log", "part_log"])
def test_useful_logs_are_kept_but_bounded(table: str) -> None:
    """Deliberately NOT removed — they are what you want when an ingest misbehaves."""
    element = ET.fromstring(_embedded_dropin()).find(table)
    assert element is not None, f"<{table}> should be kept, not dropped"
    assert element.get("remove") != "1", f"{table} is worth keeping for diagnosis"
    ttl = element.findtext("ttl")
    assert ttl and "DELETE" in ttl, f"{table} kept but unbounded — it needs a TTL"


def test_compose_anchor_still_exists() -> None:
    """The script splices its mount in after this line, and fails loudly without it."""
    anchor = re.search(r'^ANCHOR="([^"]+)"', _script_text(), re.MULTILINE)
    assert anchor, "ANCHOR assignment moved"
    assert anchor.group(1) in COMPOSE.read_text(), (
        f"docker-compose.yml no longer contains {anchor.group(1)!r}; "
        "the script cannot place its mount and will abort"
    )


def test_spliced_mount_lands_in_the_clickhouse_service(tmp_path: Path) -> None:
    """Reproduce the awk splice and assert the result is valid YAML in the right place."""
    text = _script_text()
    mount_line = re.search(r'^MOUNT_LINE="([^"]+)"', text, re.MULTILINE)
    dropin = re.search(r'^DROPIN="([^"]+)"', text, re.MULTILINE)
    anchor = re.search(r'^ANCHOR="([^"]+)"', text, re.MULTILINE)
    assert mount_line and dropin and anchor

    line = mount_line.group(1).replace("${DROPIN}", dropin.group(1))

    out, done = [], False
    for original in COMPOSE.read_text().splitlines():
        out.append(original)
        if not done and anchor.group(1) in original:
            out.append(line)
            done = True
    assert done, "anchor never matched"

    patched = tmp_path / "docker-compose.yml"
    patched.write_text("\n".join(out) + "\n")

    config = yaml.safe_load(patched.read_text())
    volumes = config["services"]["clickhouse"]["volumes"]
    assert any(dropin.group(1) in str(v) for v in volumes), (
        "the drop-in mount did not land in the clickhouse service"
    )


def test_never_pulls_from_a_registry() -> None:
    """Airgapped hosts cannot reach a registry; a pull would hang, not fail fast."""
    text = _script_text()
    assert "--pull never" in text, "the airgap guard is gone"
    assert "image inspect" in text, "the image is no longer verified present before recreate"


def test_refuses_when_the_data_is_not_on_a_volume() -> None:
    """Recreating with data in the container layer destroys every case. Guard must stay."""
    assert "would DESTROY the event data" in _script_text(), (
        "the mount-type guard is gone — a recreate could now delete the event store"
    )
