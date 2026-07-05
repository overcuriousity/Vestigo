# Reference container build for the TraceSignal application itself.
# Optional: TraceSignal is a native `uv`/Python app and runs fine directly on the host
# (see README "Quick start"). This image exists for operators who prefer to run the
# whole stack — backing services plus the app — via docker-compose.

FROM node:22-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim AS app
WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY README.md LICENSE ./
# INSTALL_EMBEDDINGS=1 adds the optional local-embedding stack (torch +
# sentence-transformers, ~2 GB) once it is an extra. Default off: without it
# the app serves everything except local embedding; point
# TS_EMBEDDING_API_BASE_URL at a remote endpoint for embedding features
# without the heavy install.
ARG INSTALL_EMBEDDINGS=0
RUN uv sync --frozen --no-dev $(test "$INSTALL_EMBEDDINGS" = "1" && echo "--extra embeddings")

COPY --from=frontend-build /frontend/dist ./frontend/dist

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
# Run uvicorn against the app factory directly (not the `tsig-web` entry point) —
# that entry point rebuilds the frontend from source on startup, which this
# image doesn't carry (only the pre-built `frontend/dist`) or have node/npm for.
# `api.main` exposes only the `create_app()` factory (no module-level `app`),
# so uvicorn needs `--factory`.
CMD ["uvicorn", "--factory", "tracesignal.api.main:create_app", "--host", "0.0.0.0", "--port", "8080"]
