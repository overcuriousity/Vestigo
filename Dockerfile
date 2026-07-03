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
RUN uv sync --frozen --no-dev

COPY --from=frontend-build /frontend/dist ./frontend/dist

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
# Run uvicorn against the app factory directly (not the `tsig-web` entry point) —
# that entry point always rebuilds the frontend from source on startup, which this
# image doesn't carry (only the pre-built `frontend/dist`) or have node/npm for.
CMD ["uvicorn", "tracesignal.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
