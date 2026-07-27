"""Load, validate, and apply the DB-backed settings layer.

``core/config.py`` owns the merge (environment wins, then these overrides);
this module owns the round-trip to Postgres and the validation that keeps a
bad stored value from taking the process down.

Two invariants matter here:

* **A stored override is never trusted blindly.** Every value goes through
  ``Settings`` validation before it is applied — on save (so the admin gets a
  422 instead of a broken instance) and again on load (so a row written by an
  older version, or by hand, degrades to "ignore that field" with a warning
  rather than crashing startup).
* **Environment pins are honoured at every step.** A field the operator set in
  the environment is refused by the settings API and skipped by the merge, so
  an override stored before the pin appeared can never resurface.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from vestigo.core.config import (
    Settings,
    env_pinned,
    get_base_settings,
    set_runtime_overrides,
)
from vestigo.core.settings_registry import editable_fields, secret_fields

logger = logging.getLogger(__name__)


class SettingsValidationError(ValueError):
    """A proposed override set does not validate against ``Settings``."""


def validate_overrides(values: dict[str, Any]) -> dict[str, Any]:
    """Validate candidate overrides against the full ``Settings`` model.

    Validating the *merged* object rather than field-by-field is what catches
    type coercion and the declared bounds (``ge``/``le``/``pattern``) with the
    same rules the environment layer gets. Returns the validated (coerced)
    values for the requested keys only.
    """
    editable = editable_fields()
    unknown = sorted(set(values) - editable)
    if unknown:
        raise SettingsValidationError(f"not settable via the settings API: {', '.join(unknown)}")

    base = get_base_settings()
    candidate = {**base.model_dump(), **values}
    try:
        validated = Settings.model_validate(candidate)
    except ValidationError as exc:
        raise SettingsValidationError(str(exc)) from exc
    return {key: getattr(validated, key) for key in values}


def _usable_overrides(stored: dict[str, Any]) -> dict[str, Any]:
    """Filter stored rows down to what may actually be applied.

    Drops env-pinned fields, fields no longer in the registry, secrets under
    ``secrets_mode=env-only``, and anything that fails validation — each with
    a warning, because a silently ignored override is a support call.
    """
    settings = get_base_settings()
    editable = editable_fields()
    secrets = secret_fields()
    usable: dict[str, Any] = {}
    for key, value in stored.items():
        if key not in editable:
            logger.warning("Ignoring stored setting %r: not an editable setting", key)
            continue
        if env_pinned(key):
            logger.info("Ignoring stored setting %r: pinned by VESTIGO_%s", key, key.upper())
            continue
        if key in secrets and settings.secrets_mode == "env-only":
            logger.warning("Ignoring stored secret %r: VESTIGO_SECRETS_MODE=env-only", key)
            continue
        usable[key] = value

    if not usable:
        return {}
    try:
        return validate_overrides(usable)
    except SettingsValidationError as exc:
        logger.warning("Stored settings failed validation, applying field by field: %s", exc)

    # One bad row must not discard every good one.
    accepted: dict[str, Any] = {}
    for key, value in usable.items():
        try:
            accepted.update(validate_overrides({key: value}))
        except SettingsValidationError as field_exc:
            logger.warning("Ignoring stored setting %r: %s", key, field_exc)
    return accepted


async def load_runtime_settings() -> dict[str, Any]:
    """Read overrides from Postgres and apply them to this process.

    Best-effort, like the agent resolver: an unreachable metadata store logs
    and leaves the environment layer in place rather than blocking startup.
    """
    try:
        from vestigo.api.deps import get_store

        rows = await get_store().list_app_settings()
    except Exception:
        logger.warning(
            "Could not read app_settings; running on environment defaults", exc_info=True
        )
        return {}
    applied = _usable_overrides({row.key: row.value for row in rows})
    set_runtime_overrides(applied)
    if applied:
        logger.info("Applied %d database-backed setting override(s)", len(applied))
    return applied


async def save_runtime_settings(values: dict[str, Any], updated_by: str | None) -> dict[str, Any]:
    """Validate, persist, and immediately apply an override change.

    ``None`` for a key clears that override (the row is deleted, the field
    falls back to environment-or-default). Raises
    :class:`SettingsValidationError` before writing anything if the resulting
    configuration would be invalid.
    """
    from vestigo.api.deps import get_store

    settings = get_base_settings()
    to_set = {k: v for k, v in values.items() if v is not None}
    pinned = sorted(k for k in values if env_pinned(k))
    if pinned:
        raise SettingsValidationError(
            "pinned by the environment, edit the deployment instead: " + ", ".join(pinned)
        )
    if settings.secrets_mode == "env-only":
        blocked = sorted(set(to_set) & secret_fields())
        if blocked:
            raise SettingsValidationError(
                "secret storage in the database is disabled (VESTIGO_SECRETS_MODE=env-only): "
                + ", ".join(blocked)
            )
    validated = validate_overrides(to_set) if to_set else {}

    payload: dict[str, Any] = {k: None for k in values if values[k] is None}
    payload.update(validated)
    await get_store().set_app_settings(payload, updated_by)
    return await load_runtime_settings()
