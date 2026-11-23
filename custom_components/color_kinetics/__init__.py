"""Philips Color Kinetics over KiNET."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .hub import ColorKineticsConfigEntry, ColorKineticsHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ColorKineticsConfigEntry
) -> bool:
    """Connect to the configured Data Enablers and expose their fixtures."""
    hub = ColorKineticsHub(hass, entry)
    await hub.async_start()
    entry.runtime_data = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    hub.initial_task = entry.async_create_background_task(
        hass,
        hub.async_initial_refresh(),
        "color_kinetics refresh",
    )
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
