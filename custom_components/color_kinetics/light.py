"""One RGB light per Color Kinetics fixture."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ATTR_XY_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util import color as color_util

from .const import CHANNELS_PER_FIXTURE, DOMAIN
from .hub import ColorKineticsConfigEntry, ColorKineticsHub, Fixture
from .properties import SUFFIX_ALL_FIXTURES

_CHANNELS = CHANNELS_PER_FIXTURE


def scale_rgb(rgb: tuple[int, int, int], brightness: int) -> bytes:
    """Scale an RGB triple by a 0-255 brightness into DMX channel bytes."""
    level = max(0, min(255, int(brightness)))
    return bytes((channel * level + 127) // 255 for channel in rgb)


class _ColorData(ExtraStoredData):
    """Optimistic color stored beside the entity state."""

    def __init__(self, is_on: bool, brightness: int, rgb: tuple[int, int, int]) -> None:
        self.is_on = is_on
        self.brightness = brightness
        self.rgb = rgb

    def as_dict(self) -> dict[str, Any]:
        """Serialize for restore."""
        return {
            "is_on": self.is_on,
            "brightness": self.brightness,
            "rgb": list(self.rgb),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ColorKineticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the group light and one light per fixture."""
    hub = entry.runtime_data
    entities: dict[str, FixtureLight] = {}
    groups: dict[str, AllFixturesLight] = {}

    def _sync() -> None:
        added: list[LightEntity] = []
        for host in hub.hosts:
            if host not in groups:
                group = AllFixturesLight(hub, host, entities)
                groups[host] = group
                added.append(group)
        for fixture in hub.fixtures():
            entity = entities.get(fixture.key)
            if entity is None:
                entity = FixtureLight(hub, fixture.key)
                entities[fixture.key] = entity
                added.append(entity)
            elif entity.hass is not None:
                entity.apply_fixture_update()
        if added:
            async_add_entities(added)
        for group in groups.values():
            if group.hass is not None:
                group.publish()

    entry.async_on_unload(hub.add_listener(_sync))
    _sync()


class FixtureLight(RestoreEntity, LightEntity):
    """Optimistic RGB light. The Data Enabler does not report color back."""

    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_assumed_state = True
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(self, hub: ColorKineticsHub, key: str) -> None:
        """Bind the light to one fixture key."""
        self._hub = hub
        self._key = key
        fixture = hub.get_fixture(key)
        self._attr_unique_id = key
        self._attr_is_on = False
        self._attr_brightness = 255
        self._attr_rgb_color = (255, 255, 255)
        self._attr_available = bool(fixture and hub.is_available(fixture))
        self._attr_device_info = self._device_info(fixture)

    def _fixture(self) -> Fixture | None:
        return self._hub.get_fixture(self._key)

    def _device_info(self, fixture: Fixture | None) -> DeviceInfo:
        if fixture is None:
            return DeviceInfo(identifiers={(DOMAIN, self._key)})
        return self._hub.fixture_device_info(fixture)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Serial, 1-based DMX start, and type code."""
        fixture = self._fixture()
        if fixture is None:
            return {}
        attributes: dict[str, Any] = {
            "serial": fixture.serial_hex,
            "dmx_start": None
            if fixture.dmx_start is None
            else fixture.dmx_start + 1,
            "supply": fixture.host,
            "source": fixture.source,
        }
        if fixture.type_code is not None:
            attributes["type_code"] = f"0x{fixture.type_code:02X}"
        return attributes

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Remember color even if the entity was unavailable."""
        rgb = self._attr_rgb_color or (255, 255, 255)
        brightness = 255 if self._attr_brightness is None else int(self._attr_brightness)
        return _ColorData(bool(self._attr_is_on), brightness, tuple(rgb))

    async def async_added_to_hass(self) -> None:
        """Restore the last color and write it into the frame buffer."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            raw = extra.as_dict()
            self._attr_is_on = bool(raw.get("is_on"))
            if raw.get("brightness") is not None:
                self._attr_brightness = int(raw["brightness"])
            if raw.get("rgb"):
                self._attr_rgb_color = tuple(int(channel) for channel in raw["rgb"])
        else:
            last = await self.async_get_last_state()
            if last is not None and last.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                self._attr_is_on = last.state == STATE_ON
                if last.attributes.get(ATTR_BRIGHTNESS) is not None:
                    self._attr_brightness = int(last.attributes[ATTR_BRIGHTNESS])
                if last.attributes.get(ATTR_RGB_COLOR) is not None:
                    self._attr_rgb_color = tuple(last.attributes[ATTR_RGB_COLOR])
        self._attr_color_mode = ColorMode.RGB
        fixture = self._fixture()
        self._attr_available = bool(fixture and self._hub.is_available(fixture))
        self._push()

    def apply_fixture_update(self) -> None:
        """Availability, model, or DMX address changed after a rescan."""
        fixture = self._fixture()
        self._attr_available = bool(fixture and self._hub.is_available(fixture))
        self._attr_device_info = self._device_info(fixture)
        self._push()
        if self.hass is not None:
            self.async_write_ha_state()
        self._hub.notify_light_states()

    def _channels(self) -> bytes:
        if not self._attr_is_on:
            return bytes(_CHANNELS)
        rgb = tuple(self._attr_rgb_color or (255, 255, 255))
        brightness = 255 if self._attr_brightness is None else int(self._attr_brightness)
        return scale_rgb((int(rgb[0]), int(rgb[1]), int(rgb[2])), brightness)

    def _push(self) -> None:
        fixture = self._fixture()
        if fixture is None or fixture.dmx_start is None:
            return
        if fixture.host not in self._hub.hosts:
            return
        if not self._hub.is_available(fixture):
            return
        if self._attr_is_on:
            payload = self._channels()
        else:
            payload = bytes(_CHANNELS)
        self._hub.write_channels(fixture.host, fixture.dmx_start, payload)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set color and brightness. State is optimistic."""
        if kwargs.get(ATTR_BRIGHTNESS) == 0:
            await self.async_turn_off()
            return
        if ATTR_RGB_COLOR in kwargs:
            self._attr_rgb_color = tuple(int(channel) for channel in kwargs[ATTR_RGB_COLOR])
        elif ATTR_HS_COLOR in kwargs:
            self._attr_rgb_color = color_util.color_hs_to_RGB(*kwargs[ATTR_HS_COLOR])
        elif ATTR_XY_COLOR in kwargs:
            self._attr_rgb_color = color_util.color_xy_to_RGB(*kwargs[ATTR_XY_COLOR])
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = int(kwargs[ATTR_BRIGHTNESS])
        self._attr_is_on = True
        self._attr_color_mode = ColorMode.RGB
        self._push()
        self.async_write_ha_state()
        self._hub.notify_light_states()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the fixture's channels off. The last color is kept."""
        self._attr_is_on = False
        self._push()
        self.async_write_ha_state()
        self._hub.notify_light_states()


