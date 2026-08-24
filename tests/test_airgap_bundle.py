"""The airgapped deployment path, asserted where it actually breaks.

Nobody notices a broken offline build until they are standing at an isolated
host with a USB stick, so the wiring is checked here instead: that the build
never reaches for the node image, that the bundle's compose file builds
nothing, and that the three files which have to agree on a variable name
actually do.

The installer's decisions — refusing an --app-only bundle on a host without the
backing services, refusing a short image archive, keeping an operator's .env
across an upgrade — are exercised for real against a fake container engine and a
bundle directory that carries a stub archive instead of gigabytes.

Building a real bundle is a container build plus several GB of image export, so
that stays out of the suite; `scripts/airgap-bundle.sh --help` and
`docs/DEPLOYMENT.md` §"Route A" document the manual rehearsal.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
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
    stage copies from, so selecting this one means the node build image is
    never looked up — the failure the operator hit on the isolated host.
    """
    text = DOCKERFILE.read_text()
    assert "ARG FRONTEND_STAGE=frontend-build" in text
    assert "FROM scratch AS frontend-prebuilt" in text


def test_the_stage_is_selected_by_from_and_never_by_copy_from():
    """Docker refuses `COPY --from=${VAR}`; buildah expands it happily.

    That asymmetry is why the first version of this Dockerfile passed a local
    podman build and failed every Docker one with "variable expansion is not
    supported for --from". The supported form — Docker's own suggested
    workaround — is an alias stage: `FROM ${ARG} AS frontend`, then a literal
    `COPY --from=frontend`. Pinned because reverting to the terser form is an
    easy and entirely plausible edit.
    """
    text = DOCKERFILE.read_text()
    assert "FROM ${FRONTEND_STAGE} AS frontend\n" in text
    assert "COPY --from=frontend /frontend/dist ./frontend/dist" in text
    assert not re.search(r"COPY --from=\$\{?FRONTEND_STAGE", text)
    # `FROM` reads the ARG from global scope, so it must be declared before
    # the first stage — and needs no re-declaration inside the app stage,
    # which is where a re-declaration would now be dead weight.
    assert text.index("ARG FRONTEND_STAGE=") < text.index("FROM node:")
    app_stage = text.split("FROM python:")[1]
    assert not re.search(r"^ARG FRONTEND_STAGE$", app_stage, re.MULTILINE)


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
    # Split on `/` and compare the registry component exactly rather than
    # prefix-matching the whole reference: `startswith("docker.io/")` is the
    # shape CodeQL flags as `py/incomplete-url-substring-sanitization`, and it
    # is right that the anchored form is the one to write — a reference is a
    # structured name, so check the field, not the text around it.
    assert all(image.split("/", 1)[0] == "docker.io" for image in found)
    # The app image is built, not pulled, so it must not appear here.
    assert all(image.rsplit("/", 1)[-1].split(":")[0] != "vestigo-app" for image in found)


@pytest.mark.parametrize("script", [BUNDLE_SH, INSTALL_SH])
def test_scripts_are_executable_and_syntactically_valid(script):
    assert os.access(script, os.X_OK), f"{script.name} is not executable"
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_the_installer_never_overwrites_an_existing_env():
    """An upgrade must not silently rewrite an operator's configuration."""
    text = INSTALL_SH.read_text()
    assert "if [ ! -f .env ]; then" in text
    assert "keeping existing .env" in text


def test_podman_gets_the_multi_image_flag():
    """`podman save` without `-m` writes ONE image carrying every tag.

    It does not fail. The far side loads a `postgres:17-alpine` that is really
    qdrant, which is the worst possible place to discover a packaging bug, so
    the flag is asserted rather than assumed.
    """
    text = BUNDLE_SH.read_text()
    assert '[ "$ENGINE" = podman ]' in text
    assert "SAVE_OPTS+=(-m)" in text


