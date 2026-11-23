"""Saved-look selector on each Data Enabler."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .hub import ColorKineticsConfigEntry, ColorKineticsHub
from .properties import SUFFIX_LOOK


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ColorKineticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one look selector per Data Enabler. Options are the saved names."""
    hub = entry.runtime_data
    async_add_entities(LookSelect(hub, host) for host in hub.hosts)


class LookSelect(SelectEntity):
    """Recall a saved look. The options are the look names."""

    _attr_has_entity_name = True
    _attr_translation_key = SUFFIX_LOOK
    _attr_icon = "mdi:palette"
    _attr_should_poll = False
    _attr_options: list[str]
    _attr_current_option: str | None = None

    def __init__(self, hub: ColorKineticsHub, host: str) -> None:
        """Bind the selector to one supply. Looks themselves are per entry."""
        self._hub = hub
        self._host = host
        self._unsub = None
        self._attr_unique_id = f"{hub.supply_identifier(host)}:{SUFFIX_LOOK}"
        self._attr_device_info = hub.supply_device_info(host)
        self._attr_options = hub.look_names()
        self._attr_current_option = hub.selected_look

    async def async_added_to_hass(self) -> None:
        """Follow saves, recalls, and deletes."""
        await super().async_added_to_hass()
        self._unsub = self._hub.add_look_listener(self.refresh)
        self.refresh()

    async def async_will_remove_from_hass(self) -> None:
        """Stop listening."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def refresh(self) -> None:
        """Publish the current look names and the last recalled name."""
        self._attr_device_info = self._hub.supply_device_info(self._host)
        self._attr_options = self._hub.look_names()
        selected = self._hub.selected_look
        self._attr_current_option = selected if selected in self._attr_options else None
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Recall the named look."""
        await self._hub.async_recall_look(option)
        self.refresh()
