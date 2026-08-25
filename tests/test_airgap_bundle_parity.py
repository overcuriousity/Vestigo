"""Every ClickHouse drop-in the bundle carries must reach the install directory.

`install.sh` copied `allow-default-network.xml` and silently not `memory.xml`,
so the airgap compose's `./clickhouse/memory.xml` mount source never existed.
Docker materialises a missing bind-mount source as an empty *directory*, so
ClickHouse started, merged no ceiling, and derived 0.9 x whatever RAM a
limit-less container saw — the exact unbounded condition 1.15 shipped to
prevent, on the deployment that actually reaches production.

Grepping shell is a blunt instrument, but the alternative is running an airgap
install in CI, and the failure this guards is one nobody notices for a release.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUNDLE_SCRIPT = REPO / "scripts" / "airgap-bundle.sh"
INSTALL_SCRIPT = REPO / "deploy" / "airgap" / "install.sh"
AIRGAP_COMPOSE = REPO / "deploy" / "airgap" / "docker-compose.airgap.yml"


def _staged_clickhouse_files() -> set[str]:
    """Basenames the bundle script copies into the bundle's `clickhouse/`."""
    text = BUNDLE_SCRIPT.read_text()
    return set(re.findall(r'cp deploy/clickhouse/(\S+) "\$BUNDLE/clickhouse/"', text))


def _installed_clickhouse_files() -> set[str]:
    """Basenames install.sh copies into the install directory's `clickhouse/`."""
    text = INSTALL_SCRIPT.read_text()
    return set(re.findall(r'cp clickhouse/(\S+) "\$INSTALL_DIR/clickhouse/"', text))


def _mounted_clickhouse_files() -> set[str]:
    """Basenames the airgap compose bind-mounts out of `./clickhouse/`."""
    text = AIRGAP_COMPOSE.read_text()
    return set(re.findall(r"- \./clickhouse/(\S+?):", text))


def test_bundle_stages_the_memory_ceiling():
    assert "memory.xml" in _staged_clickhouse_files()


def test_installer_copies_every_file_the_bundle_stages():
    """The regression itself: a staged file the installer forgets is a mount
    source that does not exist, and Docker turns that into an empty directory
    rather than an error."""
    missing = _staged_clickhouse_files() - _installed_clickhouse_files()
    assert not missing, f"install.sh never copies: {sorted(missing)}"


def test_every_mounted_file_is_one_the_installer_copies():
    missing = _mounted_clickhouse_files() - _installed_clickhouse_files()
    assert not missing, f"compose mounts files install.sh never places: {sorted(missing)}"


def test_installer_asserts_the_memory_ceiling_is_a_regular_file():
    """A directory at that path is the silent-failure shape, so the check has to
    be `-f`, not `-e`."""
    text = INSTALL_SCRIPT.read_text()
    assert "clickhouse/memory.xml" in text
    assert re.search(
        r'\[ ! -f (clickhouse/memory\.xml|"\$INSTALL_DIR/clickhouse/memory\.xml") \]', text
    ), "install.sh must test memory.xml with -f before compose up"