def test_both_sides_count_the_images_in_the_archive():
    """Builder and installer independently verify the archive's inventory.

    The count is what catches a short save; checking it on both sides means a
    bundle that was damaged in transit fails before anything is loaded.
    """
    assert "grep -o '\"RepoTags\"' | wc -l" in BUNDLE_SH.read_text()
    assert "grep -o '\"RepoTags\"' | wc -l" in INSTALL_SH.read_text()
    assert "VESTIGO_IMAGE_COUNT" in BUNDLE_SH.read_text()
    assert "VESTIGO_IMAGE_COUNT" in INSTALL_SH.read_text()


def test_the_app_image_is_fully_qualified_in_all_three_files():
    """Bare `vestigo-app:TAG` means one thing to podman and another to docker.

    Podman stores a locally built unqualified image as `localhost/vestigo-app`
    and `podman save` writes that name into the archive. `docker load` keeps it
    verbatim, but resolves a bare reference to `docker.io/library/vestigo-app` —
    so a podman-built bundle installed on a docker host reported "missing
    image(s) after load" for the image the load log had just listed, and refused
    to install. Every one of the three files that names the image must therefore
    carry the registry component, and carry the same one.
    """
    build, compose, check = BUNDLE_SH.read_text(), COMPOSE.read_text(), INSTALL_SH.read_text()
    assert 'APP_IMAGE="localhost/vestigo-app:$TAG"' in build
    assert re.search(
        r"^\s*image: localhost/vestigo-app:\$\{VESTIGO_IMAGE_TAG", compose, re.MULTILINE
    )
    assert 'image_usable "localhost/vestigo-app:$VESTIGO_IMAGE_TAG"' in check
    # And no unqualified reference survives anywhere: one missed spot is the
    # whole bug back again.
    for name, text in (("bundle", build), ("compose", compose), ("install", check)):
        assert not re.search(r"(?<![\w/])vestigo-app:\$", text), f"unqualified image in {name}"


def test_the_compose_project_name_is_pinned():
    """Volumes must not depend on the directory a bundle happened to unpack in.

    Compose derives the project name — and every volume name with it — from the
    directory. An upgrade unpacks a *new* directory, so without a pinned `name:`
    the new stack comes up beside the old data, empty, and looks healthy.
    """
    assert re.search(r"^name: vestigo$", COMPOSE.read_text(), re.MULTILINE)


# ── behavioral: the installer driven against a fake container engine ────────


