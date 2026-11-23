"""Effect speed for All fixtures and for each named group."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_EFFECT_SPEED
from .hub import ColorKineticsConfigEntry, ColorKineticsHub
from .properties import SUFFIX_EFFECT_SPEED


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ColorKineticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one speed slider per All-fixtures light and per named group."""
    hub = entry.runtime_data
    entities: dict[str, EffectSpeed] = {}

    def _sync() -> None:
        added: list[EffectSpeed] = []
        for owner, kind in _owners(hub):
            unique_id = f"{owner}:{SUFFIX_EFFECT_SPEED}"
            entity = entities.get(unique_id)
            if entity is None:
                entity = EffectSpeed(hub, owner, unique_id, kind)
                entities[unique_id] = entity
                added.append(entity)
            elif entity.hass is not None:
                entity.refresh()
        if added:
            async_add_entities(added)

    entry.async_on_unload(hub.add_listener(_sync))
    _sync()


def _owners(hub: ColorKineticsHub) -> list[tuple[str, str]]:
    owners = [(hub.all_fixtures_owner(host), "supply") for host in hub.hosts]
    owners.extend((hub.group_owner(group["id"]), "group") for group in hub.light_groups())
    return owners


class EffectSpeed(RestoreNumber, NumberEntity):
    """Cycles per the effect slider. 1 takes 6 seconds, 10 takes 0.6 seconds."""

    _attr_native_min_value = 1
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_should_poll = False
    _attr_icon = "mdi:speedometer"
    _attr_has_entity_name = True

    def __init__(
        self, hub: ColorKineticsHub, owner: str, unique_id: str, kind: str
    ) -> None:
        """Bind the slider to one effect owner."""
        self._hub = hub
        self._owner = owner
        self._kind = kind
        self._attr_unique_id = unique_id
        self._attr_native_value = float(hub.effect_speed(owner) or DEFAULT_EFFECT_SPEED)
        self._apply_identity()

    def _apply_identity(self) -> None:
        if self._kind == "supply":
            host = next(
                (host for host in self._hub.hosts if self._hub.all_fixtures_owner(host) == self._owner),
                self._hub.hosts[0] if self._hub.hosts else "",
            )
            self._attr_translation_key = SUFFIX_EFFECT_SPEED
            self._attr_name = None
            if host:
                self._attr_device_info = self._hub.supply_device_info(host)
            return
        group_id = self._owner.split(":", 1)[1]
        group = next((item for item in self._hub.light_groups() if item["id"] == group_id), None)
        name = group["name"] if group else "Group"
        self._attr_translation_key = None
        self._attr_name = f"{name} effect speed"
        if group is not None:
            self._attr_device_info = self._hub.group_device_info(group)

    async def async_added_to_hass(self) -> None:
        """Restore the last slider position."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = float(last.native_value)
            self._hub.set_effect_speed(self._owner, self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Store the speed. A running effect uses it on the next frame."""
        self._attr_native_value = float(value)
        self._hub.set_effect_speed(self._owner, float(value))
        self.async_write_ha_state()

    def refresh(self) -> None:
        """Follow a renamed group or a replaced supply device."""
        self._apply_identity()
        if self.hass is not None:
            self.async_write_ha_state()
