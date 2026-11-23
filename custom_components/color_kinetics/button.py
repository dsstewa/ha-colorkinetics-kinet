"""Rescan button, one per Data Enabler."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .hub import ColorKineticsConfigEntry, ColorKineticsHub, ScanInProgressError
from .protocol import KiNetError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ColorKineticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a rescan button for each configured Data Enabler."""
    hub = entry.runtime_data
    async_add_entities(RescanButton(hub, host) for host in hub.hosts)


class RescanButton(ButtonEntity):
    """Run one fixture scan. Discovery is not polled."""

    _attr_has_entity_name = True
    _attr_translation_key = "rescan"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:refresh"
    _attr_should_poll = False

    def __init__(self, hub: ColorKineticsHub, host: str) -> None:
        """Bind the button to one supply."""
        self._hub = hub
        self._host = host
        identifier = hub.supply_identifier(host)
        self._attr_unique_id = hub.button_unique_id(host)
        info = hub._supplies.get(host) or {}
        device = DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=hub.supply_name(host),
            manufacturer=info.get("manufacturer") or "Philips Color Kinetics",
            model=info.get("model") or "Data Enabler",
            sw_version=info.get("firmware") or None,
            serial_number=info.get("serial_hex"),
        )
        self._attr_device_info = device

    async def async_press(self) -> None:
        """Scan fixtures on this Data Enabler. Takes about 14 seconds."""
        try:
            await self._hub.async_rescan(self._host)
        except ScanInProgressError as err:
            raise HomeAssistantError(
                f"A fixture scan is already running on {self._host}.",
                translation_domain=DOMAIN,
                translation_key="scan_in_progress",
                translation_placeholders={"host": self._host},
            ) from err
        except KiNetError as err:
            self._hub._create_issue("scan_failed", self._host)
            raise HomeAssistantError(
                f"Fixture scan of {self._host} failed. Previous fixtures were kept."
            ) from err
