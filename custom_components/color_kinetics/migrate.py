"""Upgrade a 0.1.0 config entry to the 8-hex supply serial."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import CONF_SUPPLIES, DOMAIN
from .properties import canonical_supply_serial

_LOGGER = logging.getLogger(__name__)

# Current config-entry version. 0.1.0 entries are version 1.
ENTRY_VERSION = 2
ENTRY_MINOR_VERSION = 1
# Entry migration runs before the hub opens the cache, while the file is still
# the 0.1.x store (major version 1). The hub then migrates that file to
# STORAGE_VERSION, which adds looks. This Store stays on version 1 so it can
# read the file it is rewriting.
_STORAGE_VERSION = 1


def _migrate_supplies(
    container: dict[str, Any], replacements: dict[str, str]
) -> dict[str, Any]:
    """Copy ``container`` with supply serials rewritten. Updates ``replacements``."""
    raw = container.get(CONF_SUPPLIES)
    if not isinstance(raw, dict):
        return container
    supplies: dict[str, Any] = {}
    for host, info in raw.items():
        if not isinstance(info, dict):
            supplies[str(host)] = info
            continue
        updated = dict(info)
        old = updated.get("serial_hex")
        if isinstance(old, str):
            new = canonical_supply_serial(old)
            if new != old:
                replacements[old.strip().upper()] = new
                updated["serial_hex"] = new
        supplies[str(host)] = updated
    migrated = dict(container)
    migrated[CONF_SUPPLIES] = supplies
    return migrated


def _rewrite_unique_id(unique_id: str, replacements: dict[str, str]) -> str:
    """Map a supply id, or a ``serial:suffix`` id, onto the new serial."""
    if unique_id in replacements:
        return replacements[unique_id]
    for old, new in replacements.items():
        prefix = f"{old}:"
        if unique_id.startswith(prefix):
            return f"{new}{unique_id[len(old):]}"
    return unique_id


async def _migrate_store(
    hass: HomeAssistant, entry_id: str, replacements: dict[str, str]
) -> None:
    """Rewrite supply serials saved by the 0.1.0 runtime cache."""
    store: Store[dict[str, Any]] = Store(hass, _STORAGE_VERSION, f"{DOMAIN}_{entry_id}")
    stored = await store.async_load()
    if not isinstance(stored, dict):
        return
    supplies = stored.get("supplies")
    if not isinstance(supplies, dict):
        return
    changed = False
    for info in supplies.values():
        if not isinstance(info, dict):
            continue
        old = info.get("serial_hex")
        if not isinstance(old, str):
            continue
        new = canonical_supply_serial(old)
        if new != old:
            replacements[old.strip().upper()] = new
            info["serial_hex"] = new
            changed = True
    if changed:
        await store.async_save(stored)


def _migrate_registries(
    hass: HomeAssistant, entry_id: str, replacements: dict[str, str]
) -> None:
    """Point the existing supply device and its entities at the new serial."""
    if not replacements:
        return
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry_id):
        identifiers: set[tuple[str, str]] = set()
        changed = False
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier in replacements:
                identifiers.add((domain, replacements[identifier]))
                changed = True
            else:
                identifiers.add((domain, identifier))
        serial = device.serial_number
        if isinstance(serial, str) and serial.strip().upper() in replacements:
            serial = replacements[serial.strip().upper()]
            changed = True
        else:
            serial = None
        if not changed:
            continue
        kwargs: dict[str, Any] = {"new_identifiers": identifiers}
        if serial is not None:
            kwargs["serial_number"] = serial
        device_registry.async_update_device(device.id, **kwargs)

    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry_id):
        rewritten = _rewrite_unique_id(entity.unique_id, replacements)
        if rewritten != entity.unique_id:
            entity_registry.async_update_entity(entity.entity_id, new_unique_id=rewritten)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Rewrite 0.1.0 supply serials in place. Fixture serials stay as they are."""
    if entry.version > ENTRY_VERSION:
        return False
    if entry.version == ENTRY_VERSION and entry.minor_version >= ENTRY_MINOR_VERSION:
        return True

    _LOGGER.info(
        "Migrating Color Kinetics entry %s from version %s.%s to %s.%s",
        entry.entry_id,
        entry.version,
        entry.minor_version,
        ENTRY_VERSION,
        ENTRY_MINOR_VERSION,
    )
    replacements: dict[str, str] = {}
    data = _migrate_supplies(dict(entry.data), replacements)
    options = _migrate_supplies(dict(entry.options), replacements)
    await _migrate_store(hass, entry.entry_id, replacements)
    _migrate_registries(hass, entry.entry_id, replacements)
    unique_id = entry.unique_id
    if isinstance(unique_id, str):
        unique_id = _rewrite_unique_id(unique_id, replacements)
    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        unique_id=unique_id,
        version=ENTRY_VERSION,
        minor_version=ENTRY_MINOR_VERSION,
    )
    return True
