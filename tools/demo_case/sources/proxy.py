"""Web proxy log, as a generic CSV export.

Baseline browsing is Zipf-weighted across a fixed set of business hosts with
log-normal transfer sizes, which is what gives the numeric detectors a
believable distribution to be surprised by. Three signals ride on top: the
near-periodic beacon, the slow bulk upload during exfil, and a single user
agent carrying characters nothing else in the corpus uses.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from tools.demo_case import scenario

PROXY_HEADER: tuple[str, ...] = (
    "datetime",
    "timestamp_desc",
    "source",
    "message",
    "client_ip",
    "user",
    "host",
    "url",
    "method",
    "status",
    "bytes_out",
    "bytes_in",
    "user_agent",
)

_BUSINESS_HOSTS = (
    "intranet.corp.local",
    "mail.corp.local",
    "sharepoint.corp.local",
    "jira.corp.local",
    "confluence.corp.local",
    "github.com",
    "docs.python.org",
    "stackoverflow.com",
    "news.ycombinator.com",
    "reuters.com",
    "bbc.co.uk",
    "linkedin.com",
    "salesforce.com",
    "office365.com",
    "zoom.us",
    "cdn.jsdelivr.net",
    "fonts.googleapis.com",
    "update.microsoft.com",
    "packages.debian.org",
    "pypi.org",
)

_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Edg/132.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "python-requests/2.32.3",
    "curl/8.9.1",
    "Microsoft-Delivery-Optimization/10.1",
    "Outlook/16.0 (Windows NT 10.0)",
    "Zoom/5.17.11",
)
#: Guillemets appear nowhere else in the corpus — the charset-novelty signal.
ODD_USER_AGENT = "Mozilla/5.0 «compatible» Sync/2.1 (build 4471)"

_JUMP_HOST_IP = "10.30.4.11"


def _row(
    moment,
    client_ip: str,
    user: str,
    host: str,
    url: str,
    method: str,
    status: int,
    bytes_out: int,
    bytes_in: int,
    user_agent: str,
) -> dict[str, str]:
    return {
        "datetime": moment.isoformat(),
        "timestamp_desc": "Content Modification Time",
        "source": "WEBHIST",
        "message": f"{method} {host}{url} {status} out={bytes_out} in={bytes_in}",
        "client_ip": client_ip,
        "user": user,
        "host": host,
        "url": url,
        "method": method,
        "status": str(status),
        "bytes_out": str(bytes_out),
        "bytes_in": str(bytes_in),
        "user_agent": user_agent,
    }


def _client_ips() -> dict[str, str]:
    """A stable workstation IP per account."""
    r = scenario.rng("proxy-clients")
    return {user: f"10.20.{r.randrange(1, 20)}.{r.randrange(2, 250)}" for user in scenario.USERS}


def _baseline() -> Iterator[dict[str, str]]:
    """Ordinary browsing: heavy tail on a few hosts, small uploads."""
    r = scenario.rng("proxy-baseline")
    ips = _client_ips()
    humans = [u for u in scenario.USERS if not u.startswith("svc_")]
    # Zipf-ish weights: the intranet dominates, the long tail is thin.
    weights = [1 / (i + 1) ** 0.9 for i in range(len(_BUSINESS_HOSTS))]
    for moment in scenario.walk(scenario.SCENARIO_START, scenario.SCENARIO_END, 2900, r):
        user = r.choice(humans)
        host = r.choices(_BUSINESS_HOSTS, weights=weights, k=1)[0]
        method = "POST" if r.random() < 0.12 else "GET"
        yield _row(
            moment,
            ips[user],
            user,
            host,
            r.choice(("/", "/index.html", "/api/v2/items", "/static/app.js", "/search?q=report")),
            method,
            r.choices((200, 304, 403, 404, 500), weights=(80, 12, 3, 4, 1), k=1)[0],
            max(120, int(r.lognormvariate(7.2, 0.7))),
            max(200, int(r.lognormvariate(10.2, 1.1))),
            r.choice(_USER_AGENTS),
        )


def _beacon() -> Iterator[dict[str, str]]:
    """Five-minute callbacks from the jump host, from the foothold onward."""
    r = scenario.rng("proxy-beacon")
    for moment in scenario.jittered_series(
        scenario.PHASES[1].start + timedelta(minutes=12),
        scenario.PHASES[3].end,
        interval=300,
        jitter=8,
        r=r,
    ):
        yield _row(
            moment,
            _JUMP_HOST_IP,
            scenario.COMPROMISED_USER,
            scenario.C2_HOST,
            "/api/telemetry/ping",
            "GET",
            200,
            r.randrange(280, 340),
            r.randrange(90, 160),
            ODD_USER_AGENT,
        )


def _exfil() -> Iterator[dict[str, str]]:
    """The upload: a resumable session, hundreds of multi-megabyte chunks.

    Chunked rather than a single POST because that is how bulk uploads
    actually look, and because a lone outlier row moves no destination share —
    the shift in *where* traffic goes is as much of the story as its size.
    """
    r = scenario.rng("proxy-exfil")
    moment = scenario.PHASES[3].start + timedelta(hours=1, minutes=52)
    for part in range(260):
        yield _row(
            moment,
            _JUMP_HOST_IP,
            scenario.COMPROMISED_USER,
            scenario.EXFIL_HOST,
            f"/upload/session/8fa21c/part-{part:03d}",
            "POST",
            200,
            r.randrange(8_000_000, 42_000_000),
            r.randrange(200, 900),
            ODD_USER_AGENT,
        )
        moment += timedelta(minutes=r.uniform(1.5, 6.0))


def proxy_rows() -> Iterator[dict[str, str]]:
    """Yield every proxy row, ascending by timestamp."""
    rows: list[dict[str, str]] = []
    for builder in (_baseline, _beacon, _exfil):
        rows.extend(builder())
    rows.sort(key=lambda r: r["datetime"])
    return iter(rows)


def write_proxy_csv(path: Path) -> int:
    """Write the CSV and return the row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PROXY_HEADER)
        writer.writeheader()
        for row in proxy_rows():
            writer.writerow(row)
            written += 1
    return written
