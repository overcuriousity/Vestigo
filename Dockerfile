# Reference container build for the Vestigo application itself.
# Optional: Vestigo is a native `uv`/Python app and runs fine directly on the host
# (see README "Quick start"). This image exists for operators who prefer to run the
# whole stack — backing services plus the app — via docker-compose.

# Where the built frontend comes from. Two stages provide it:
#   frontend-build     (default) builds it here, needs the node base image.
#   frontend-prebuilt  takes `frontend/dist` from the build context, so an
#                      offline host never resolves node:22-alpine at all —
#                      BuildKit skips a stage no reachable stage copies from.
# `docs/DEPLOYMENT.md` §Airgapped drives this; `scripts/airgap-bundle.sh`
# builds the dist on the connected side.
ARG FRONTEND_STAGE=frontend-build

FROM node:22-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# No base image of its own — `scratch` is empty, so this stage costs nothing
# to resolve and carries only what the context already holds.
FROM scratch AS frontend-prebuilt
COPY frontend/dist /frontend/dist

# The selection happens here rather than at the `COPY --from` below, because
# `COPY --from=${VAR}` is not expanded by Docker's BuildKit ("variable
# expansion is not supported for --from, define a new stage with FROM using
# ARG from global scope as a workaround") — buildah/podman does expand it,
# which is why this only ever failed on the Docker path. `FROM ${VAR}` is
# expanded by every builder, and an alias stage costs nothing: it adds no
# layer, and the stage it does *not* alias stays unreachable and unresolved,
# which is the whole point of `frontend-prebuilt` for an offline host.
FROM ${FRONTEND_STAGE} AS frontend

FROM python:3.13-slim AS app
WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY README.md LICENSE ./
# INSTALL_EMBEDDINGS=1 adds the optional local-embedding stack (torch +
# sentence-transformers, ~2 GB) once it is an extra. Default off: without it
# the app serves everything except local embedding; point
# VESTIGO_EMBEDDING_API_BASE_URL at a remote endpoint for embedding features
# without the heavy install.
ARG INSTALL_EMBEDDINGS=0
RUN uv sync --frozen --no-dev $(test "$INSTALL_EMBEDDINGS" = "1" && echo "--extra embeddings")

COPY --from=frontend /frontend/dist ./frontend/dist

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
# Run uvicorn against the app factory directly (not the `vestigo-web` entry point) —
# that entry point rebuilds the frontend from source on startup, which this
# image doesn't carry (only the pre-built `frontend/dist`) or have node/npm for.
# `api.main` exposes only the `create_app()` factory (no module-level `app`),
# so uvicorn needs `--factory`.
CMD ["uvicorn", "--factory", "vestigo.api.main:create_app", "--host", "0.0.0.0", "--port", "8080"]
