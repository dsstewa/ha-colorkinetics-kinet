"""Philips Color Kinetics over KiNET."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import CONF_FOLLOW_ENTRY, DOMAIN, PLATFORMS
from .entry_data import follow_of
from .hub import STORAGE_VERSION, ColorKineticsConfigEntry, ColorKineticsHub
from .migrate import async_migrate_entry as async_migrate_entry
from .services import async_register_services, async_remove_services


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
        await hub.async_restore_panel()
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))
        hub.initial_task = entry.async_create_background_task(
            hass,
            hub.async_initial_refresh(),
            "color_kinetics refresh",
        )
        async_register_services(hass)
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
        if not hass.config_entries.async_loaded_entries(DOMAIN):
            async_remove_services(hass)
    return unload_ok


async def async_reload_entry(
    hass: HomeAssistant, entry: ColorKineticsConfigEntry
) -> None:
    """Reload after options change.

    Moving the default fade does not reload. The number entity writes that
    option often, and a reload would drop the socket and the frame loop.
    """
    hub = getattr(entry, "runtime_data", None)
    if hub is not None and hub.absorb_fade_option():
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored fixture colors and drop follow links that pointed here."""
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id or follow_of(other) != entry.entry_id:
            continue
        options = dict(other.options)
        options[CONF_FOLLOW_ENTRY] = ""
        hass.config_entries.async_update_entry(other, options=options)
    store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
    await store.async_remove()
