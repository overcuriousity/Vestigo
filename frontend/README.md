# TraceSignal Frontend

React 19 + TypeScript + Vite SPA for the TraceSignal forensic log-analysis tool.

## Development

```bash
# Start backend (from repo root)
uv run tsig-web                    # FastAPI on :8080
docker compose up -d             # backing services

# Start frontend dev server
npm install
npm run dev                      # Vite on :5173, proxies /api → :8080
```

## Production build

```bash
npm run build      # outputs to frontend/dist/
```

Copy `frontend/dist/` to your server and use the nginx config at `../deploy/nginx.conf`.

## Type checking & tests

```bash
npm run typecheck  # tsc --noEmit
npm run test       # vitest unit tests
```

## Airgap notes

All assets (JS, CSS, fonts, icons) are bundled — no CDN or runtime external calls.
Confirm after build: check DevTools Network for any cross-origin requests.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the Oxlint configuration

If you are developing a production application, we recommend enabling type-aware lint rules by installing `oxlint-tsgolint` and editing `.oxlintrc.json`:

```json
{
  "$schema": "./node_modules/oxlint/configuration_schema.json",
  "plugins": ["react", "typescript", "oxc"],
  "options": {
    "typeAware": true
  },
  "rules": {
    "react/rules-of-hooks": "error",
    "react/only-export-components": ["warn", { "allowConstantExport": true }]
  }
}
```

See the [Oxlint rules documentation](https://oxc.rs/docs/guide/usage/linter/rules) for the full list of rules and categories.
