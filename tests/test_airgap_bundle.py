"""The airgapped deployment path, asserted where it actually breaks.

Nobody notices a broken offline build until they are standing at an isolated
host with a USB stick, so the wiring is checked here instead: that the build
never reaches for the node image, that the bundle's compose file builds
nothing, and that the three files which have to agree on a variable name
actually do.

Running the bundle end to end is a container build and several GB of image
export — out of scope for the test suite; `scripts/airgap-bundle.sh --help`
documents the manual rehearsal.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
COMPOSE = REPO / "deploy/airgap/docker-compose.airgap.yml"
BUNDLE_SH = REPO / "scripts/airgap-bundle.sh"
INSTALL_SH = REPO / "deploy/airgap/install.sh"


def test_prebuilt_frontend_stage_needs_no_base_image():
    """`frontend-prebuilt` is `FROM scratch`, so nothing is resolved for it.

    This is the whole airgap mechanism: BuildKit skips a stage no reachable
    stage copies from, so selecting this one means `node:22-alpine` is never
    looked up — the failure the operator hit on the isolated host.
    """
    text = DOCKERFILE.read_text()
    assert "ARG FRONTEND_STAGE=frontend-build" in text
    assert "FROM scratch AS frontend-prebuilt" in text
    assert "COPY --from=${FRONTEND_STAGE} /frontend/dist ./frontend/dist" in text
    # The ARG must be re-declared inside the app stage or it expands to empty.
    app_stage = text.split("FROM python:")[1]
    assert re.search(r"^ARG FRONTEND_STAGE$", app_stage, re.MULTILINE)


def test_dockerignore_lets_the_prebuilt_dist_through():
    """An ignored `frontend/dist` would make the prebuilt stage copy nothing."""
    lines = [
        line.strip()
        for line in (REPO / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "frontend/dist" not in lines


def test_the_bundle_compose_file_builds_nothing():
    """A `build:` key would need a registry — the one thing the target lacks."""
    text = COMPOSE.read_text()
    directives = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in directives if line.strip().startswith("build:")]
    services_block = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    services = re.findall(r"^  (\w+):$", services_block, re.MULTILINE)
    assert set(services) == {"postgres", "clickhouse", "qdrant", "app"}
    # Every service runs a loaded image rather than building one.
    assert len(re.findall(r"^    image:", services_block, re.MULTILINE)) == len(services)


def test_the_image_tag_variable_agrees_across_all_three_files():
    """compose reads it, install.sh writes it, the builder names it.

    A rename in one place alone leaves an installer that starts the previous
    version and reports success.
    """
    assert "VESTIGO_IMAGE_TAG" in COMPOSE.read_text()
    assert "VESTIGO_IMAGE_TAG=$TAG" in BUNDLE_SH.read_text()
    assert "VESTIGO_IMAGE_TAG=$VESTIGO_IMAGE_TAG" in INSTALL_SH.read_text()


def test_the_builder_finds_every_backing_service_image_in_the_compose_file():
    """The builder greps the compose file so tags live in one place.

    Asserted by running that exact extraction: a compose edit that changes the
    line shape would otherwise silently ship a bundle missing an image, which
    only surfaces as a failed `load` on the isolated host.
    """
    extraction = r"sed -n 's/^ *image: \(docker\.io[^$]*\)$/\1/p'"
    assert extraction in BUNDLE_SH.read_text()
    found = subprocess.run(
        ["sed", "-n", r"s/^ *image: \(docker\.io[^$]*\)$/\1/p", str(COMPOSE)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert len(found) == 3
    assert all(image.startswith("docker.io/") for image in found)
    # The app image is built, not pulled, so it must not appear here.
    assert not any("vestigo-app" in image for image in found)


@pytest.mark.parametrize("script", [BUNDLE_SH, INSTALL_SH])
def test_scripts_are_executable_and_syntactically_valid(script):
    assert os.access(script, os.X_OK), f"{script.name} is not executable"
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_the_installer_never_overwrites_an_existing_env():
    """An upgrade must not silently rewrite an operator's configuration."""
    text = INSTALL_SH.read_text()
    assert "if [ ! -f .env ]; then" in text
    assert "keeping existing .env" in text
