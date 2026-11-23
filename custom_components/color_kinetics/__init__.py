"""Philips Color Kinetics over KiNET."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, PLATFORMS
from .hub import STORAGE_VERSION, ColorKineticsConfigEntry, ColorKineticsHub
from .migrate import async_migrate_entry as async_migrate_entry


async def async_setup_entry(
    hass: HomeAssistant, entry: ColorKineticsConfigEntry
) -> bool:
    """Connect to the configured Data Enablers and expose their fixtures."""
    hub = ColorKineticsHub(hass, entry)
    try:
        await hub.async_start()
        entry.runtime_data = hub
        # Unload (including a failed setup) closes the socket and the frame task.
        entry.async_on_unload(hub.async_stop)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))
        hub.initial_task = entry.async_create_background_task(
            hass,
            hub.async_initial_refresh(),
            "color_kinetics refresh",
        )
    except Exception:
        await hub.async_stop()
        raise
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ColorKineticsConfigEntry
) -> bool:
    """Stop sending and remove entities."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok


async def async_reload_entry(
    hass: HomeAssistant, entry: ColorKineticsConfigEntry
) -> None:
    """Reload after options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored fixture colors with the config entry."""
    store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
    await store.async_remove()
