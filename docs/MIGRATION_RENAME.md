# Migration: TraceVector → TraceSignal rename

The project was renamed from **TraceVector** to **TraceSignal** (issue #5). The rename is a
hard cutover — there are no compatibility shims. Existing deployments need the following
one-time adjustments.

## Environment variables: `TV_*` → `TS_*`

All configuration variables changed prefix. Rename every `TV_*` variable in your `.env` /
service environment to `TS_*` (values unchanged), e.g.:

| Old | New |
|---|---|
| `TV_POSTGRES_URL` | `TS_POSTGRES_URL` |
| `TV_CLICKHOUSE_DATABASE` | `TS_CLICKHOUSE_DATABASE` |
| `TV_QDRANT_COLLECTION_PREFIX` | `TS_QDRANT_COLLECTION_PREFIX` |
| `TV_ALLOW_ONLINE` | `TS_ALLOW_ONLINE` |
| `TV_OIDC_ENABLED` | `TS_OIDC_ENABLED` |

`TV_*` variables are silently ignored after the rename — a missed variable falls back to its
default, so review your full set (see `.env.example`).

## Existing databases: no rename required

Only the **defaults** changed (`tracevector` → `tracesignal` for the Postgres user/database,
ClickHouse database, and Qdrant collection prefix). Existing data stays where it is — point
the new variables at the old names explicitly:

```bash
TS_POSTGRES_URL=postgresql+asyncpg://tracevector:...@localhost/tracevector
TS_CLICKHOUSE_DATABASE=tracevector
TS_QDRANT_COLLECTION_PREFIX=tracevector
```

Fresh deployments use the new `tracesignal` defaults from `.env.example` /
`docker-compose.yml`.

## CLI entry points

- `uv run tv ...` → `uv run tsig ...`
- `uv run tv-web` → `uv run tsig-web`

## Browser-local UI preferences

The frontend's `localStorage` keys changed (`tv-theme`/`tv-ui`/`tv-auth` →
`tsig-theme`/`tsig-ui`/`tsig-auth`). Theme and UI layout preferences reset once per browser;
users are logged out once and log back in. No data is affected.

## GitHub repository

`ScalarForensic/TraceVector` → `ScalarForensic/TraceSignal` (manual rename in GitHub
settings). GitHub redirects the old URL, so existing clones keep working; update remotes at
leisure:

```bash
git remote set-url origin git@github.com:ScalarForensic/TraceSignal.git
```

Delete this file once all known deployments have migrated.
