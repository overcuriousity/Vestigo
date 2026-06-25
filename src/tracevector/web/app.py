"""Entry point for the TraceVector web server."""

import shutil
import subprocess
from pathlib import Path

import uvicorn


def _ensure_frontend_built() -> None:
    """Build the frontend automatically if its distribution folder is missing."""
    project_root = Path(__file__).resolve().parents[3]
    frontend_dir = project_root / "frontend"
    dist_dir = frontend_dir / "dist"

    if dist_dir.is_dir():
        return

    package_json = frontend_dir / "package.json"
    if not package_json.is_file():
        raise RuntimeError(
            f"Frontend source not found at {frontend_dir}. "
            "Cannot build the web UI automatically."
        )

    if shutil.which("npm") is None:
        raise RuntimeError(
            "npm is required to build the frontend automatically. "
            "Install Node.js/npm, or build manually with: "
            "cd frontend && npm install && npm run build"
        )

    print("Frontend dist not found. Building it now...")
    subprocess.run(["npm", "install"], cwd=frontend_dir, check=True)
    subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True)
    print("Frontend build complete.")


# Build the frontend before creating the FastAPI app so the UI routes are mounted.
_ensure_frontend_built()

from tracevector.api.main import create_app  # noqa: E402

app = create_app()


def start() -> None:
    """Start the Uvicorn server."""
    uvicorn.run(
        "tracevector.web.app:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
    )


if __name__ == "__main__":
    start()
