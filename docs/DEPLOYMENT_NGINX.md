# TLS reverse proxy (nginx)

TraceSignal (`tsig-web`) listens on plain HTTP, `0.0.0.0:8080`
(`src/tracesignal/web/app.py`). It has no TLS support of its own — put nginx in front of it
to terminate HTTPS for LAN/production use. Config: `docs/nginx-tls.conf`.

Certbot/Let's Encrypt is out of scope here (this host is airgapped/LAN-only per
`docs/TECH_STACK.md` §6). Use a self-signed cert instead.

## 1. Generate a self-signed certificate

```bash
sudo mkdir -p /etc/nginx/tls
sudo openssl req -x509 -nodes -newkey rsa:4096 \
  -keyout /etc/nginx/tls/tracesignal.key \
  -out    /etc/nginx/tls/tracesignal.crt \
  -days 825 \
  -subj "/CN=tracesignal.example.internal" \
  -addext "subjectAltName=DNS:tracesignal.example.internal,IP:192.168.18.125"
sudo chmod 600 /etc/nginx/tls/tracesignal.key
```

Adjust `-subj`/`subjectAltName` to your actual hostname/IP — browsers and most HTTP clients
enforce SAN matching, a bare CN is not enough anymore. Analysts' browsers will show a
self-signed warning on first visit (expected, click through / pin the cert); there's no CA
issuing it.

## 2. Install the nginx config

```bash
sudo cp docs/nginx-tls.conf /etc/nginx/sites-available/tracesignal.conf
sudo ln -s /etc/nginx/sites-available/tracesignal.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Edit `server_name` and the cert paths in the copied file to match your environment first.

## 3. Required TraceSignal-side settings

Once TLS terminates at nginx, the app itself still thinks it's plain HTTP — two settings need
to change (`TS_*` env vars, see `src/tracesignal/core/config.py`):

- `TS_AUTH_COOKIE_SECURE=1` — the session cookie's `Secure` flag defaults to `false`
  (`auth_cookie_secure`, dev default). Behind TLS this **must** be `1`, otherwise the session
  cookie is sent unflagged and a browser would still attach it over a stray HTTP request.
- `TS_ENVIRONMENT=production` — disables uvicorn's auto-reload watcher, which isn't wanted
  once nginx is fronting things for real use.

If OIDC SSO is enabled (`TS_OIDC_ENABLED=1`), also update `TS_OIDC_REDIRECT_URL` to the
`https://` form of your callback URL — the IdP redirect target must match what nginx exposes,
not `http://localhost:8080`.

## Notes on the proxy config

- `client_max_body_size 200G` — raised from nginx's 1 MiB default; Plaso CSV/JSONL source
  uploads can be large. Lower it to whatever ceiling your largest expected source needs.
- The SSE live-update stream (`api/routers/stream.py`, `GET /api/cases/{id}/stream`) gets a
  dedicated regex location with `proxy_buffering off` — buffering would delay/batch events
  until nginx's buffer fills, defeating the point of a live feed. The location is scoped to
  the exact `/stream` path (not all of `/api/cases/`) so the large source-upload endpoint
  keeps the 300s body/send timeouts from `location /` instead of nginx's 60s defaults. A 20s
  server-side keepalive is already built in (`_KEEPALIVE_SECONDS`), so `proxy_read_timeout
  3600s` just needs to outlive several keepalives, not be infinite.
- `X-Forwarded-For` is forwarded for logging only — TraceSignal deliberately does **not**
  trust it for access-control decisions (see comment in `api/routers/auth.py`), since this
  is meant to run on a LAN where the header would otherwise be attacker-controlled.
- No upstream `Connection: upgrade`/websocket handling is configured — the app has no
  WebSocket routes today (SSE only), so ordinary HTTP/1.1 keepalive is sufficient.
