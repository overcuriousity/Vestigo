"""GeoIP enricher: resolves IP-address attribute values to country/city via MaxMind GeoLite2.

Availability requires an admin-uploaded ``.mmdb`` database file (see
``api/routers/admin.py``'s upload endpoint). Since arbitrary ingested data has
no canonical "ip" field name, this enricher scans every attribute value that
matches the IPv4 pattern rather than a single hardcoded field — the job loop
(``enrichers/jobs.py``) is responsible for pairing each match back to the
source attribute key it came from.
"""

from __future__ import annotations

from pathlib import Path

import geoip2.database
import geoip2.errors

from tracesignal.core.config import get_settings
from tracesignal.enrichers.base import AvailabilityResult, Enricher

# IPv4 only for v1; IPv6 support is a documented follow-up.
IPV4_REGEX = (
    r"^(?:(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])$"
)


def geoip_database_path() -> Path:
    """Return the configured on-disk path for the GeoLite2 database file."""
    return Path(get_settings().enricher_data_path) / "geoip" / "GeoLite2-City.mmdb"


class GeoIPEnricher(Enricher):
    """Resolves public IP addresses to country/city via a local MaxMind GeoLite2 database."""

    key = "geoip"
    display_name = "GeoIP (MaxMind GeoLite2)"
    description = (
        "Resolves IP address attribute values to country and city using a "
        "locally uploaded MaxMind GeoLite2 City database."
    )
    eligibility_regex = IPV4_REGEX
    output_fields = ("geoip_country", "geoip_city")

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or geoip_database_path()
        self._reader: geoip2.database.Reader | None = None

    def check_availability(self) -> AvailabilityResult:
        if not self._db_path.exists():
            return AvailabilityResult(False, "GeoLite2 database not uploaded")
        try:
            with geoip2.database.Reader(str(self._db_path)):
                pass
        except Exception as exc:  # noqa: BLE001
            return AvailabilityResult(False, f"Database unreadable: {exc}")
        return AvailabilityResult(True)

    def _get_reader(self) -> geoip2.database.Reader:
        if self._reader is None:
            self._reader = geoip2.database.Reader(str(self._db_path))
        return self._reader

    def close(self) -> None:
        """Release the open database file handle, if any."""
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def enrich_value(self, raw_value: str) -> dict[str, str] | None:
        try:
            response = self._get_reader().city(raw_value)
        except (geoip2.errors.AddressNotFoundError, ValueError):
            return None
        country = response.country.name or ""
        city = response.city.name or ""
        if not country and not city:
            return None
        return {"geoip_country": country, "geoip_city": city}
