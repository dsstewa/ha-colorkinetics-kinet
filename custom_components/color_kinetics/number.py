"""Effect speed and intensity for All fixtures and for each named group."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_DEFAULT_FADE, DEFAULT_EFFECT_INTENSITY, DEFAULT_EFFECT_SPEED
from .entry_data import clamp_fade_seconds
from .hub import ColorKineticsConfigEntry, ColorKineticsHub
from .properties import (
    SUFFIX_DEFAULT_FADE,
    SUFFIX_EFFECT_INTENSITY,
    SUFFIX_EFFECT_SPEED,
    SUFFIX_PANEL_BRIGHTNESS,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ColorKineticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add speed and intensity sliders per All-fixtures light and per named group."""
    hub = entry.runtime_data
    entities: dict[str, EffectSpeed | EffectIntensity] = {}
    fades: dict[str, DefaultFade] = {}
    brightness: dict[str, PanelBrightness] = {}

    def _sync() -> None:
        added: list[EffectSpeed | EffectIntensity | DefaultFade | PanelBrightness] = []
        for owner, kind in _owners(hub):
            for factory, suffix, registry in (
                (EffectSpeed, SUFFIX_EFFECT_SPEED, hub.speed_entities),
                (EffectIntensity, SUFFIX_EFFECT_INTENSITY, hub.intensity_entities),
            ):
                unique_id = f"{owner}:{suffix}"
                entity = entities.get(unique_id)
                if entity is None:
                    entity = factory(hub, owner, unique_id, kind)
                    entities[unique_id] = entity
                    added.append(entity)
                elif entity.hass is not None:
                    entity.refresh()
                registry[owner] = entity
        for host in hub.hosts:
            unique_id = f"{hub.supply_identifier(host)}:{SUFFIX_DEFAULT_FADE}"
            fade = fades.get(unique_id)
            if fade is None:
                fade = DefaultFade(hub, host, unique_id)
                fades[unique_id] = fade
                added.append(fade)
            elif fade.hass is not None:
                fade.refresh()
            hub.fade_entities[host] = fade
            unique_id = f"{hub.supply_identifier(host)}:{SUFFIX_PANEL_BRIGHTNESS}"
            level = brightness.get(unique_id)
            if level is None:
                level = PanelBrightness(hub, host, unique_id)
                brightness[unique_id] = level
                added.append(level)
            elif level.hass is not None:
                level.refresh()
        if added:
            async_add_entities(added)

    entry.async_on_unload(hub.add_listener(_sync))
    _sync()


def _owners(hub: ColorKineticsHub) -> list[tuple[str, str]]:
    owners = [(hub.all_fixtures_owner(host), "supply") for host in hub.hosts]
    owners.extend((hub.group_owner(group["id"]), "group") for group in hub.light_groups())
    return owners


class EffectSpeed(RestoreNumber, NumberEntity):
    """Cycles per the effect slider. 1 takes 60 seconds, 10 takes 3 seconds.

    The 1–10 range, the step, and the entity id are unchanged. A saved look
    keeps the stored number, and that number is a slower cycle than before.
    """

    _attr_native_min_value = 1
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_should_poll = False
    _attr_icon = "mdi:speedometer"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

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
            if host:
                self._attr_device_info = self._hub.supply_device_info(host)
            return
        group_id = self._owner.split(":", 1)[1]
        group = next((item for item in self._hub.light_groups() if item["id"] == group_id), None)
        name = group["name"] if group else "Group"
        self._attr_translation_key = None
        self._attr_name = f"{name} effect speed"
        self._attr_icon = "mdi:speedometer"
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

    def apply_external_speed(self, value: float) -> None:
        """Show a speed restored with a look."""
        self._attr_native_value = float(value)
        self._hub.set_effect_speed(self._owner, float(value))
        if self.hass is not None:
            self.async_write_ha_state()

    def refresh(self) -> None:
        """Follow a renamed group or a replaced supply device."""
        self._apply_identity()
        if self.hass is not None:
            self.async_write_ha_state()


