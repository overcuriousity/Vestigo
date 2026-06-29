"""FastAPI application factory and API routers."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from tracevector import __version__
from tracevector.api.routers import cases, events, jobs

_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="TraceVector",
        description="Local-first forensic log investigation platform.",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:8080"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_class=JSONResponse)
    async def health() -> dict:
        return {"status": "ok", "version": __version__}

    app.include_router(cases.router)
    app.include_router(events.router)
    app.include_router(jobs.router)

    # Serve the built frontend when frontend/dist exists.
    # Run `npm run build` inside frontend/ once; tv-web then serves everything.
    # For development with HMR, run `npm run dev` (port 5173) alongside tv-web instead.
    if _FRONTEND_DIST.is_dir():
        assets_dir = _FRONTEND_DIST / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_frontend(full_path: str) -> FileResponse:
            candidate = _FRONTEND_DIST / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(_FRONTEND_DIST / "index.html")

    return app