def _fake_bundle(tmp_path, images, *, scope="full", archive_images=None):
    """A bundle directory complete enough for install.sh, minus the GBs."""
    bundle = tmp_path / "bundle"
    (bundle / "images").mkdir(parents=True)
    (bundle / "clickhouse").mkdir()

    # A docker-archive is a tar with a manifest.json at its root; install.sh
    # only counts the entries, so a real one is unnecessary.
    manifest = json.dumps([{"RepoTags": [image]} for image in (archive_images or images)])
    (bundle / "manifest.json").write_text(manifest)
    with tarfile.open(bundle / "images/vestigo-stack.tar", "w") as tar:
        tar.add(bundle / "manifest.json", arcname="manifest.json")
    (bundle / "manifest.json").unlink()

    shutil.copy(COMPOSE, bundle / "compose.airgap.yml")
    shutil.copy(REPO / "deploy/clickhouse/allow-default-network.xml", bundle / "clickhouse")
    shutil.copy(INSTALL_SH, bundle / "install.sh")
    (bundle / "install.sh").chmod(0o755)
    (bundle / ".env.example").write_text("VESTIGO_ENVIRONMENT=production\n")
    (bundle / "nginx-tls.conf").write_text("# stub\n")
    (bundle / "BUNDLE-INFO").write_text("Vestigo 9.9.9 (deadbee)\n")
    (bundle / "images.list").write_text("".join(f"{image}\n" for image in images))
    (bundle / "bundle.env").write_text(
        "VESTIGO_IMAGE_TAG=9.9.9-deadbee\n"
        "VESTIGO_VERSION=9.9.9\n"
        "VESTIGO_COMMIT=deadbee\n"
        f"VESTIGO_IMAGE_COUNT={len(archive_images or images)}\n"
        f"VESTIGO_BUNDLE_SCOPE={scope}\n"
    )
    sums = subprocess.run(
        "find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum",
        shell=True,
        cwd=bundle,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    (bundle / "MANIFEST.sha256").write_text(sums)
    return bundle


def _fake_engine(tmp_path, present_images, *, load_output="", unusable_images=()):
    """A podman stand-in that knows which images exist and logs `compose up`.

    `load_output` and `unusable_images` model the split the installer now has to
    survive: an engine that registers an image's metadata (so `image inspect`
    passes) while its layers never extracted (so `create` cannot prepare a
    snapshot), reporting the failure on stderr and exiting 0 regardless.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "engine.log"
    known = "\n".join(present_images)
    broken = "\n".join(unusable_images)
    # `docker` must exist and be unusable: that is the case the engine probe was
    # written for, and it keeps a real docker on the test host out of the way.
    (bin_dir / "docker").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "podman").write_text(
        f"""#!/bin/sh
echo "$@" >> {log}
case "$1 $2" in
  "image inspect")
    printf '%s\\n' '{known}' | grep -qxF "$3" ;;
  "create "*)
    # A container can only be created from an image whose layers unpacked.
    # The pattern must be a prefix match: the installer probes with
    # `create <image> <command>`, so "$1 $2" carries the image and an exact
    # "create " never matches — it fell through to the catch-all below and
    # reported every image usable, including the broken one.
    printf '%s\\n' '{broken}' | grep -qxF "$2" && exit 125
    echo probe-container-id ;;
  "rm -f") exit 0 ;;
  "volume inspect") exit 1 ;;
  "volume ls") exit 0 ;;
  "compose version"|"compose up"|"info ") exit 0 ;;
  "load -i")
    # Exit 0 even when it reports failures — the behaviour that fooled the
    # first version of the check.
    cat <<'LOADOUT'
{load_output}
LOADOUT
    exit 0 ;;
  *) exit 0 ;;
