"""The fabricated incident: dates, cast, and shared randomness.

Everything the generator produces derives from this module, and every value
here is fixed. The archive must be reproducible from a clean checkout, so there
is no wall-clock time, no ``random`` global, and no environment input anywhere
in the generator.

Randomness is drawn per named stream (``rng("proxy")``) rather than from one
shared generator: adding or reordering a source then cannot shift the output of
every other source, which keeps regeneration diffs readable.

Scenario: the contractor account m.okonkwo is sprayed from a VPN pool, gains a
foothold on the jump host, a service is installed for persistence, the account
moves laterally to the file server, an archive is staged, and it is uploaded in
slow chunks to a file-sharing host.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SCENARIO_START = datetime(2026, 5, 1, tzinfo=UTC)
SCENARIO_END = datetime(2026, 5, 30, 23, 59, 59, tzinfo=UTC)
#: Days 1–23 are the analyst's baseline; the intrusion starts here.
BASELINE_END = datetime(2026, 5, 24, tzinfo=UTC)


@dataclass(frozen=True)
class Phase:
    """One labeled suspect window of the intrusion."""

    key: str
    label: str
    start: datetime
    end: datetime


PHASES: tuple[Phase, ...] = (
    Phase(
        "recon",
        "Credential spray",
        datetime(2026, 5, 24, tzinfo=UTC),
        datetime(2026, 5, 25, 12, tzinfo=UTC),
    ),
    Phase(
        "foothold",
        "Foothold and persistence",
        datetime(2026, 5, 25, 12, tzinfo=UTC),
        datetime(2026, 5, 27, tzinfo=UTC),
    ),
    Phase(
        "lateral",
        "Lateral movement",
        datetime(2026, 5, 27, tzinfo=UTC),
        datetime(2026, 5, 29, tzinfo=UTC),
    ),
    Phase(
        "exfil",
        "Staging and exfiltration",
        datetime(2026, 5, 29, tzinfo=UTC),
        datetime(2026, 5, 30, 22, tzinfo=UTC),
    ),
)

PHASES_BY_KEY: dict[str, Phase] = {p.key: p for p in PHASES}

WORKSTATIONS: tuple[str, ...] = tuple(f"WKS-{i:03d}" for i in range(1, 13))
SERVERS: tuple[str, ...] = ("DC-01", "DC-02", "JUMP-01", "FILE-01", "APP-01", "BACKUP-01")
USERS: tuple[str, ...] = (
    "a.lindqvist",
    "b.moreau",
    "c.nakamura",
    "d.oyelaran",
    "e.silva",
    "f.haddad",
    "g.petrov",
    "h.ivanova",
    "m.okonkwo",
    "svc_backup",
    "svc_scan",
)

COMPROMISED_USER = "m.okonkwo"
#: The one workstation the contractor habitually uses — every other host they
#: appear on during the intrusion is a new (user, host) pair.
COMPROMISED_HOME_WORKSTATION = "WKS-004"
#: Where the spray comes from: a workstation on the contractor VPN pool.
SPRAY_SOURCE_WORKSTATION = "WKS-011"
JUMP_HOST = "JUMP-01"
FILE_SERVER = "FILE-01"
BACKUP_HOST = "BACKUP-01"
#: The upload destination during exfil.
EXFIL_HOST = "files.transferzone-cdn.net"
#: The beacon destination — a long random-looking subdomain, on purpose.
C2_HOST = "x7fq2m9v4kz1p8rt.updates-telemetry.net"
#: The Linux host whose clock drifts, producing benign out-of-order records.
SKEWED_HOST = "APP-01"

_SEED = 0x5645_5354  # "VEST"


def rng(stream: str) -> random.Random:
    """Return a reproducible generator for one named stream.

    Args:
        stream: Stream name; independent streams never influence each other.
    """
    return random.Random(f"{_SEED}:{stream}")


#: Hour-of-day weights for a working population: quiet overnight, peaking
#: mid-morning and mid-afternoon with a lunch dip.
_HOUR_WEIGHTS = (1, 1, 1, 1, 1, 2, 4, 9, 18, 24, 26, 22, 18, 22, 26, 24, 18, 11, 7, 5, 4, 3, 2, 1)


def walk(start: datetime, end: datetime, per_day: int, r: random.Random) -> Iterator[datetime]:
    """Yield sorted timestamps at roughly ``per_day`` per day, weighted to work hours.

    Weekends get about a fifth of the volume. Without this shape every
    frequency and cadence detector sees a flat synthetic baseline, and the
    histogram in the Explorer looks obviously fake at a glance.

    Args:
        start: First instant of the range (inclusive).
        end: Last instant of the range (exclusive).
        per_day: Target weekday volume.
        r: The caller's stream, so volume draws stay reproducible.
    """
    stamps: list[datetime] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        weekend = day.weekday() >= 5
        count = max(1, int(r.gauss(per_day * (0.2 if weekend else 1.0), per_day * 0.12)))
        for _ in range(count):
            hour = r.choices(range(24), weights=_HOUR_WEIGHTS, k=1)[0]
            moment = day + timedelta(hours=hour, minutes=r.randrange(60), seconds=r.randrange(60))
            if start <= moment < end:
                stamps.append(moment)
        day += timedelta(days=1)
    yield from sorted(stamps)


def jittered_series(
    start: datetime, end: datetime, interval: float, jitter: float, r: random.Random
) -> Iterator[datetime]:
    """Yield a near-periodic series — a beacon, or a scheduled job.

    Args:
        start: When the series begins.
        end: When it stops (exclusive).
        interval: Mean seconds between events.
        jitter: Maximum absolute deviation from the mean, in seconds.
        r: The caller's stream.
    """
    moment = start
    while moment < end:
        yield moment
        moment += timedelta(seconds=interval + r.uniform(-jitter, jitter))


def in_phase(moment: datetime, key: str) -> bool:
    """Whether ``moment`` falls inside the named phase."""
    phase = PHASES_BY_KEY[key]
    return phase.start <= moment < phase.end
