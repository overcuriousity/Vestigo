"""ASN enricher: resolves IP-address attribute values to network operator via MaxMind GeoLite2-ASN.

Availability requires an admin-uploaded ``.mmdb`` database file (see
``api/routers/admin.py``'s upload endpoint). Since arbitrary ingested data has
no canonical "ip" field name, this enricher scans every attribute value that
matches the IP pattern rather than a single hardcoded field — the job loop
(``enrichers/jobs.py``) is responsible for pairing each match back to the
source attribute key it came from.

Scope: GeoLite2-ASN yields the *announcing* AS number and organization —
usually the hosting or connectivity provider operating the address. It is NOT
whois: no netname, registrant, abuse contact, or allocation dates. RIR whois
needs network access and is deliberately out of scope for an enricher.

Self-contained by design: enrichers are modular demonstration/reference
plugins (like the ingestion converters) and intentionally do not share code —
the MaxMind mechanics below mirror ``enrichers/geoip.py`` rather than import
from it.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from pathlib import Path
from typing import Any

import geoip2.database
import geoip2.errors
import maxminddb

from vestigo.core.config import get_settings
from vestigo.enrichers.base import (
    AssetSpec,
    AssetValidationError,
    AvailabilityResult,
    Enricher,
)
from vestigo.ingestion.files import hash_file

logger = logging.getLogger(__name__)

# This is the *eligibility pattern*, not a validator: it is pushed into
# ClickHouse match() by check_eligibility and reused as the cheap per-value
# gate in is_field_eligible, so it must stay a ClickHouse-compatible re2
# regex. Correctness validation happens via stdlib ipaddress in enrich_value.
# The IPv6 arm is deliberately loose (re2 has no lookarounds/backrefs); it
# covers full/compressed forms and IPv4-mapped tails like ::ffff:1.2.3.4.
IP_REGEX = (
    r"^(?:(?:(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
    r"|(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}"
    r"|(?:[0-9a-fA-F]{0,4}:){1,6}(?:(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9]))$"
)

# Single source for the output-field names: the `output_fields` contract tuple
# and enrich_value's return-dict keys must always agree. Order is part of
# config_hash() — never reorder.
_FIELD_NUMBER = "asn_number"
_FIELD_ORG = "asn_org"


def asn_database_path() -> Path:
    """Return the configured on-disk path for the GeoLite2-ASN database file."""
    return Path(get_settings().enricher_data_path) / "asn" / "GeoLite2-ASN.mmdb"


def asn_sidecar_path(db_path: Path | None = None) -> Path:
    """Return the metadata sidecar path recorded alongside the ``.mmdb`` at upload time."""
    base = db_path or asn_database_path()
    return base.with_name(base.name + ".meta.json")


def write_asn_sidecar(db_path: Path, metadata: dict[str, Any]) -> None:
    """Atomically write the database-identity sidecar next to the ``.mmdb`` file."""
    sidecar = asn_sidecar_path(db_path)
    tmp = sidecar.with_suffix(".tmp")
    tmp.write_text(json.dumps(metadata, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(tmp, sidecar)


def read_asn_sidecar(db_path: Path) -> dict[str, Any] | None:
    """Return the sidecar's metadata dict, or None if missing/unreadable."""
    sidecar = asn_sidecar_path(db_path)
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class ASNEnricher(Enricher):
    """Resolves public IP addresses to AS number/organization via a local GeoLite2-ASN database."""

    key = "asn"
    display_name = "ASN (MaxMind GeoLite2)"
    description = (
        "Resolves IP address attribute values to the announcing AS number and "
        "organization — usually the hosting/connectivity provider — using a "
        "locally uploaded MaxMind GeoLite2-ASN database. Not whois: no netname, "
        "registrant, abuse contact, or allocation dates."
    )
    eligibility_regex = IP_REGEX
    output_fields = (_FIELD_NUMBER, _FIELD_ORG)
    asset_spec = AssetSpec(
        name="GeoLite2 ASN database",
        description=(
            "MaxMind GeoLite2-ASN database (.mmdb). Download from maxmind.com "
            "(free with a GeoLite2 account) and upload here; the ASN flavor is "
            "required — City/Country databases are rejected."
        ),
        file_extensions=(".mmdb",),
    )

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path_override = db_path
        self._reader: geoip2.database.Reader | None = None
        # Set by _pin() on a spawned per-run instance: the identity (hash +
        # build metadata) of the exact database bytes this run reads, captured
        # once so config_hash() and every lookup describe the same file even if
        # an admin replaces the .mmdb mid-run. None on the registry singleton
        # (which never enriches), where config_extras() falls back to the
        # current sidecar/file.
        self._pinned_meta: dict[str, Any] | None = None

    @property
    def _db_path(self) -> Path:
        # Resolved lazily (not captured in __init__) so the long-lived
        # registry instance follows configuration/test changes to
        # asn_database_path() instead of freezing the import-time value.
        return self._db_path_override or asn_database_path()

    def spawn(self) -> ASNEnricher:
        """Fresh per-run instance with its database bytes pinned at spawn time.

        Opens the ``.mmdb`` once and reads it fully into memory (``MODE_FD``),
        hashing the *same* file object so the reader and the recorded
        ``config_hash`` identity are guaranteed to describe identical bytes.
        This closes the forensic-provenance gap that a lazily-opened reader
        left: without pinning, an admin replacing the database between job
        start and the first lookup would produce values from the new database
        stamped with the old database's hash. Best-effort — if the file can't
        be opened/read, the instance falls back to a lazy path-based reader and
        the sidecar-derived identity (prior behavior).
        """
        inst = ASNEnricher(db_path=self._db_path_override)
        inst._pin()
        return inst

    def _pin(self) -> None:
        """Read the database into memory once and capture its exact identity."""
        path = self._db_path
        try:
            fh = path.open("rb")
        except OSError as exc:
            logger.warning(
                "Could not open ASN database %s to pin it at spawn (%s); falling back to lazy open",
                path,
                exc,
            )
            return
        try:
            # hash_file reads the stream and rewinds it to the start, so the
            # Reader below consumes the identical bytes we just hashed. MODE_FD
            # loads the whole database into memory, so the fd can be closed
            # afterward and a later on-disk replacement cannot affect this run.
            sha256 = hash_file(fh)
            reader = geoip2.database.Reader(fh, mode=maxminddb.MODE_FD)
            reader_meta = reader.metadata()
            self._reader = reader
            self._pinned_meta = {
                "sha256": sha256,
                "build_epoch": reader_meta.build_epoch,
                "database_type": reader_meta.database_type,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not pin ASN database %s at spawn (%s); falling back to lazy open",
                path,
                exc,
            )
            self._reader = None
            self._pinned_meta = None
        finally:
            fh.close()

    def check_availability(self) -> AvailabilityResult:
        if not self._db_path.exists():
            return AvailabilityResult(False, "GeoLite2-ASN database not uploaded")
        # Sidecar-first: the .meta.json is written atomically with the install
        # (upload endpoint), so its database_type is trustworthy — no need to
        # mmap the whole .mmdb just to read metadata. Opening a Reader is the
        # fallback for pre-sidecar (hand-copied) installs only.
        meta = read_asn_sidecar(self._db_path)
        if meta and meta.get("database_type"):
            database_type = str(meta["database_type"])
        else:
            try:
                with geoip2.database.Reader(str(self._db_path)) as reader:
                    database_type = reader.metadata().database_type
            except Exception as exc:  # noqa: BLE001
                return AvailabilityResult(False, f"Database unreadable: {exc}")
        if "ASN" not in database_type:
            return AvailabilityResult(
                False,
                f"Wrong database flavor: {database_type!r}; an ASN database is required",
            )
        return AvailabilityResult(True)

    def asset_status(self) -> dict[str, Any] | None:
        """Installed-database state for the admin UI, read from file + sidecar."""
        if not self._db_path.exists():
            return {"uploaded": False, "size_bytes": None, "detail": {}}
        return {
            "uploaded": True,
            "size_bytes": self._db_path.stat().st_size,
            "detail": read_asn_sidecar(self._db_path) or {},
        }

    def install_asset(self, tmp_path: Path, sha256: str) -> dict[str, Any]:
        """Validate an uploaded ``.mmdb`` and atomically install it.

        Confirms the file opens as a real GeoLite2 database *and* is
        ASN-flavored (``enrich_value`` calls ``.asn()``, which a City/Country
        database would blow up on mid-job), then replaces any previous
        database and records its identity in the metadata sidecar (hash +
        build epoch, the inputs to ``config_hash()``).
        """
        from datetime import UTC, datetime

        try:
            with geoip2.database.Reader(str(tmp_path)) as reader:
                meta = reader.metadata()
        except Exception as exc:  # noqa: BLE001
            raise AssetValidationError(f"Invalid GeoLite2 database: {exc}") from exc
        if "ASN" not in meta.database_type:
            raise AssetValidationError(
                f"Wrong database flavor: {meta.database_type!r}; an ASN database is "
                "required (the ASN enricher performs asn lookups)"
            )
        sidecar = {
            "sha256": sha256,
            "build_epoch": meta.build_epoch,
            "database_type": meta.database_type,
            "uploaded_at": datetime.now(UTC).isoformat(),
        }
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # tmp_path is under the system temp dir, which may be a different
        # filesystem than the target data dir — Path.replace() (os.rename)
        # fails cross-device, so use shutil.move (copy+unlink fallback).
        import shutil

        shutil.move(str(tmp_path), str(self._db_path))
        # Sidecar must exist before availability is refreshed, so a job
        # triggered right after this upload hashes against the new metadata.
        write_asn_sidecar(self._db_path, sidecar)
        return sidecar

    def config_extras(self) -> dict[str, Any]:
        """Identity of the installed database file (hash + build metadata).

        On a spawned per-run instance, returns the identity pinned at
        ``spawn()`` from the exact bytes this run reads — never the sidecar,
        which an admin could have rewritten to a newer database mid-run. On the
        unpinned registry singleton, falls back to the sidecar written at
        upload time; for databases installed before the sidecar existed,
        compute it once and persist it (best effort — a read-only data dir just
        means recomputing next time).
        """
        if self._pinned_meta is not None:
            return {
                "database_sha256": self._pinned_meta.get("sha256", ""),
                "build_epoch": self._pinned_meta.get("build_epoch", 0),
                "database_type": self._pinned_meta.get("database_type", ""),
            }
        meta = read_asn_sidecar(self._db_path)
        if meta is None:
            with geoip2.database.Reader(str(self._db_path)) as reader:
                reader_meta = reader.metadata()
                meta = {
                    "sha256": hash_file(self._db_path),
                    "build_epoch": reader_meta.build_epoch,
                    "database_type": reader_meta.database_type,
                }
            try:
                write_asn_sidecar(self._db_path, meta)
            except OSError:
                logger.warning("Could not persist ASN metadata sidecar next to %s", self._db_path)
        return {
            "database_sha256": meta.get("sha256", ""),
            "build_epoch": meta.get("build_epoch", 0),
            "database_type": meta.get("database_type", ""),
        }

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
        """Resolve one IP value, or None for invalid input / no ASN match.

        Only an invalid address or a legitimate lookup miss maps to None —
        reader failures (closed handle, corrupt database) propagate so the
        job fails loudly instead of silently producing incomplete results.
        """
        try:
            ipaddress.ip_address(raw_value)
        except ValueError:
            return None
        try:
            response = self._get_reader().asn(raw_value)
        except geoip2.errors.AddressNotFoundError:
            return None
        number = response.autonomous_system_number
        org = response.autonomous_system_organization or ""
        if number is None and not org:
            return None
        return {
            _FIELD_NUMBER: str(number) if number is not None else "",
            _FIELD_ORG: org,
        }