class EffectIntensity(RestoreNumber, NumberEntity):
    """How strong an effect-specific trait is, from 1 to 10.

    Tail length, twinkle density, flicker depth, and similar traits read this
    slider. 5 is the middle and the default.
    """

    _attr_native_min_value = 1
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_should_poll = False
    _attr_icon = "mdi:brightness-7"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, hub: ColorKineticsHub, owner: str, unique_id: str, kind: str
    ) -> None:
        """Bind the slider to one effect owner."""
        self._hub = hub
        self._owner = owner
        self._kind = kind
        self._attr_unique_id = unique_id
        self._attr_native_value = float(
            hub.effect_intensity(owner) or DEFAULT_EFFECT_INTENSITY
        )
        self._apply_identity()

    def _apply_identity(self) -> None:
        if self._kind == "supply":
            host = next(
                (
                    host
                    for host in self._hub.hosts
                    if self._hub.all_fixtures_owner(host) == self._owner
                ),
                self._hub.hosts[0] if self._hub.hosts else "",
            )
            self._attr_translation_key = SUFFIX_EFFECT_INTENSITY
            if host:
                self._attr_device_info = self._hub.supply_device_info(host)
            return
        group_id = self._owner.split(":", 1)[1]
        group = next((item for item in self._hub.light_groups() if item["id"] == group_id), None)
        name = group["name"] if group else "Group"
        self._attr_translation_key = None
        self._attr_name = f"{name} effect intensity"
        self._attr_icon = "mdi:brightness-7"
        if group is not None:
            self._attr_device_info = self._hub.group_device_info(group)

    async def async_added_to_hass(self) -> None:
        """Restore the last slider position."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = float(last.native_value)
            self._hub.set_effect_intensity(self._owner, self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Store the intensity. A running effect uses it on the next frame."""
        self._attr_native_value = float(value)
        self._hub.set_effect_intensity(self._owner, float(value))
        self.async_write_ha_state()

    def apply_external_speed(self, value: float) -> None:
        """Show an intensity restored with a look."""
        self._attr_native_value = float(value)
        self._hub.set_effect_intensity(self._owner, float(value))
        if self.hass is not None:
            self.async_write_ha_state()

    def refresh(self) -> None:
        """Follow a renamed group or a replaced supply device."""
        self._apply_identity()
        if self.hass is not None:
            self.async_write_ha_state()


class PanelBrightness(RestoreNumber, NumberEntity):
    """Brightness for the control panel, from 0 to 100 percent.

    The number is the level Follow hub copies. Moving it updates the panel
    target. The light entity's own brightness slider still accepts a direct
    command.
    """

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_should_poll = False
    _attr_icon = "mdi:brightness-6"
    _attr_has_entity_name = True
    _attr_translation_key = SUFFIX_PANEL_BRIGHTNESS
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, hub: ColorKineticsHub, host: str, unique_id: str) -> None:
        """Bind the slider to one Data Enabler."""
        self._hub = hub
        self._host = host
        self._unsub = None
        self._attr_unique_id = unique_id
        self._attr_native_value = float(hub.panel_state(host)["brightness"])
        self._attr_device_info = hub.supply_device_info(host)

    async def async_added_to_hass(self) -> None:
        """Restore the last level when storage has no panel record yet."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._hub.adopt_restore(self._host, brightness=float(last.native_value))
        self._unsub = self._hub.add_panel_listener(self.refresh)
        self.refresh()

    async def async_will_remove_from_hass(self) -> None:
        """Stop listening."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def async_set_native_value(self, value: float) -> None:
        """Store the level and push it to the current target."""
        self._attr_native_value = float(value)
        await self._hub.async_set_brightness(self._host, float(value))
        self.async_write_ha_state()

    def refresh(self) -> None:
        """Show the level stored on the hub."""
        self._attr_device_info = self._hub.supply_device_info(self._host)
        self._attr_native_value = float(self._hub.panel_state(self._host)["brightness"])
        if self.hass is not None:
            self.async_write_ha_state()


class DefaultFade(NumberEntity):
    """Seconds to fade when a light command does not pass ``transition``.

    0 snaps, which is the 0.2.0 behavior. The value is stored on the config
    entry, so a restart brings the same default back.
    """

    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 0.1
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False
    _attr_icon = "mdi:timer-outline"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = SUFFIX_DEFAULT_FADE
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 1

    def __init__(self, hub: ColorKineticsHub, host: str, unique_id: str) -> None:
        """Bind the number to one Data Enabler. The value is shared by the entry."""
        self._hub = hub
        self._host = host
        self._attr_unique_id = unique_id
        self._attr_native_value = hub.default_fade
        self._attr_device_info = hub.supply_device_info(host)

    async def async_set_native_value(self, value: float) -> None:
        """Store the fade. The next light command that omits transition uses it."""
        clamped = clamp_fade_seconds(value, fallback=self._hub.default_fade)
        self._attr_native_value = clamped
        self._hub.set_default_fade(clamped)
        self.async_write_ha_state()
        options = dict(self._hub.entry.options)
        if options.get(CONF_DEFAULT_FADE) == clamped:
            return
        options[CONF_DEFAULT_FADE] = clamped
        self.hass.config_entries.async_update_entry(self._hub.entry, options=options)

    def apply_fade(self, value: float) -> None:
        """Show a value written from the options flow or another supply's number."""
        self._attr_native_value = float(value)
        if self.hass is not None:
            self.async_write_ha_state()

    def refresh(self) -> None:
        """Follow a replaced supply device."""
        self._attr_device_info = self._hub.supply_device_info(self._host)
        self._attr_native_value = self._hub.default_fade
        if self.hass is not None:
            self.async_write_ha_state()