class AllFixturesLight(LightEntity):
    """One RGB light that paints every available discovered fixture on a supply."""

    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_assumed_state = True
    _attr_has_entity_name = True
    _attr_translation_key = SUFFIX_ALL_FIXTURES
    _attr_icon = "mdi:lightbulb-group"
    _attr_should_poll = False

    def __init__(
        self,
        hub: ColorKineticsHub,
        host: str,
        lights: dict[str, FixtureLight],
    ) -> None:
        """Bind the group to one Data Enabler and the fixture lights on it."""
        self._hub = hub
        self._host = host
        self._lights = lights
        self._unsub = None
        self._attr_unique_id = hub.all_fixtures_unique_id(host)
        self._attr_is_on = False
        self._attr_brightness = 255
        self._attr_rgb_color = (255, 255, 255)
        self._attr_device_info = hub.supply_device_info(host)
        self._sync_from_members()

    def _members(self) -> list[FixtureLight]:
        chosen: list[FixtureLight] = []
        for light in self._lights.values():
            fixture = light._fixture()
            if fixture is None or fixture.host != self._host:
                continue
            if fixture.source == "manual":
                continue
            if not self._hub.is_available(fixture):
                continue
            chosen.append(light)
        return chosen

    def _sync_from_members(self) -> None:
        members = self._members()
        self._attr_available = bool(members)
        if not members:
            self._attr_is_on = False
            return
        self._attr_is_on = any(bool(member.is_on) for member in members)
        colors = {
            tuple(int(channel) for channel in (member._attr_rgb_color or (255, 255, 255)))
            for member in members
        }
        levels = {
            255 if member._attr_brightness is None else int(member._attr_brightness)
            for member in members
        }
        if len(colors) == 1 and len(levels) == 1:
            self._attr_rgb_color = next(iter(colors))
            self._attr_brightness = next(iter(levels))

    def publish(self) -> None:
        """Refresh the aggregated state."""
        self._attr_device_info = self._hub.supply_device_info(self._host)
        self._sync_from_members()
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Follow member lights and fixture availability."""
        await super().async_added_to_hass()
        self._unsub = self._hub.add_light_listener(self.publish)
        self.publish()

    async def async_will_remove_from_hass(self) -> None:
        """Stop listening."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _channels(self) -> bytes:
        if not self._attr_is_on:
            return bytes(_CHANNELS)
        rgb = tuple(self._attr_rgb_color or (255, 255, 255))
        brightness = 255 if self._attr_brightness is None else int(self._attr_brightness)
        return scale_rgb((int(rgb[0]), int(rgb[1]), int(rgb[2])), brightness)

    def _paint(self, payload: bytes) -> None:
        starts: list[int] = []
        for member in self._members():
            fixture = member._fixture()
            member._attr_is_on = bool(self._attr_is_on)
            member._attr_rgb_color = tuple(self._attr_rgb_color or (255, 255, 255))
            member._attr_brightness = self._attr_brightness
            member._attr_color_mode = ColorMode.RGB
            starts.append(fixture.dmx_start)
            if member.hass is not None:
                member.async_write_ha_state()
        if starts:
            self._hub.write_same_channels(self._host, starts, payload)
        self.publish()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Apply one color and brightness to every available discovered fixture."""
        if not self._members():
            self.publish()
            return
        if kwargs.get(ATTR_BRIGHTNESS) == 0:
            await self.async_turn_off()
            return
        rgb = tuple(self._attr_rgb_color or (255, 255, 255))
        if ATTR_RGB_COLOR in kwargs:
            rgb = tuple(int(channel) for channel in kwargs[ATTR_RGB_COLOR])
        elif ATTR_HS_COLOR in kwargs:
            rgb = color_util.color_hs_to_RGB(*kwargs[ATTR_HS_COLOR])
        elif ATTR_XY_COLOR in kwargs:
            rgb = color_util.color_xy_to_RGB(*kwargs[ATTR_XY_COLOR])
        brightness = 255 if self._attr_brightness is None else int(self._attr_brightness)
        if ATTR_BRIGHTNESS in kwargs:
            brightness = int(kwargs[ATTR_BRIGHTNESS])
        self._attr_rgb_color = rgb
        self._attr_brightness = brightness
        self._attr_is_on = True
        self._attr_color_mode = ColorMode.RGB
        self._paint(self._channels())

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn every available discovered fixture off in one frame."""
        self._attr_is_on = False
        self._paint(self._channels())
