"""One RGB light per Color Kinetics fixture."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    ATTR_XY_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util import color as color_util

from .color_temp import clamp_kelvin, kelvin_to_rgb
from .const import (
    CHANNELS_PER_FIXTURE,
    DEFAULT_EFFECT_SPEED,
    DOMAIN,
    MAX_COLOR_TEMP_KELVIN,
    MIN_COLOR_TEMP_KELVIN,
)
from .effects import (
    EFFECT_NAMES,
    EFFECT_SOLID,
    fixture_channels,
    legacy_palette,
    resolve_effect,
)
from .hub import ColorKineticsConfigEntry, ColorKineticsHub, Fixture
from .properties import SUFFIX_ALL_FIXTURES

_CHANNELS = CHANNELS_PER_FIXTURE


def scale_rgb(rgb: tuple[int, int, int], brightness: int) -> bytes:
    """Scale an RGB triple by a 0-255 brightness into DMX channel bytes."""
    level = max(0, min(255, int(brightness)))
    return bytes((channel * level + 127) // 255 for channel in rgb)


def transition_seconds(kwargs: dict[str, Any], default: float = 0.0) -> float:
    """Seconds to fade.

    An omitted ``transition`` uses ``default``. An explicit 0, or a value that
    is not a number, snaps. A scene that sets ``transition`` gets that value.
    """
    if ATTR_TRANSITION not in kwargs:
        return max(0.0, float(default))
    raw = kwargs.get(ATTR_TRANSITION)
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if seconds <= 0:
        return 0.0
    return seconds


def apply_color_kwargs(entity: LightEntity, kwargs: dict[str, Any]) -> bool:
    """Apply rgb, hs, xy, or kelvin. Return true when the caller set a color.

    Kelvin wins only when no rgb, hs, or xy is in the same call. The stored
    RGB for kelvin is the blackbody color, so effects use that color.
    """
    if any(key in kwargs for key in (ATTR_RGB_COLOR, ATTR_HS_COLOR, ATTR_XY_COLOR)):
        if ATTR_RGB_COLOR in kwargs:
            entity._attr_rgb_color = tuple(int(channel) for channel in kwargs[ATTR_RGB_COLOR])
        elif ATTR_HS_COLOR in kwargs:
            entity._attr_rgb_color = color_util.color_hs_to_RGB(*kwargs[ATTR_HS_COLOR])
        else:
            entity._attr_rgb_color = color_util.color_xy_to_RGB(*kwargs[ATTR_XY_COLOR])
        entity._attr_color_mode = ColorMode.RGB
        return True
    if ATTR_COLOR_TEMP_KELVIN in kwargs:
        kelvin = clamp_kelvin(kwargs[ATTR_COLOR_TEMP_KELVIN])
        entity._attr_color_temp_kelvin = kelvin
        entity._attr_rgb_color = kelvin_to_rgb(kelvin)
        entity._attr_color_mode = ColorMode.COLOR_TEMP
        return True
    return False


class _ColorData(ExtraStoredData):
    """Optimistic color stored beside the entity state."""

    def __init__(
        self,
        is_on: bool,
        brightness: int,
        rgb: tuple[int, int, int],
        color_mode: str,
        color_temp_kelvin: int | None,
    ) -> None:
        self.is_on = is_on
        self.brightness = brightness
        self.rgb = rgb
        self.color_mode = color_mode
        self.color_temp_kelvin = color_temp_kelvin

    def as_dict(self) -> dict[str, Any]:
        """Serialize for restore."""
        return {
            "is_on": self.is_on,
            "brightness": self.brightness,
            "rgb": list(self.rgb),
            "color_mode": self.color_mode,
            "color_temp_kelvin": self.color_temp_kelvin,
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
    named: dict[str, GroupLight] = {}

    def _sync() -> None:
        added: list[LightEntity] = []
        for host in hub.hosts:
            if host not in groups:
                group = AllFixturesLight(hub, host, entities)
                groups[host] = group
                added.append(group)
        for spec in hub.light_groups():
            entity = named.get(spec["id"])
            if entity is None:
                entity = GroupLight(hub, spec["id"], entities)
                named[spec["id"]] = entity
                added.append(entity)
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
        for group in (*groups.values(), *named.values()):
            if group.hass is not None:
                group.publish()

    hub.fixture_lights = entities
    hub.supply_lights = groups
    hub.group_lights = named
    entry.async_on_unload(hub.add_listener(_sync))
    _sync()


class FixtureLight(RestoreEntity, LightEntity):
    """Optimistic RGB light. The Data Enabler does not report color back."""

    _attr_supported_color_modes = {ColorMode.COLOR_TEMP, ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
    _attr_supported_features = LightEntityFeature.TRANSITION
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
        self._off_when_fade_ends = False
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
        return _ColorData(
            bool(self._attr_is_on),
            brightness,
            tuple(int(channel) for channel in rgb),
            str(self._attr_color_mode or ColorMode.RGB),
            self._attr_color_temp_kelvin,
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last color and write it into the frame buffer."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            raw = extra.as_dict()
            self._attr_is_on = bool(raw.get("is_on"))
            self._restore_color(raw)
        else:
            last = await self.async_get_last_state()
            if last is not None and last.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                self._attr_is_on = last.state == STATE_ON
                self._restore_color(
                    {
                        "brightness": last.attributes.get(ATTR_BRIGHTNESS),
                        "rgb": last.attributes.get(ATTR_RGB_COLOR),
                        "color_mode": last.attributes.get(ATTR_COLOR_MODE),
                        "color_temp_kelvin": last.attributes.get(ATTR_COLOR_TEMP_KELVIN),
                    }
                )
        fixture = self._fixture()
        self._attr_available = bool(fixture and self._hub.is_available(fixture))
        self._push()
        self._hub.notify_light_states()

    def _restore_color(self, raw: dict[str, Any]) -> None:
        """Restore brightness, RGB, and the last color mode."""
        if raw.get("brightness") is not None:
            self._attr_brightness = int(raw["brightness"])
        if raw.get("rgb"):
            self._attr_rgb_color = tuple(int(channel) for channel in raw["rgb"])
        kelvin = raw.get("color_temp_kelvin")
        if raw.get("color_mode") == ColorMode.COLOR_TEMP and kelvin is not None:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = clamp_kelvin(kelvin)
            self._attr_rgb_color = kelvin_to_rgb(self._attr_color_temp_kelvin)
            return
        self._attr_color_mode = ColorMode.RGB

    def complete_fade(self) -> None:
        """Mark the light off after a turn-off fade reaches zero."""
        if not self._off_when_fade_ends:
            return
        self._off_when_fade_ends = False
        self._attr_is_on = False
        if self.hass is not None:
            self.async_write_ha_state()

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
        if self._key in self._hub.effects.owners:
            return
        if self._hub.fades.contains(self._key):
            return
        if self._attr_is_on:
            payload = self._channels()
        else:
            payload = bytes(_CHANNELS)
        self._hub.write_channels(fixture.host, fixture.dmx_start, payload)

    def apply_panel_color(self, rgb: tuple[int, int, int], brightness: int) -> None:
        """Paint one fixture from a scene. The write is the normal light path.

        Scene recall does not fade. The default fade stays on ordinary light
        commands. Brightness 0 turns the fixture off and keeps the color.
        """
        self._off_when_fade_ends = False
        self._attr_color_mode = ColorMode.RGB
        self._attr_rgb_color = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        self._attr_brightness = int(brightness)
        self._attr_is_on = int(brightness) > 0
        self._hub.release_fixture_effect(self._key)
        self._hub.cancel_fades([self._key])
        self._push()
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set color and brightness. State is optimistic."""
        if kwargs.get(ATTR_BRIGHTNESS) == 0:
            await self.async_turn_off(**kwargs)
            return
        apply_color_kwargs(self, kwargs)
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = int(kwargs[ATTR_BRIGHTNESS])
        self._attr_is_on = True
        self._off_when_fade_ends = False
        self._hub.note_command([self._key])
        self._hub.release_fixture_effect(self._key)
        self._hub.cancel_fades([self._key])
        duration = transition_seconds(kwargs, self._hub.default_fade)
        if duration > 0:
            self._hub.start_fade(self._key, self._channels(), duration)
        else:
            self._push()
        self.async_write_ha_state()
        self._hub.notify_light_states()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Fade the fixture's channels to zero, then report off.

        The last color is kept. An explicit ``transition`` of 0 snaps off.
        """
        self._hub.note_command([self._key])
        self._hub.release_fixture_effect(self._key)
        self._hub.cancel_fades([self._key])
        duration = transition_seconds(kwargs, self._hub.default_fade)
        if duration > 0:
            self._off_when_fade_ends = True
            if not self._hub.start_fade(self._key, bytes(_CHANNELS), duration):
                self.complete_fade()
        else:
            self._off_when_fade_ends = False
            self._attr_is_on = False
            self._push()
        self.async_write_ha_state()
        self._hub.notify_light_states()

    def apply_look(
        self,
        saved: dict[str, Any],
        transition: float,
        channel_target: bytes | None = None,
    ) -> None:
        """Restore one fixture from a look. A missing fixture is never passed in.

        ``channel_target`` is the bytes to fade toward. An effect recall passes
        the effect's first frame so a stale saved color is not the target.
        """
        self._off_when_fade_ends = False
        kelvin = saved.get("color_temp_kelvin")
        if saved.get("color_mode") == ColorMode.COLOR_TEMP and kelvin is not None:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = clamp_kelvin(kelvin)
            self._attr_rgb_color = kelvin_to_rgb(self._attr_color_temp_kelvin)
        else:
            rgb = saved.get("rgb") or (255, 255, 255)
            self._attr_rgb_color = tuple(int(channel) for channel in rgb)
            self._attr_color_mode = ColorMode.RGB
        if saved.get("brightness") is not None:
            self._attr_brightness = int(saved["brightness"])
        self._attr_is_on = bool(saved.get("is_on"))
        self._hub.release_fixture_effect(self._key)
        self._hub.cancel_fades([self._key])
        payload = self._channels() if channel_target is None else bytes(channel_target)
        if transition > 0:
            self._hub.start_fade(self._key, payload, transition)
        else:
            self._hub.start_fade(self._key, payload, 0)
        if self.hass is not None:
            self.async_write_ha_state()


class AllFixturesLight(LightEntity):
    """One RGB light that paints every available discovered fixture on a supply."""

    _attr_supported_color_modes = {ColorMode.COLOR_TEMP, ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
    _attr_supported_features = LightEntityFeature.EFFECT | LightEntityFeature.TRANSITION
    _attr_effect_list = list(EFFECT_NAMES)
    _attr_effect = EFFECT_SOLID
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
        self._owner_id = hub.all_fixtures_owner(host)
        self._sync_from_members()

    def _candidate_keys(self) -> list[str]:
        return [
            serial
            for serial in self._hub.fixture_order()
            if (fixture := self._hub.get_fixture(serial)) is not None
            and fixture.host == self._host
            and fixture.source != "manual"
        ]

    def _members(self) -> list[FixtureLight]:
        chosen: list[FixtureLight] = []
        for key in self._candidate_keys():
            light = self._lights.get(key)
            fixture = None if light is None else light._fixture()
            if light is None or fixture is None or not self._hub.is_available(fixture):
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
        modes = {member.color_mode for member in members}
        if len(modes) == 1:
            self._attr_color_mode = next(iter(modes))
        if self.color_mode == ColorMode.COLOR_TEMP:
            kelvins = {
                member.color_temp_kelvin
                for member in members
                if member.color_temp_kelvin is not None
            }
            if len(kelvins) == 1:
                self._attr_color_temp_kelvin = next(iter(kelvins))

    def publish(self) -> None:
        """Refresh the aggregated state."""
        self._attr_device_info = self._hub.supply_device_info(self._host)
        self._apply_effect_state()
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

    def _stolen(self) -> bool:
        """True when every member is currently owned by a different effect."""
        members = self._members()
        if not members:
            return False
        return all(
            self._hub.effects.owners.get(member._key) not in (None, self._owner_id)
            for member in members
        )

    def _apply_effect_state(self) -> None:
        run = self._hub.effects.runs.get(self._owner_id)
        if run is not None and run.order:
            self._attr_effect = run.effect
            self._attr_is_on = True
            self._attr_available = bool(self._members())
            return
        pending = self._hub.deferred_effect(self._owner_id)
        if pending is not None and pending.get("effect") not in (None, EFFECT_SOLID):
            self._attr_effect = str(pending["effect"])
            self._attr_is_on = True
            self._attr_available = bool(self._members())
            return
        self._attr_effect = EFFECT_SOLID
        if self._stolen():
            self._attr_is_on = False
            self._attr_available = bool(self._members())
            return
        self._sync_from_members()

    def _remember_members(self, members: list[FixtureLight] | None = None) -> None:
        """Copy this light onto members it still owns.

        A fixture this effect owns is on, at the effect's color and brightness.
        A fixture another effect owns is left alone.
        """
        rgb = tuple(self._attr_rgb_color or (255, 255, 255))
        for member in self._members() if members is None else members:
            owner = self._hub.effects.owners.get(member._key)
            if owner is not None and owner != self._owner_id:
                continue
            run = self._hub.effects.runs.get(self._owner_id) if owner == self._owner_id else None
            if run is not None and member._key in run.order:
                member._attr_is_on = True
                member._attr_brightness = int(run.brightness)
                member._attr_rgb_color = tuple(int(channel) for channel in run.rgb)
            else:
                member._attr_is_on = bool(self._attr_is_on)
                member._attr_rgb_color = rgb
                member._attr_brightness = self._attr_brightness
            member._off_when_fade_ends = False
            member._attr_color_mode = self.color_mode or ColorMode.RGB
            if self.color_temp_kelvin is not None:
                member._attr_color_temp_kelvin = self.color_temp_kelvin
            if member.hass is not None:
                member.async_write_ha_state()

    def _paint(self, payload: bytes, members: list[FixtureLight] | None = None) -> None:
        chosen = self._members() if members is None else members
        by_host: dict[str, list[int]] = {}
        for member in chosen:
            fixture = member._fixture()
            by_host.setdefault(fixture.host, []).append(fixture.dmx_start)
        self._remember_members(chosen)
        for host, starts in by_host.items():
            self._hub.write_same_channels(host, starts, payload)
        self.publish()

    def _claim(self, effect: str, only_keys: set[str] | None = None) -> None:
        """Latest command wins: these fixtures leave any other effect.

        ``only_keys`` limits the claim to fixtures this command still owns,
        so a fade that ends does not steal a fixture a newer command took.
        """
        keys = [member._key for member in self._members()]
        if only_keys is not None:
            keys = [key for key in keys if key in only_keys]
        self._hub.cancel_fades(keys)
        if effect == EFFECT_SOLID:
            self._hub.effects.stop(self._owner_id)
            for key in keys:
                self._hub.effects.release_fixture(key)
            return
        if not keys:
            return
        style = self._hub._effect_style(self._owner_id)
        self._hub.effects.start(
            self._owner_id,
            effect,
            keys,
            tuple(self._attr_rgb_color or (255, 255, 255)),
            255 if self._attr_brightness is None else int(self._attr_brightness),
            self._hub.effect_speed(self._owner_id),
            style["intensity"],
            style["palette"],
            style["direction"],
            self._custom_colors(),
        )
        self._hub.ensure_effect_loop()

    def _custom_colors(self) -> tuple[tuple[int, int, int], ...]:
        """Colors currently on this light and its members, for the Custom palette."""
        primary = tuple(int(channel) for channel in (self._attr_rgb_color or (255, 255, 255)))
        colors = [primary]
        for member in self._members():
            rgb = tuple(int(channel) for channel in (member._attr_rgb_color or primary))
            if rgb not in colors:
                colors.append(rgb)
        return tuple(colors)

    def _apply_requested_effect(self, raw: str) -> str:
        """Resolve an old effect name and select the palette that name implies."""
        forced = legacy_palette(raw)
        if forced is not None:
            self._hub.set_effect_palette(self._owner_id, forced)
            picker = self._hub.palette_entities.get(self._owner_id)
            if picker is not None:
                picker.apply_external(forced)
        effect = resolve_effect(raw)
        if effect not in EFFECT_NAMES:
            return EFFECT_SOLID
        return effect

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Apply one color, brightness, or effect to every member in order."""
        if not self._members():
            self.publish()
            self._hub.notify_light_states()
            return
        if kwargs.get(ATTR_BRIGHTNESS) == 0:
            await self.async_turn_off(**kwargs)
            return
        color_chosen = apply_color_kwargs(self, kwargs)
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = int(kwargs[ATTR_BRIGHTNESS])
        self._attr_is_on = True
        if ATTR_EFFECT in kwargs:
            effect = self._apply_requested_effect(str(kwargs.get(ATTR_EFFECT) or ""))
        elif color_chosen:
            effect = EFFECT_SOLID
        else:
            effect = self._apply_requested_effect(str(self._attr_effect or EFFECT_SOLID))
        self._attr_effect = effect
        members = self._members()
        epoch = self._hub.note_command([member._key for member in members])
        if effect != EFFECT_SOLID and ATTR_TRANSITION not in kwargs:
            duration = 0.0
        else:
            duration = transition_seconds(kwargs, self._hub.default_fade)
        if effect != EFFECT_SOLID and duration > 0:
            rgb = tuple(int(channel) for channel in (self._attr_rgb_color or (255, 255, 255)))
            brightness = 255 if self._attr_brightness is None else int(self._attr_brightness)
            self._fade_in_effect(members, effect, rgb, brightness, duration, epoch)
            self.publish()
            self._hub.notify_light_states()
            return
        self._claim(effect)
        if effect == EFFECT_SOLID:
            if duration > 0:
                self._remember_members(members)
                now = self._hub.effect_clock()
                for member in members:
                    self._hub.cancel_fades([member._key])
                    self._hub.start_fade(member._key, self._channels(), duration, now=now)
                self.publish()
                self._hub.notify_light_states()
                return
            self._paint(self._channels(), members)
            self._hub.notify_light_states()
            return
        self._remember_members(members)
        self._hub.render_effects(self._hub.effect_clock())
        self.publish()
        self._hub.notify_light_states()

    def _fade_in_effect(
        self,
        members: list[FixtureLight],
        effect: str,
        rgb: tuple[int, int, int],
        brightness: int,
        duration: float,
        epoch: int,
    ) -> None:
        """Fade each member up to the effect's first frame, then start it.

        The effect that owns these fixtures is stopped first. Otherwise the
        next effect frame cancels the fade and keeps painting the old effect.
        Members are on at the new effect's color and brightness while the
        channels fade, so a look saved during the fade does not record the
        previous color.
        """
        self._hub.effects.stop(self._owner_id)
        count = len(members)
        style = self._hub._effect_style(self._owner_id)
        now = self._hub.effect_clock()
        for index, member in enumerate(members):
            self._hub.release_fixture_effect(member._key)
            member._off_when_fade_ends = False
            member._attr_is_on = True
            member._attr_brightness = brightness
            member._attr_rgb_color = rgb
            member._attr_color_mode = self.color_mode or ColorMode.RGB
            if self.color_temp_kelvin is not None:
                member._attr_color_temp_kelvin = self.color_temp_kelvin
            target = fixture_channels(
                effect,
                index,
                count,
                0.0,
                rgb,
                brightness,
                style["intensity"],
                style["palette"],
                style["direction"],
                self._custom_colors(),
            )
            self._hub.cancel_fades([member._key])
            self._hub.start_fade(member._key, target, duration, now=now)
            if member.hass is not None:
                member.async_write_ha_state()
        self._hub.defer_effects(
            epoch,
            duration,
            [
                (
                    self._owner_id,
                    {
                        "effect": effect,
                        "speed": self._hub.effect_speed(self._owner_id),
                        "intensity": style["intensity"],
                        "palette": style["palette"],
                        "direction": style["direction"],
                        "rgb": [int(channel) for channel in rgb],
                        "brightness": int(brightness),
                        "is_on": True,
                    },
                )
            ],
        )

    def _members_we_may_turn_off(self) -> list[FixtureLight]:
        """Fixtures this light may black out. Another effect's fixtures are left alone."""
        owned: list[FixtureLight] = []
        for member in self._members():
            owner = self._hub.effects.owners.get(member._key)
            if owner is not None and owner != self._owner_id:
                continue
            owned.append(member)
        return owned

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn this light's fixtures off and stop its effect.

        Fixtures another effect owns are not released and are not written.
        """
        members = self._members_we_may_turn_off()
        self._attr_effect = EFFECT_SOLID
        self._hub.note_command([member._key for member in members])
        self._hub.effects.stop(self._owner_id)
        duration = transition_seconds(kwargs, self._hub.default_fade)
        if duration > 0 and members:
            now = self._hub.effect_clock()
            for member in members:
                member._off_when_fade_ends = True
                self._hub.cancel_fades([member._key])
                if not self._hub.start_fade(member._key, bytes(_CHANNELS), duration, now=now):
                    member.complete_fade()
                elif member.hass is not None:
                    member.async_write_ha_state()
            self.publish()
            self._hub.notify_light_states()
            return
        for member in members:
            member._off_when_fade_ends = False
        self._attr_is_on = False
        self._paint(self._channels(), members)
        self._hub.notify_light_states()

    def apply_effect_look(
        self, saved: dict[str, Any], only_keys: set[str] | None = None
    ) -> None:
        """Restore this light's effect. Solid leaves the fixture colors in place."""
        effect = self._apply_requested_effect(str(saved.get("effect") or EFFECT_SOLID))
        rgb = saved.get("rgb") or (255, 255, 255)
        self._attr_rgb_color = tuple(int(channel) for channel in rgb)
        if saved.get("brightness") is not None:
            self._attr_brightness = int(saved["brightness"])
        try:
            speed = float(saved.get("speed", DEFAULT_EFFECT_SPEED))
        except (TypeError, ValueError):
            speed = float(DEFAULT_EFFECT_SPEED)
        self._hub.set_effect_speed(self._owner_id, speed)
        self._hub._apply_saved_style(self._owner_id, saved)
        self._attr_effect = effect
        if effect == EFFECT_SOLID:
            self._hub.effects.stop(self._owner_id)
            self.publish()
            return
        if only_keys is not None and not any(
            member._key in only_keys for member in self._members()
        ):
            self.publish()
            return
        self._attr_is_on = True
        self._claim(effect, only_keys=only_keys)
        self._remember_members()
        self._hub.render_effects(self._hub.effect_clock())
        self.publish()


