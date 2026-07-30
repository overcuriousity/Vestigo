"""Linux auth and syslog, as JSON Lines.

Free-text ``message`` strings with variable parts (pids, addresses, durations)
so structural clustering has something real to cluster. Three deliberate
shapes ride along with the ordinary churn:

* ``APP-01`` drifts against NTP, emitting records slightly out of order. It is
  benign, and being able to see that quickly is the point.
* the nightly backup on ``BACKUP-01`` moves from 02:15 to 03:40 mid-month —
  also benign, and a useful counterweight to the malicious beacon.
* sudo on the file server shifts toward archiving commands as the intruder
  stages data.

Rows are written in *arrival* order rather than by the stamp inside the record.
The two differ only for the clock-skew corrections, and that difference is the
whole point: it must survive to disk or the skew is invisible.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from vestigo.demo import scenario

_SSH_CLIENTS = ("10.20.4.17", "10.20.4.31", "10.20.9.8", "10.20.11.42", "10.30.2.5")
_ROUTINE_SUDO = (
    "systemctl status nginx",
    "journalctl -u sshd -n 200",
    "apt-get update",
    "df -h",
    "cat /var/log/app/service.log",
)
_STAGING_SUDO = (
    "tar -czf /srv/staging/finance.tar.gz /srv/shares/finance",
    "find /srv/shares -name '*.xlsx' -mtime -90",
    "rsync -a /srv/shares/finance/ /srv/staging/",
)


def _row(moment, hostname: str, program: str, pid: int, user: str, message: str) -> dict[str, str]:
    return {
        "datetime": moment.isoformat(),
        "timestamp": moment.isoformat(),
        "timestamp_desc": "Content Modification Time",
        "source": "LOG",
        "hostname": hostname,
        "program": program,
        "pid": str(pid),
        "user": user,
        "message": message,
    }


def _baseline() -> Iterator[dict[str, str]]:
    """sshd, sudo, cron and systemd churn across every server."""
    r = scenario.rng("linux-baseline")
    humans = [u for u in scenario.USERS if not u.startswith("svc_")]
    for moment in scenario.walk(scenario.SCENARIO_START, scenario.SCENARIO_END, 2500, r):
        host = r.choice(scenario.SERVERS)
        user = r.choice(humans)
        pid = r.randrange(400, 65_000)
        draw = r.random()
        if draw < 0.42:
            client = r.choice(_SSH_CLIENTS)
            yield _row(
                moment,
                host,
                "sshd",
                pid,
                user,
                f"Accepted publickey for {user} from {client} port {r.randrange(30000, 61000)} ssh2",
            )
        elif draw < 0.58:
            yield _row(
                moment,
                host,
                "sshd",
                pid,
                user,
                f"Disconnected from user {user} {r.choice(_SSH_CLIENTS)} port "
                f"{r.randrange(30000, 61000)}",
            )
        elif draw < 0.74:
            command = (
                r.choice(_STAGING_SUDO)
                if host == scenario.FILE_SERVER
                and moment >= scenario.PHASES[1].start
                and r.random() < 0.55
                else r.choice(_ROUTINE_SUDO)
            )
            yield _row(
                moment,
                host,
                "sudo",
                pid,
                user,
                f"pam_unix(sudo:session): session opened for user root by {user}: {command}",
            )
        elif draw < 0.88:
            yield _row(
                moment,
                host,
                "cron",
                pid,
                "root",
                f"CRON session opened for user root by (uid={r.choice((0, 1001, 1002))})",
            )
        else:
            unit = r.choice(("nginx.service", "app-worker.service", "node-exporter.service"))
            yield _row(
                moment,
                host,
                "systemd",
                pid,
                "root",
                f"Started {unit} after {r.uniform(0.1, 9.9):.2f}s",
            )


def _clock_skew() -> Iterator[dict[str, str]]:
    """APP-01's NTP drift: records stamped slightly in the past, then recovery.

    Emitted interleaved with that host's own ordering, so the source file
    genuinely contains descending timestamps rather than merely claiming to.
    """
    r = scenario.rng("linux-skew")
    moment = scenario.SCENARIO_START + timedelta(days=2, hours=4)
    while moment < scenario.SCENARIO_END:
        pid = r.randrange(400, 65_000)
        yield _row(
            moment,
            scenario.SKEWED_HOST,
            "chronyd",
            pid,
            "root",
            f"Selected source 10.30.0.9 offset {r.uniform(0.4, 9.5):.3f}s",
        )
        # The correction is *written* here but stamped behind the line before
        # it — the file keeps arrival order, so the record order really is
        # non-monotonic rather than merely claiming to be.
        correction = _row(
            moment - timedelta(seconds=r.uniform(2, 90)),
            scenario.SKEWED_HOST,
            "chronyd",
            pid,
            "root",
            f"System clock wrong by {r.uniform(-9.5, -0.4):.3f} seconds, adjustment started",
        )
        correction["_arrival"] = (moment + timedelta(milliseconds=1)).isoformat()
        yield correction
        moment += timedelta(hours=r.uniform(14, 30))


def _backup_job() -> Iterator[dict[str, str]]:
    """The nightly backup — 02:15 all month, then 03:40 from the foothold on."""
    r = scenario.rng("linux-backup")
    day = scenario.SCENARIO_START
    while day < scenario.SCENARIO_END:
        shifted = day >= scenario.PHASES[1].start
        hour, minute, jitter = (3, 40, 360) if shifted else (2, 15, 40)
        moment = day.replace(hour=hour, minute=minute) + timedelta(
            seconds=r.uniform(-jitter, jitter)
        )
        pid = r.randrange(400, 65_000)
        yield _row(
            moment,
            scenario.BACKUP_HOST,
            "backup",
            pid,
            "svc_backup",
            f"nightly backup run started target=/srv/shares retention={r.choice((7, 14, 30))}d",
        )
        yield _row(
            moment + timedelta(minutes=r.uniform(11, 26)),
            scenario.BACKUP_HOST,
            "backup",
            pid,
            "svc_backup",
            f"nightly backup run completed files={r.randrange(9000, 41000)} "
            f"bytes={r.randrange(10**9, 9 * 10**9)}",
        )
        day += timedelta(days=1)


def _intrusion() -> Iterator[dict[str, str]]:
    """The intruder's own Linux footprint, plus a template nobody has seen."""
    r = scenario.rng("linux-intrusion")
    lateral = scenario.PHASES[2]
    moment = lateral.start + timedelta(hours=2, minutes=20)
    while moment < scenario.PHASES[3].end:
        yield _row(
            moment,
            scenario.FILE_SERVER,
            "unattended-upgrade",
            r.randrange(400, 65_000),
            "root",
            "unattended-upgrade shim invoked package=telemetry-agent origin=local-file",
        )
        moment += timedelta(hours=r.uniform(5, 14))

    moment = lateral.start + timedelta(hours=1)
    while moment < scenario.PHASES[3].end:
        yield _row(
            moment,
            scenario.FILE_SERVER,
            "sshd",
            r.randrange(400, 65_000),
            scenario.COMPROMISED_USER,
            f"Accepted password for {scenario.COMPROMISED_USER} from 10.20.11.42 port "
            f"{r.randrange(30000, 61000)} ssh2",
        )
        moment += timedelta(minutes=r.uniform(25, 190))


def linux_rows() -> Iterator[dict[str, str]]:
    """Yield every Linux row in the order the collector received them.

    Ordered by *arrival* rather than by the stamp inside the record. For every
    row but the clock-skew corrections the two are the same; for those, arrival
    is later than the stamp, which is exactly what a drifting clock produces
    and what makes the ordering detector's finding real rather than staged.
    """
    rows: list[dict[str, str]] = []
    for builder in (_baseline, _clock_skew, _backup_job, _intrusion):
        rows.extend(builder())
    rows.sort(key=lambda r: r.get("_arrival") or r["timestamp"])
    for row in rows:
        row.pop("_arrival", None)
        yield row


def write_linux_jsonl(path: Path) -> int:
    """Write the JSONL file and return the row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in linux_rows():
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    return written
