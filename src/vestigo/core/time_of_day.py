"""What the time-of-day habit detector (D12) accepts — shared by it and the settings.

One definition, so a value an admin can save in the web console is exactly a
value the detector runs with: the settings layer validating a looser shape
would store a default every run of the detector then refuses.
"""

from __future__ import annotations

import re
import zoneinfo

# The bucket resolutions the day can be cut into. Each divides 1440, so the
# last bucket ends exactly at midnight and the circular distance is well
# defined.
HABIT_BUCKET_MINUTES = (15, 30, 60, 120, 180, 240)

# What an IANA zone name may look like before zoneinfo is asked about it. The
# name is inlined into SQL (ClickHouse takes a timezone as a constant), so the
# token pattern is the first line of defence and zoneinfo the second.
_TZ_TOKEN = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}$")


def validate_bucket_minutes(minutes: int) -> int:
    """Return *minutes* if it is a habit bucket width; raise ``ValueError`` otherwise."""
    if minutes not in HABIT_BUCKET_MINUTES:
        raise ValueError(
            f"bucket_minutes must be one of {', '.join(str(b) for b in HABIT_BUCKET_MINUTES)}"
        )
    return minutes


def validate_timezone(name: str) -> str:
    """Return *name* if it is an IANA zone this host knows; raise ``ValueError`` otherwise."""
    if not isinstance(name, str) or not _TZ_TOKEN.match(name):
        raise ValueError("timezone must be an IANA zone name such as 'UTC' or 'Europe/Berlin'")
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"timezone {name!r} is not a known IANA zone name") from exc
    return name
