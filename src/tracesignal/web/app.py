"""Entry point for the TraceSignal web server."""

import os
import subprocess
from pathlib import Path

import uvicorn

from tracesignal.api.main import create_app
from tracesignal.core.config import get_settings

_FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"
_FRONTEND_DIST = _FRONTEND_DIR / "dist"


def _build_frontend() -> None:
    """Build the frontend bundle when it is missing (or a rebuild is forced).

    Building runs `npm install`, which reaches the network — unacceptable as
    an unconditional startup step for an airgapped-by-default deployment
    (docs/TECH_STACK.md §6). A prebuilt `frontend/dist` (carried over per the
    airgapped install docs, or baked into the container image) is served
    as-is. Set TS_FRONTEND_REBUILD=1 to force a rebuild after frontend
    source changes.
    """
    force = os.environ.get("TS_FRONTEND_REBUILD", "").lower() in {"1", "true", "yes"}
    if _FRONTEND_DIST.is_dir() and not force:
        return
    print("Building frontend...")
    subprocess.run(["npm", "install"], cwd=_FRONTEND_DIR, check=True)
    subprocess.run(["npm", "run", "build"], cwd=_FRONTEND_DIR, check=True)


_build_frontend()
app = create_app()


def start() -> None:
    # The auto-reloader is a development tool (extra watcher process, file
    # polling); only enable it outside production.
    reload = get_settings().environment == "development"
    uvicorn.run(
        "tracesignal.web.app:app",
        host="0.0.0.0",
        port=8080,
        reload=reload,
    )


if __name__ == "__main__":
    start()
