"""Rescan button, one per Data Enabler."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .hub import ColorKineticsConfigEntry, ColorKineticsHub, ScanInProgressError
from .properties import SUFFIX_ALL_OFF, SUFFIX_STOP_EFFECT
from .protocol import KiNetError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ColorKineticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add rescan, stop, and all-off buttons for each configured Data Enabler."""
    hub = entry.runtime_data
    async_add_entities(
        entity
        for host in hub.hosts
        for entity in (
            RescanButton(hub, host),
            StopEffectButton(hub, host),
            AllOffButton(hub, host),
        )
    )


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
        self._attr_unique_id = hub.button_unique_id(host)
        self._attr_device_info = hub.supply_device_info(host)

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
        except (KiNetError, OSError) as err:
            self._hub._create_issue("scan_failed", self._host)
            raise HomeAssistantError(
                f"Fixture scan of {self._host} failed. Previous fixtures were kept."
            ) from err


class StopEffectButton(ButtonEntity):
    """Stop the panel effect and leave the current colors on the wire."""

    _attr_has_entity_name = True
    _attr_translation_key = SUFFIX_STOP_EFFECT
    _attr_icon = "mdi:stop"
    _attr_should_poll = False

    def __init__(self, hub: ColorKineticsHub, host: str) -> None:
        """Bind the button to one supply."""
        self._hub = hub
        self._host = host
        self._attr_unique_id = f"{hub.supply_identifier(host)}:{SUFFIX_STOP_EFFECT}"
        self._attr_device_info = hub.supply_device_info(host)

    async def async_press(self) -> None:
        """Stop the effect on the panel target."""
        await self._hub.async_stop_effect(self._host)


class AllOffButton(ButtonEntity):
    """Turn the panel target off through the normal light path."""

    _attr_has_entity_name = True
    _attr_translation_key = SUFFIX_ALL_OFF
    _attr_icon = "mdi:lightbulb-off"
    _attr_should_poll = False

    def __init__(self, hub: ColorKineticsHub, host: str) -> None:
        """Bind the button to one supply."""
        self._hub = hub
        self._host = host
        self._attr_unique_id = f"{hub.supply_identifier(host)}:{SUFFIX_ALL_OFF}"
        self._attr_device_info = hub.supply_device_info(host)

    async def async_press(self) -> None:
        """Turn every fixture in the panel target off."""
        await self._hub.async_all_off(self._host)