class GroupLight(AllFixturesLight):
    """A named, ordered subset of fixtures. The id is stable when the name changes."""

    _attr_translation_key = None

    def __init__(
        self, hub: ColorKineticsHub, group_id: str, lights: dict[str, FixtureLight]
    ) -> None:
        """Bind the light to one stored group."""
        self._group_id = group_id
        super().__init__(hub, hub.hosts[0] if hub.hosts else "", lights)
        self._owner_id = hub.group_owner(group_id)
        self._attr_unique_id = self._owner_id
        self._apply_group_name()

    def _spec(self) -> dict | None:
        return next(
            (group for group in self._hub.light_groups() if group["id"] == self._group_id),
            None,
        )

    def _apply_group_name(self) -> None:
        spec = self._spec()
        self._attr_name = spec["name"] if spec else "Group"
        self._attr_icon = "mdi:lightbulb-group"
        if spec is not None:
            self._attr_device_info = self._hub.group_device_info(spec)

    def _candidate_keys(self) -> list[str]:
        spec = self._spec()
        if spec is None:
            return []
        return list(spec["members"])

    def publish(self) -> None:
        """Refresh name, device, and effect state."""
        self._apply_group_name()
        self._apply_effect_state()
        if self.hass is not None:
            self.async_write_ha_state()