esac
"""
    )
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    return bin_dir, log


def _run_install(bundle, bin_dir, install_dir, *args):
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "VESTIGO_HEALTH_TIMEOUT_SECONDS": "0",
    }
    return subprocess.run(
        ["bash", str(bundle / "install.sh"), "--dir", str(install_dir), *args],
        capture_output=True,
        text=True,
        env=env,
    )


ALL_IMAGES = [
    "localhost/vestigo-app:9.9.9-deadbee",
    "docker.io/library/postgres:17-alpine",
    "docker.io/clickhouse/clickhouse-server:26.6.1.1193-alpine",
    "docker.io/qdrant/qdrant:v1.18.2",
]


def test_install_starts_the_stack_when_every_image_is_present(tmp_path):
    bundle = _fake_bundle(tmp_path, ALL_IMAGES)
    bin_dir, log = _fake_engine(tmp_path, ALL_IMAGES)
    install_dir = tmp_path / "opt"

    result = _run_install(bundle, bin_dir, install_dir)

    assert result.returncode == 0, result.stderr
    assert "compose up -d --no-build" in log.read_text()
    # The install directory, not the bundle, is what the stack runs from.
    assert (install_dir / "docker-compose.yml").is_file()
    assert "VESTIGO_IMAGE_TAG=9.9.9-deadbee" in (install_dir / ".env").read_text()


def test_an_app_only_bundle_refuses_a_host_without_the_backing_services(tmp_path):
    """The failure this whole bundle exists to prevent, caught before `up`.

    A missing image sends compose to a registry the host cannot reach, so it
    surfaces as a DNS timeout rather than as the true cause. Nothing may start.
    """
    install_dir = tmp_path / "opt"
    install_dir.mkdir()
    (install_dir / ".env").write_text("VESTIGO_IMAGE_TAG=1.0.0-oldcommit\n")
    bundle = _fake_bundle(tmp_path, ALL_IMAGES[:1], scope="app-only")
    bin_dir, log = _fake_engine(tmp_path, ALL_IMAGES[:1])

    result = _run_install(bundle, bin_dir, install_dir)

    assert result.returncode != 0
    assert "app-only" in result.stderr
    assert "docker.io/library/postgres:17-alpine" in result.stderr
    assert "compose up" not in log.read_text()
    # A bundle that cannot work leaves the existing install untouched: same tag,
    # no half-copied payload pointing at an image this host does not have.
    assert (install_dir / ".env").read_text() == "VESTIGO_IMAGE_TAG=1.0.0-oldcommit\n"
    assert not (install_dir / "docker-compose.yml").exists()


def test_a_short_image_archive_is_rejected_before_anything_loads(tmp_path):
    """The `podman save -m` failure, caught on the receiving side too."""
    bundle = _fake_bundle(tmp_path, ALL_IMAGES, archive_images=ALL_IMAGES[:1])
    # bundle.env now declares 1; rewrite it to the 4 a real full bundle claims.
    env_file = bundle / "bundle.env"
    env_file.write_text(env_file.read_text().replace("IMAGE_COUNT=1", "IMAGE_COUNT=4"))
    subprocess.run(
        "find . -type f ! -name MANIFEST.sha256 -print0 | sort -z "
        "| xargs -0 sha256sum > MANIFEST.sha256",
        shell=True,
        cwd=bundle,
        check=True,
    )
    bin_dir, log = _fake_engine(tmp_path, ALL_IMAGES)

    result = _run_install(bundle, bin_dir, tmp_path / "opt")

    assert result.returncode != 0
    assert "holds 1 image(s)" in result.stderr
    assert not log.exists() or "load" not in log.read_text()


def test_upgrading_keeps_the_env_and_reports_both_tags(tmp_path):
    """Carrying a newer bundle to a running host must not reset its config."""
    install_dir = tmp_path / "opt"
    install_dir.mkdir()
    (install_dir / ".env").write_text(
        "VESTIGO_IMAGE_TAG=1.0.0-oldcommit\nVESTIGO_ADMIN_PASSWORD=kept-secret\n"
    )
    bundle = _fake_bundle(tmp_path, ALL_IMAGES)
    bin_dir, _ = _fake_engine(tmp_path, ALL_IMAGES)

    result = _run_install(bundle, bin_dir, install_dir)

    assert result.returncode == 0, result.stderr
    env_text = (install_dir / ".env").read_text()
    assert "VESTIGO_ADMIN_PASSWORD=kept-secret" in env_text
    assert "VESTIGO_IMAGE_TAG=9.9.9-deadbee" in env_text
    assert "1.0.0-oldcommit" in result.stdout  # named in the upgrade and rollback lines


def test_unknown_arguments_are_fatal(tmp_path):
    """`--dry-run` must not be read as 'install everything'."""
    bundle = _fake_bundle(tmp_path, ALL_IMAGES)
    bin_dir, log = _fake_engine(tmp_path, ALL_IMAGES)

    result = _run_install(bundle, bin_dir, tmp_path / "opt", "--dry-run")

    assert result.returncode != 0
    assert "unknown option" in result.stderr
    assert not log.exists()


# The real thing, from a fresh Docker 29 inside an unprivileged LXC. `load`
# printed one of these per image and exited 0.
UNPACK_FAILURE = (
    "Loaded image: localhost/vestigo-app:9.9.9-deadbee\n"
    "Error unpacking image localhost/vestigo-app:9.9.9-deadbee: apply layer error for "
    '"docker.io/library/vestigo-app:9.9.9-deadbee": failed to extract layer '
    "sha256:5b21fa92fbc3: failed to mount /var/lib/containerd/tmpmounts/containerd-mount1: "
    'mount source: "overlay", target: "/var/lib/containerd/tmpmounts/containerd-mount1", '
    "fstype: overlay, flags: 0, err: permission denied"
)


def test_images_that_register_but_do_not_unpack_stop_the_install(tmp_path):
    """`load` exits 0 after failing to extract every layer.

    It registers an image's metadata before unpacking it, so a host that cannot
    mount overlay — a fresh Docker on the containerd snapshotter inside an
    unprivileged LXC — leaves four images that `image inspect` is perfectly
    happy with and no container can start from. The installer used to believe
    them, copy the payload and start a stack in which nothing ran. Trusting an
    exit status is what `podman save -m` already taught us one layer up.
    """
    install_dir = tmp_path / "opt"
    install_dir.mkdir()
    (install_dir / ".env").write_text("VESTIGO_IMAGE_TAG=1.0.0-oldcommit\n")
    bundle = _fake_bundle(tmp_path, ALL_IMAGES)
    bin_dir, log = _fake_engine(tmp_path, ALL_IMAGES, load_output=UNPACK_FAILURE)

    result = _run_install(bundle, bin_dir, install_dir)

    assert result.returncode != 0
    assert "could not unpack" in result.stderr
    assert "permission denied" in result.stderr  # the operator's actual cause, quoted back
    assert "Troubleshooting" in result.stderr
    # Nothing started, and the running install is exactly as it was.
    assert "compose up" not in log.read_text()
    assert (install_dir / ".env").read_text() == "VESTIGO_IMAGE_TAG=1.0.0-oldcommit\n"
    assert not (install_dir / "docker-compose.yml").exists()


def test_an_image_present_but_unusable_is_caught_before_the_stack_starts(tmp_path):
    """Belt to the load check's braces: `inspect` passes, `create` cannot.

    A quieter engine might register metadata without printing anything this
    could grep for. Preparing a snapshot is the part that actually needs the
    layers, so the check creates a throwaway container rather than trusting
    `image inspect`, which reads metadata alone.
    """
    install_dir = tmp_path / "opt"
    install_dir.mkdir()
    (install_dir / ".env").write_text("VESTIGO_IMAGE_TAG=1.0.0-oldcommit\n")
    bundle = _fake_bundle(tmp_path, ALL_IMAGES)
    bin_dir, log = _fake_engine(
        tmp_path, ALL_IMAGES, unusable_images=["docker.io/qdrant/qdrant:v1.18.2"]
    )

    result = _run_install(bundle, bin_dir, install_dir)

    assert result.returncode != 0
    assert "docker.io/qdrant/qdrant:v1.18.2" in result.stderr
    assert "compose up" not in log.read_text()
    assert not (install_dir / "docker-compose.yml").exists()


def test_the_bundle_holds_no_compose_file_that_compose_would_auto_discover(tmp_path):
    """Running `docker compose` from the bundle must find nothing.

    The compose file pins `name: vestigo`, so a copy named `docker-compose.yml`
    in the extracted bundle means a command run from the wrong directory drives
    the *real* project with no `.env` beside it. The bundle carries it under a
    name compose does not look for; only the install directory gets the
    canonical one.
    """
    bundle = _fake_bundle(tmp_path, ALL_IMAGES)
    autodiscovered = {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"}
    assert not {p.name for p in bundle.iterdir()} & autodiscovered
    assert (bundle / "compose.airgap.yml").is_file()

    # And the builder is what puts it there under that name.
    assert 'cp deploy/airgap/docker-compose.airgap.yml "$BUNDLE/compose.airgap.yml"' in (
        BUNDLE_SH.read_text()
    )

    bin_dir, _ = _fake_engine(tmp_path, ALL_IMAGES)
    install_dir = tmp_path / "opt"
    assert _run_install(bundle, bin_dir, install_dir).returncode == 0
    assert (install_dir / "docker-compose.yml").is_file()


def test_check_verifies_without_touching_the_host(tmp_path):
    bundle = _fake_bundle(tmp_path, ALL_IMAGES)
    bin_dir, log = _fake_engine(tmp_path, ALL_IMAGES)
    install_dir = tmp_path / "opt"

    result = _run_install(bundle, bin_dir, install_dir, "--check")

    assert result.returncode == 0, result.stderr
    assert not install_dir.exists()
    assert not log.exists() or "compose up" not in log.read_text()
