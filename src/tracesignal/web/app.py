"""Entry point for the TraceSignal web server."""

import subprocess
from pathlib import Path

import uvicorn

from tracesignal.api.main import create_app

_FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"


def _build_frontend() -> None:
    print("Building frontend...")
    subprocess.run(["npm", "install"], cwd=_FRONTEND_DIR, check=True)
    subprocess.run(["npm", "run", "build"], cwd=_FRONTEND_DIR, check=True)


_build_frontend()
app = create_app()


def start() -> None:
    uvicorn.run(
        "tracesignal.web.app:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
    )


if __name__ == "__main__":
    start()
