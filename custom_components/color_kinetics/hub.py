"""Runtime for one Color Kinetics config entry.

One UDP client, one frame buffer per Data Enabler, and a fixture list keyed
by serial. Discovery runs at startup only when that supply has never been
scanned, and again when the user presses Rescan. It is not polled.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import (
    CHANNELS_PER_FIXTURE,
    CONF_FIXTURE_ORDER,
    CONF_GROUPS,
    DEFAULT_EFFECT_SPEED,
    DOMAIN,
    KEEPALIVE_SECONDS,
    MANUFACTURER,
    MAX_FPS,
)
from .entry_data import (
    hosts_of,
    manual_text_of,
    output_settings_of,
    supplies_of,
)
from .manual import ManualFixture, ManualFixtureError, parse_manual_fixtures
from .effects import EFFECT_SOLID, EffectEngine
from .fade import FadeBook
from .looks import find_look_name, normalize_look_name
from .order import effective_order
from .properties import (
    COMMON_FIXTURE_PROPS,
    GEN4_FIXTURE_PROPS,
    SUFFIX_ALL_FIXTURES,
    SUFFIX_DMX,
    SUFFIX_EFFECT_SPEED,
    SUFFIX_LOOK,
    SUFFIX_POWER_TIME,
    SUFFIX_RAW,
    SUFFIX_SCALE,
    SUFFIX_STARTUP,
    SUPPLY_PROPS,
    TYPE_BLAST,
    model_name,
)
from .protocol import (
    DMX_SLOTS,
    PORTOUT_MIN_DATA,
    KiNetClient,
    KiNetError,
    SupplyInfo,
    build_dmxout,
    build_portout,
    dmx_start_zero_based,
    format_fixture_serial,
    supply_entry_dict,
)
from .pump import FramePump

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 2

Listener = Any


class ColorKineticsStore(Store[dict[str, Any]]):
    """Fixture cache. Version 2 adds named looks beside the saved channels."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep a 0.1.1 cache and add an empty look map."""
        if isinstance(old_data, dict):
            data = dict(old_data)
        else:
            data = {}
        if old_major_version < STORAGE_VERSION and not isinstance(data.get("looks"), dict):
            data["looks"] = {}
        return data


@dataclass
class _DeferredEffects:
    """Effects from a look that should start when its fade finishes."""

    epoch: int
    deadline: float
    effects: list[tuple[str, dict[str, Any]]]


class ScanInProgressError(KiNetError):
    """A fixture scan is already running on this supply."""

    def __init__(self, host: str) -> None:
        """Store the supply address."""
        self.host = host
        super().__init__(f"A fixture scan is already running on {host}.")


@dataclass(frozen=True)
class Fixture:
    """One fixture, discovered or entered by hand."""

    key: str
    host: str
    serial_hex: str | None
    label: str | None
    dmx_start: int | None
    type_code: int | None
    present: bool
    source: str
    properties: dict[int, int]
    miss_count: int = 0

    @property
    def display_name(self) -> str:
        """Default device name. A user rename is stored on the device."""
        if self.label:
            return self.label
        if self.serial_hex:
            return f"Color Kinetics {self.serial_hex}"
        return "Color Kinetics fixture"

    @property
    def model(self) -> str:
        """Expose the fixture type code. The two captured types differ."""
        return model_name(self.type_code)


def _fixture_from_dict(raw: dict[str, Any]) -> Fixture:
    properties = {int(key): int(value) for key, value in raw.get("properties", {}).items()}
    return Fixture(
        key=str(raw["key"]),
        host=str(raw["host"]),
        serial_hex=raw.get("serial_hex"),
        label=raw.get("label"),
        dmx_start=raw.get("dmx_start"),
        type_code=raw.get("type_code"),
        present=bool(raw.get("present", True)),
        source=str(raw.get("source") or "discovery"),
        properties=properties,
        miss_count=int(raw.get("miss_count") or 0),
    )


def _fixture_to_dict(fixture: Fixture) -> dict[str, Any]:
    source = "discovery" if fixture.source == "both" else fixture.source
    return {
        "key": fixture.key,
        "host": fixture.host,
        "serial_hex": fixture.serial_hex,
        "label": fixture.label,
        "dmx_start": fixture.dmx_start,
        "type_code": fixture.type_code,
        "present": fixture.present,
        "source": source,
        "miss_count": fixture.miss_count,
        "properties": {str(key): value for key, value in fixture.properties.items()},
    }


class ColorKineticsHub:
    """Holds the socket, the fixture list, and the light-data buffers."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Create a hub. ``async_start`` opens the socket."""
        self.hass = hass
        self.entry = entry
        self.client = KiNetClient()
        self.pump = FramePump(
            min_interval=1 / MAX_FPS,
            keepalive=KEEPALIVE_SECONDS,
            send=self._transmit,
        )
        self._store: Store[dict[str, Any]] = ColorKineticsStore(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}"
        )
        self._fixtures: dict[str, Fixture] = {}
        self._supplies: dict[str, dict[str, Any]] = {}
        self._ports: dict[str, list[dict[str, int]]] = {}
        self._scanned_hosts: set[str] = set()
        self._buffers: dict[str, bytearray] = {}
        self._span: dict[str, int] = {}
        self._saved_channels: dict[str, bytes] = {}
        self._listeners: list[Listener] = []
        self._light_listeners: list[Listener] = []
        self.effects = EffectEngine()
        self.fades = FadeBook()
        self._effect_task: asyncio.Task[None] | None = None
        self._effect_ready = asyncio.Event()
        self._speeds: dict[str, float] = {}
        self._looks: dict[str, dict[str, Any]] = {}
        self.selected_look: str | None = None
        self._look_listeners: list[Listener] = []
        self._command_epoch: dict[str, int] = {}
        self._epoch = 0
        self._deferred: list[_DeferredEffects] = []
        self.fixture_lights: dict[str, Any] = {}
        self.supply_lights: dict[str, Any] = {}
        self.group_lights: dict[str, Any] = {}
        self.speed_entities: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._supply_device_ids: dict[str, str] = {}
        self._stopped = False
        self.initial_task: asyncio.Task[None] | None = None
        self.last_error: Exception | None = None

    @property
    def hosts(self) -> list[str]:
        """Configured Data Enabler addresses."""
        return hosts_of(self.entry)

    def fixtures(self) -> list[Fixture]:
        """Fixtures in DMX order."""
        return sorted(
            self._fixtures.values(),
            key=lambda fixture: (
                fixture.host,
                fixture.dmx_start if fixture.dmx_start is not None else 10_000,
                fixture.key,
            ),
        )

    def get_fixture(self, key: str) -> Fixture | None:
        """Return one fixture by its stable key."""
        return self._fixtures.get(key)

    def is_available(self, fixture: Fixture) -> bool:
        """A fixture is controllable when it has an address and was seen or entered."""
        return (
            fixture.host in self.hosts
            and fixture.present
            and fixture.dmx_start is not None
        )

    def supply_identifier(self, host: str) -> str:
        """Stable device-registry id for a supply. Serial wins over the IP."""
        info = self._supplies.get(host) or {}
        serial = info.get("serial_hex")
        if serial:
            return str(serial)
        return f"host:{host}"

    def supply_device_id(self, host: str) -> str | None:
        """Device-registry id of the supply, once it has been registered."""
        return self._supply_device_ids.get(host)

    def supply_name(self, host: str) -> str:
        """Default supply device name, including the IP so two units differ."""
        info = self._supplies.get(host) or {}
        label = info.get("label") or info.get("model") or "Data Enabler"
        return f"{label} ({host})"

    def button_unique_id(self, host: str) -> str:
        """Unique id of the rescan button for one supply."""
        return f"{self.supply_identifier(host)}:rescan"

    def all_fixtures_unique_id(self, host: str) -> str:
        """Unique id of the group light for one supply."""
        return f"{self.supply_identifier(host)}:{SUFFIX_ALL_FIXTURES}"

    def all_fixtures_owner(self, host: str) -> str:
        """Effect owner id for the All fixtures light on one supply."""
        return self.all_fixtures_unique_id(host)

    def group_owner(self, group_id: str) -> str:
        """Effect owner id for a named group. Stable for the life of the group."""
        return f"group:{group_id}"

    def fixture_order(self) -> list[str]:
        """User order, with new discoveries appended by DMX address."""
        saved = [
            str(serial)
            for serial in self.entry.options.get(CONF_FIXTURE_ORDER) or []
        ]
        discovered: list[tuple[str, int]] = []
        seen: set[str] = set()
        for fixture in self.fixtures():
            if not fixture.serial_hex or fixture.source == "manual" or fixture.serial_hex in seen:
                continue
            seen.add(fixture.serial_hex)
            address = fixture.dmx_start if fixture.dmx_start is not None else 1_000_000
            discovered.append((fixture.serial_hex, address))
        return effective_order(saved, discovered)

    def light_groups(self) -> list[dict[str, Any]]:
        """Named groups from options. Each has id, name, and member serials."""
        groups: list[dict[str, Any]] = []
        for raw in self.entry.options.get(CONF_GROUPS) or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            members = [str(serial) for serial in raw.get("members") or []]
            if not members:
                continue
            groups.append(
                {"id": str(raw["id"]), "name": str(raw.get("name") or "Group"), "members": members}
            )
        return groups

    def group_device_info(self, group: dict[str, Any]) -> DeviceInfo:
        """Attach a group to the Data Enabler of its first known fixture."""
        for serial in group.get("members") or []:
            fixture = self.get_fixture(str(serial))
            if fixture is not None and fixture.host in self._supply_device_ids:
                return self.supply_device_info(fixture.host)
        if self.hosts:
            return self.supply_device_info(self.hosts[0])
        return DeviceInfo(identifiers={(DOMAIN, self.group_owner(str(group["id"])))})

    def effect_speed(self, owner: str) -> float:
        """Slider value for an effect owner. Missing sliders use the default."""
        return float(self._speeds.get(owner, DEFAULT_EFFECT_SPEED))

    def set_effect_speed(self, owner: str, speed: float) -> None:
        """Update the speed. A running effect uses it on the next frame."""
        self._speeds[owner] = float(speed)
        run = self.effects.runs.get(owner)
        if run is not None:
            run.speed = float(speed)

    def effect_clock(self) -> float:
        """Monotonic time base for effect frames and fades."""
        return time.monotonic()

    def _frame_delay(self) -> float:
        """Seconds between animation frames. The pump still caps sends at this rate."""
        return 1 / MAX_FPS

    async def _sleep(self, delay: float) -> None:
        """Wait between frames. Tests replace this to hold the loop still."""
        await asyncio.sleep(delay)

    def ensure_effect_loop(self) -> None:
        """Start the single effect task if it is not already running."""
        if self._effect_task is None or self._effect_task.done():
            self._effect_task = asyncio.create_task(
                self._effect_loop(), name="color_kinetics effects"
            )
        self._effect_ready.set()

    def _animating(self) -> bool:
        """True while an effect, a fade, or a deferred look effect still needs frames."""
        return self.effects.active or self.fades.active or bool(self._deferred)

    def note_command(self, keys: list[str]) -> int:
        """Record that a new command owns these fixtures.

        A look fade that was waiting to start an effect will not take a fixture
        whose epoch has moved on.
        """
        self._epoch += 1
        epoch = self._epoch
        for key in keys:
            self._command_epoch[key] = epoch
        self._drop_stale_deferred()
        return epoch

    def _drop_stale_deferred(self) -> None:
        self._deferred = [
            item
            for item in self._deferred
            if any(epoch == item.epoch for epoch in self._command_epoch.values())
        ]

    def defer_effects(
        self,
        epoch: int,
        delay: float,
        effects: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Start ``effects`` after ``delay`` seconds, for fixtures still at ``epoch``."""
        if not effects or delay < 0:
            return
        self._deferred.append(
            _DeferredEffects(
                epoch=epoch,
                deadline=self.effect_clock() + delay,
                effects=list(effects),
            )
        )
        self.ensure_effect_loop()

    def deferred_effect(self, owner: str) -> dict[str, Any] | None:
        """Effect a light is fading into and has not started yet."""
        for item in self._deferred:
            for item_owner, saved in item.effects:
                if item_owner == owner:
                    return saved
        return None

    def cancel_fades(self, keys: list[str]) -> None:
        """Stop fades for the fixtures a new command is taking."""
        self.fades.cancel(keys)

    def _fixture_channel_bytes(self, fixture: Fixture) -> bytes:
        """Bytes currently in the buffer for one fixture."""
        if fixture.dmx_start is None:
            return bytes(CHANNELS_PER_FIXTURE)
        buffer = self._buffers.get(fixture.host)
        if buffer is None:
            return bytes(CHANNELS_PER_FIXTURE)
        start = fixture.dmx_start
        return bytes(buffer[start : start + CHANNELS_PER_FIXTURE])

    def start_fade(self, key: str, target: bytes, duration: float) -> None:
        """Fade one fixture toward ``target`` through the frame loop.

        A zero duration, or a target that is already showing, writes at once.
        An unavailable fixture is left alone so its saved bytes stay put.
        """
        fixture = self.get_fixture(key)
        if (
            fixture is None
            or fixture.dmx_start is None
            or fixture.host not in self.hosts
            or not self.is_available(fixture)
        ):
            self.fades.cancel([key])
            return
        current = self._fixture_channel_bytes(fixture)
        if not self.fades.start(key, current, bytes(target), self.effect_clock(), duration):
            self.fades.cancel([key])
            self.write_channels(fixture.host, fixture.dmx_start, bytes(target))
            return
        self.ensure_effect_loop()

    def render_fades(self, now: float) -> None:
        """Write one fade step. Effect-owned fixtures are not overwritten."""
        dirty: set[str] = set()
        for key, payload in self.fades.frames(now).items():
            if key in self.effects.owners:
                self.fades.cancel([key])
                continue
            fixture = self.get_fixture(key)
            if fixture is None or fixture.dmx_start is None or not self.is_available(fixture):
                continue
            if self.write_channels(fixture.host, fixture.dmx_start, payload, schedule=False):
                dirty.add(fixture.host)
        for host in dirty:
            self.pump.mark_dirty(host)

    def render_effects(self, now: float) -> None:
        """Write one effect frame into the buffers and schedule a send."""

        def dmx_of(key: str):
            fixture = self.get_fixture(key)
            return fixture.host, fixture.dmx_start  # type: ignore[union-attr]

        def available(key: str) -> bool:
            fixture = self.get_fixture(key)
            return bool(fixture and self.is_available(fixture))

        for host, regions in self.effects.frames(now, dmx_of, available).items():
            wrote = False
            for start, payload in regions:
                wrote = self.write_channels(host, start, payload, schedule=False) or wrote
            if wrote:
                self.pump.mark_dirty(host)

    def release_fixture_effect(self, key: str) -> None:
        """Drop a fixture out of any running effect. Latest manual command wins."""
        self.effects.release_fixture(key)

    def _light_for_owner(self, owner: str) -> Any:
        """All-fixtures or group light that owns ``owner``, if it still exists."""
        for light in self.supply_lights.values():
            if getattr(light, "_owner_id", None) == owner:
                return light
        for light in self.group_lights.values():
            if getattr(light, "_owner_id", None) == owner:
                return light
        return None

    def _start_deferred(self, now: float) -> None:
        """Start effects whose fade deadline has passed."""
        waiting: list[_DeferredEffects] = []
        due: list[_DeferredEffects] = []
        for item in self._deferred:
            if now >= item.deadline:
                due.append(item)
            else:
                waiting.append(item)
        self._deferred = waiting
        for deferred in due:
            allowed = {
                key
                for key, epoch in self._command_epoch.items()
                if epoch == deferred.epoch
            }
            for owner, saved in deferred.effects:
                light = self._light_for_owner(owner)
                if light is None:
                    continue
                light.apply_effect_look(saved, only_keys=allowed)

    def render_frame(self, now: float) -> None:
        """One animation step: finish due look effects, then fades, then effects."""
        self._start_deferred(now)
        self.render_fades(now)
        self.render_effects(now)

    async def _effect_loop(self) -> None:
        """Render fades and effects at the frame cap. Idle until one starts."""
        try:
            while True:
                if self._animating():
                    self.render_frame(self.effect_clock())
                    await self._sleep(self._frame_delay())
                else:
                    await self._effect_ready.wait()
                    self._effect_ready.clear()
        except asyncio.CancelledError:
            raise

    def look_names(self) -> list[str]:
        """Saved look names, sorted without regard to case."""
        return sorted(self._looks, key=str.casefold)

    def add_look_listener(self, listener: Listener) -> Any:
        """Subscribe to look list changes. Returns an unsubscribe callback."""
        self._look_listeners.append(listener)

        def _remove() -> None:
            if listener in self._look_listeners:
                self._look_listeners.remove(listener)

        return _remove

    def notify_looks(self) -> None:
        """Tell look selectors that the saved names or the selection changed."""
        for listener in list(self._look_listeners):
            listener()

    def capture_look(self) -> dict[str, Any]:
        """Per-fixture color and the effect running on each group light.

        Fixtures are keyed by serial. Effect owners are the All fixtures id
        and the group id, so a rename does not change the look.
        """
        fixtures: dict[str, Any] = {}
        for key, light in self.fixture_lights.items():
            rgb = tuple(int(channel) for channel in (light._attr_rgb_color or (255, 255, 255)))
            brightness = 255 if light._attr_brightness is None else int(light._attr_brightness)
            is_on = bool(light.is_on)
            owner = self.effects.owners.get(str(key))
            run = self.effects.runs.get(owner) if owner else None
            if run is not None and str(key) in run.order:
                is_on = True
                brightness = int(run.brightness)
            fixtures[str(key)] = {
                "is_on": is_on,
                "brightness": brightness,
                "rgb": [rgb[0], rgb[1], rgb[2]],
            }
        lights: dict[str, Any] = {}
        for light in (*self.supply_lights.values(), *self.group_lights.values()):
            run = self.effects.runs.get(light._owner_id)
            pending = self.deferred_effect(light._owner_id)
            if run is not None and run.order:
                effect = run.effect
                rgb = tuple(int(channel) for channel in run.rgb)
                brightness = int(run.brightness)
                is_on = True
            elif pending is not None and pending.get("effect") not in (None, EFFECT_SOLID):
                effect = str(pending["effect"])
                rgb = tuple(int(channel) for channel in (pending.get("rgb") or (255, 255, 255)))
                brightness = int(pending.get("brightness") or 255)
                is_on = True
            else:
                effect = EFFECT_SOLID
                rgb = tuple(
                    int(channel) for channel in (light._attr_rgb_color or (255, 255, 255))
                )
                brightness = (
                    255 if light._attr_brightness is None else int(light._attr_brightness)
                )
                is_on = bool(light.is_on)
            speed = self.effect_speed(light._owner_id)
            if pending is not None and pending.get("speed") is not None:
                try:
                    speed = float(pending["speed"])
                except (TypeError, ValueError):
                    speed = self.effect_speed(light._owner_id)
            lights[str(light._owner_id)] = {
                "effect": effect,
                "speed": speed,
                "rgb": [rgb[0], rgb[1], rgb[2]],
                "brightness": brightness,
                "is_on": is_on,
            }
        return {"fixtures": fixtures, "lights": lights}

    def _require_look_name(self, name: str) -> str:
        clean = normalize_look_name(name)
        if not clean:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="look_name",
            )
        return clean

    async def async_save_look(self, name: str) -> None:
        """Store the current fixtures and effects under ``name``.

        A name that matches an existing look in any case is rejected.
        """
        clean = self._require_look_name(name)
        if find_look_name(self._looks, clean) is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="duplicate_look",
                translation_placeholders={"name": clean},
            )
        self._looks[clean] = self.capture_look()
        self.selected_look = clean
        await self._save()
        self.notify_looks()

    async def async_delete_look(self, name: str) -> None:
        """Remove a look. Matching ignores case."""
        clean = self._require_look_name(name)
        stored = find_look_name(self._looks, clean)
        if stored is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_look",
                translation_placeholders={"name": clean},
            )
        self._looks.pop(stored, None)
        if self.selected_look == stored:
            self.selected_look = None
        await self._save()
        self.notify_looks()

    def _apply_saved_speed(self, owner: str, speed: float) -> None:
        self.set_effect_speed(owner, speed)
        slider = self.speed_entities.get(owner)
        if slider is not None:
            slider.apply_external_speed(speed)

    async def async_recall_look(self, name: str, transition: float | None = None) -> None:
        """Restore a look. Fixtures that are not here anymore are skipped.

        Fixtures that are not in the look are left as they are. With a
        transition, solid colors fade and a saved effect starts when the fade
        ends, but only on fixtures this recall still owns.
        """
        clean = self._require_look_name(name)
        stored = find_look_name(self._looks, clean)
        if stored is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_look",
                translation_placeholders={"name": clean},
            )
        look = self._looks[stored]
        raw_fixtures = look.get("fixtures") if isinstance(look, dict) else None
        if not isinstance(raw_fixtures, dict):
            raw_fixtures = {}
        present: list[tuple[Any, dict[str, Any]]] = []
        for serial, saved in raw_fixtures.items():
            if not isinstance(saved, dict):
                continue
            light = self.fixture_lights.get(str(serial))
            if light is None:
                continue
            present.append((light, saved))
        keys = [light._key for light, _saved in present]
        epoch = self.note_command(keys)
        duration = 0.0 if transition is None else max(0.0, float(transition))
        for light, saved in present:
            light.apply_look(saved, duration)
        raw_lights = look.get("lights") if isinstance(look, dict) else None
        if not isinstance(raw_lights, dict):
            raw_lights = {}
        deferred: list[tuple[str, dict[str, Any]]] = []
        allowed = set(keys)
        for owner, saved in raw_lights.items():
            if not isinstance(saved, dict):
                continue
            light = self._light_for_owner(str(owner))
            if light is None:
                continue
            try:
                speed = float(saved.get("speed", DEFAULT_EFFECT_SPEED))
            except (TypeError, ValueError):
                speed = float(DEFAULT_EFFECT_SPEED)
            self._apply_saved_speed(light._owner_id, speed)
            effect = str(saved.get("effect") or EFFECT_SOLID)
            if effect == EFFECT_SOLID:
                light.apply_effect_look(saved)
                continue
            if duration > 0:
                deferred.append((str(owner), saved))
                continue
            light.apply_effect_look(saved, only_keys=allowed)
        if deferred:
            self.defer_effects(epoch, duration, deferred)
        self.selected_look = stored
        self.notify_looks()
        self.notify_light_states()

    def supply_device_info(self, host: str) -> DeviceInfo:
        """Device registry record shared by the supply's entities."""
        info = self._supplies.get(host) or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self.supply_identifier(host))},
            name=self.supply_name(host),
            manufacturer=info.get("manufacturer") or MANUFACTURER,
            model=info.get("model") or "Data Enabler",
            sw_version=info.get("firmware") or None,
            serial_number=info.get("serial_hex"),
        )

    def fixture_device_info(self, fixture: Fixture) -> DeviceInfo:
        """Device registry record for one fixture. Firmware is not in the capture."""
        info = DeviceInfo(
            identifiers={(DOMAIN, fixture.key)},
            name=fixture.display_name,
            manufacturer=MANUFACTURER,
            model=fixture.model,
            serial_number=fixture.serial_hex,
        )
        via = self.supply_device_id(fixture.host)
        if via:
            info["via_device_id"] = via
        return info

    def supply_properties(self, host: str) -> dict[str, str]:
        """Raw supply-property values from the last read, keyed by hex id."""
        info = self._supplies.get(host) or {}
        stored = info.get("properties") or {}
        return {str(key): str(value) for key, value in stored.items()}

    def supply_property(self, host: str, prop: int) -> bytes | None:
        """One supply property value, or None when that read did not answer."""
        raw = self.supply_properties(host).get(f"{prop:04X}")
        if raw is None:
            return None
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return None

    def add_listener(self, listener: Listener) -> Any:
        """Subscribe to fixture-list changes. Returns an unsubscribe callback."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def add_light_listener(self, listener: Listener) -> Any:
        """Subscribe to individual light state changes."""
        self._light_listeners.append(listener)

        def _remove() -> None:
            if listener in self._light_listeners:
                self._light_listeners.remove(listener)

        return _remove

    def notify_light_states(self) -> None:
        """Tell group lights that a member changed color or availability."""
        for listener in list(self._light_listeners):
            listener()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def async_start(self) -> None:
        """Open the socket, load the cache, and start the frame pump."""
        self._supplies = supplies_of(self.entry)
        await self.client.start()
        stored = await self._store.async_load()
        self._load_cache(stored or {})
        self._merge_manual()
        self._restore_saved_channels()
        self._register_supply_devices()
        self._drop_stale_registry_entries()
        self.pump.start()

    async def async_stop(self) -> None:
        """Stop sending, close the socket, and drop repair issues. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        self._clear_issues()
        self.effects.runs.clear()
        self.effects.owners.clear()
        self.fades.clear()
        self._deferred.clear()
        task = self._effect_task
        self._effect_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.pump.stop()
        await self._save()
        await self.client.stop()

    async def async_initial_refresh(self) -> None:
        """Poll each supply. Scan only a supply that has never completed a scan."""
        try:
            for host in list(self.hosts):
                try:
                    await self._poll(host)
                except (KiNetError, OSError) as err:
                    _LOGGER.warning("Data Enabler %s did not answer: %s", host, err)
                    self._create_issue("supply_unreachable", host)
                    continue
                self._delete_issue("supply_unreachable", host)
                if host in self._scanned_hosts:
                    continue
                try:
                    await self.async_rescan(host)
                except ScanInProgressError:
                    _LOGGER.debug("Scan already running on %s", host)
                except (KiNetError, OSError) as err:
                    _LOGGER.warning("Fixture scan failed for %s: %s", host, err)
                    self._create_issue("scan_failed", host)
        except Exception as err:  # noqa: BLE001 - keep the background task quiet
            self.last_error = err
            _LOGGER.exception("Color Kinetics refresh failed")

    async def async_rescan(self, host: str) -> None:
        """Poll, list ports, scan fixtures, and read DMX address and type.

        Raises ScanInProgressError if a scan is already running. Other
        KiNetError failures leave the previous fixture list in place.
        """
        if host not in self.hosts:
            raise KiNetError(f"{host} is not a configured Data Enabler")
        lock = self._locks.setdefault(host, asyncio.Lock())
        if lock.locked():
            raise ScanInProgressError(host)
        async with lock:
            await self._poll(host)
            self._delete_issue("supply_unreachable", host)
            ports = await self.client.read_ports(host)
            self._ports[host] = [
                {"id": port.port_id, "type": port.port_type} for port in ports.ports
            ]
            await self._read_supply_properties(host)
            records = await self.client.scan_fixtures(host)
            serials: list[int] = []
            seen: set[int] = set()
            for record in records:
                if record.serial is None or record.serial in seen:
                    continue
                seen.add(record.serial)
                serials.append(record.serial)
            properties: dict[int, dict[int, int]] = {}
            for serial in serials:
                properties[serial] = await self._read_fixture_properties(host, serial)
            self._apply_scan(host, serials, properties)
            self._scanned_hosts.add(host)
            self._delete_issue("scan_failed", host)
            await self._save()

    async def _read_supply_properties(self, host: str) -> None:
        """Read the supply properties QuickPlay reads. One miss does not stop the rest."""
        found: dict[str, str] = {}
        for prop in SUPPLY_PROPS:
            try:
                reply = await self.client.read_supply_property(host, prop)
            except (KiNetError, OSError) as err:
                _LOGGER.debug("Supply %s property %#06x failed: %s", host, prop, err)
                continue
            found[f"{prop:04X}"] = reply.value.hex()
        self._supplies.setdefault(host, {})["properties"] = found

    async def _read_fixture_properties(self, host: str, serial: int) -> dict[int, int]:
        found: dict[int, int] = {}
        for prop in COMMON_FIXTURE_PROPS:
            value = await self._read_one_fixture_property(host, serial, prop)
            if value is not None:
                found[prop] = value
        if found.get(0x05) == TYPE_BLAST:
            for prop in GEN4_FIXTURE_PROPS:
                value = await self._read_one_fixture_property(host, serial, prop)
                if value is not None:
                    found[prop] = value
        return found

    async def _read_one_fixture_property(
        self, host: str, serial: int, prop: int
    ) -> int | None:
        """One fixture property. Timeout and a bad status are missing, not fatal."""
        try:
            reply = await self.client.read_fixture_property(host, serial, prop)
        except (KiNetError, OSError) as err:
            _LOGGER.debug(
                "Fixture %s property %#04x failed: %s",
                format_fixture_serial(serial),
                prop,
                err,
            )
            return None
        if reply.status != 0x09:
            _LOGGER.debug(
                "Fixture %s property %#04x status %#04x is not usable",
                format_fixture_serial(serial),
                prop,
                reply.status,
            )
            return None
        return reply.value

    async def _poll(self, host: str) -> SupplyInfo:
        info = await self.client.poll_supply(host)
        previous = self._supplies.get(host) or {}
        stored = supply_entry_dict(info)
        if previous.get("properties"):
            stored["properties"] = previous["properties"]
        self._supplies[host] = stored
        self._register_supply_devices()
        _LOGGER.info(
            "Data Enabler %s %s firmware %s",
            host,
            info.model or info.label,
            info.firmware,
        )
        return info

    def _apply_scan(
        self,
        host: str,
        serials: list[int],
        properties: dict[int, dict[int, int]],
    ) -> None:
        """Apply one scan. A fixture is dropped only after two scans miss it.

        The frame buffer is not cleared. A fixture that is still present
        rewrites its own slots; one that is merely missing keeps the bytes
        it last sent, so a lost reply cannot black out the run.
        """
        seen: set[str] = set()
        for serial in serials:
            key = format_fixture_serial(serial)
            seen.add(key)
            props = properties.get(serial, {})
            previous = self._fixtures.get(key)
            merged = dict(previous.properties) if previous else {}
            merged.update(props)
            dmx_start = dmx_start_zero_based(props.get(0x40), props.get(0x41))
            if (
                dmx_start is None
                and previous is not None
                and previous.dmx_start is not None
                and (0x40 not in props or 0x41 not in props)
            ):
                dmx_start = previous.dmx_start
            type_code = props.get(0x05)
            if type_code is None and previous is not None:
                type_code = previous.type_code
            self._fixtures[key] = Fixture(
                key=key,
                host=host,
                serial_hex=key,
                label=previous.label if previous else None,
                dmx_start=dmx_start,
                type_code=type_code,
                present=True,
                source="discovery",
                properties=merged,
                miss_count=0,
            )
        for key, fixture in list(self._fixtures.items()):
            if fixture.host != host or fixture.source == "manual" or key in seen:
                continue
            if fixture.serial_hex is None:
                continue
            misses = fixture.miss_count + 1
            self._fixtures[key] = replace(
                fixture,
                miss_count=misses,
                present=misses < 2,
            )
        self._merge_manual()
        self._register_fixture_devices()
        self._notify()
        if self._outgoing_length(host):
            self.pump.mark_dirty(host)
        _LOGGER.info("Fixture scan of %s found %s fixture(s)", host, len(seen))

    def _merge_manual(self) -> None:
        """Overlay the options-flow fixture list onto the discovery cache."""
        self._fixtures = {
            key: fixture
            for key, fixture in self._fixtures.items()
            if fixture.source != "manual"
        }
        for key, fixture in list(self._fixtures.items()):
            if fixture.source == "both":
                self._fixtures[key] = replace(fixture, source="discovery")
        try:
            manuals = parse_manual_fixtures(manual_text_of(self.entry), self.hosts)
        except ManualFixtureError:
            _LOGGER.warning("Ignoring invalid manual fixture list")
            return
        for manual in manuals:
            self._apply_manual(manual)

    def _apply_manual(self, manual: ManualFixture) -> None:
        current = self._fixtures.get(manual.key)
        if current is not None and current.serial_hex:
            if current.present and current.dmx_start is not None:
                if current.dmx_start != manual.dmx_start:
                    _LOGGER.warning(
                        "Manual DMX %s for %s ignored; discovery reported %s",
                        manual.dmx_start + 1,
                        manual.key,
                        current.dmx_start + 1,
                    )
                dmx_start = current.dmx_start
            else:
                dmx_start = manual.dmx_start
            self._fixtures[manual.key] = replace(
                current,
                label=manual.label or current.label,
                dmx_start=dmx_start,
                present=True,
                source="both",
                host=current.host if current.present else manual.host,
            )
            return
        self._fixtures[manual.key] = Fixture(
            key=manual.key,
            host=manual.host,
            serial_hex=manual.serial_hex,
            label=manual.label,
            dmx_start=manual.dmx_start,
            type_code=None,
            present=True,
            source="manual",
            properties={},
        )

    def write_channels(
        self, host: str, start: int, data: bytes, *, schedule: bool = True
    ) -> bool:
        """Write channel bytes into the supply buffer and, by default, schedule a send."""
        if host not in self.hosts:
            return False
        end = start + len(data)
        if start < 0 or end > DMX_SLOTS:
            _LOGGER.warning(
                "DMX channels %s-%s on %s are outside 1-512", start + 1, end, host
            )
            return False
        buffer = self._buffers.setdefault(host, bytearray(DMX_SLOTS))
        buffer[start:end] = data
        self._span[host] = max(self._span.get(host, 0), end)
        self._remember_fixture_bytes(host, start, end)
        if schedule:
            self.pump.mark_dirty(host)
        return True

    def _remember_fixture_bytes(self, host: str, start: int, end: int) -> None:
        """Record the channels just written for each fixture the write covered."""
        buffer = self._buffers[host]
        for fixture in self._fixtures.values():
            if fixture.host != host or fixture.dmx_start is None:
                continue
            slot = fixture.dmx_start
            slot_end = slot + CHANNELS_PER_FIXTURE
            if start <= slot and end >= slot_end:
                self._saved_channels[fixture.key] = bytes(buffer[slot:slot_end])

    def write_same_channels(self, host: str, starts: list[int], data: bytes) -> None:
        """Write one payload at several DMX starts and schedule a single send."""
        wrote = False
        for start in starts:
            wrote = self.write_channels(host, start, data, schedule=False) or wrote
        if wrote:
            self.pump.mark_dirty(host)

    def _splice_unavailable(self, host: str) -> None:
        """Restore the last sent color for fixtures this session is not driving.

        A fixture Home Assistant has never set has no saved bytes, so its
        slots stay at the buffer's zeros and the light stays off. A saved
        color is copied back, including ``00 00 00`` after the user turned
        that fixture off.
        """
        buffer = self._buffers.setdefault(host, bytearray(DMX_SLOTS))
        for fixture in self._fixtures.values():
            if fixture.host != host or fixture.dmx_start is None or self.is_available(fixture):
                continue
            saved = self._saved_channels.get(fixture.key)
            if not saved:
                continue
            start = fixture.dmx_start
            buffer[start : start + len(saved)] = saved

    def _outgoing_length(self, host: str) -> int:
        """Full addressed span, at least the 24-byte OLA minimum.

        Every fixture with a DMX address is inside the frame. Slots with no
        saved color are zero, which is off. Returns 0 when this supply has
        nothing addressed and nothing written, so the pump holds the send.
        """
        self._splice_unavailable(host)
        end = self._span.get(host, 0)
        addressed = False
        for fixture in self._fixtures.values():
            if fixture.host != host or fixture.dmx_start is None:
                continue
            addressed = True
            end = max(end, fixture.dmx_start + CHANNELS_PER_FIXTURE)
        if end <= 0 and not addressed:
            return 0
        return max(end, PORTOUT_MIN_DATA)

    def _transmit(self, host: str) -> bool:
        if host not in self.hosts:
            self.pump.forget(host)
            return False
        length = self._outgoing_length(host)
        if length <= 0:
            return False
        buffer = self._buffers.setdefault(host, bytearray(DMX_SLOTS))
        data = bytes(buffer[:length])
        protocol, version, port = output_settings_of(self.entry)
        sequence = self.client.allocate_sequence()
        if protocol == "dmxout":
            packet = build_dmxout(sequence, data)
        else:
            packet = build_portout(sequence, port, data, version=version)
        self.client.send(host, packet)
        return True

    def _restore_saved_channels(self) -> None:
        """Write stored fixture colors into the buffers before the pump runs."""
        for key, payload in list(self._saved_channels.items()):
            fixture = self._fixtures.get(key)
            if fixture is None or fixture.dmx_start is None:
                continue
            self.write_channels(fixture.host, fixture.dmx_start, payload, schedule=False)

    def _load_cache(self, stored: dict[str, Any]) -> None:
        for host, info in stored.get("supplies", {}).items():
            self._supplies.setdefault(str(host), dict(info))
        self._scanned_hosts = {str(host) for host in stored.get("scanned_hosts", [])}
        self._ports = {
            str(host): list(ports) for host, ports in stored.get("ports", {}).items()
        }
        for key, raw in stored.get("fixtures", {}).items():
            try:
                fixture = _fixture_from_dict(raw)
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Skipping stored fixture %s", key)
                continue
            if fixture.source == "manual":
                continue
            self._fixtures[fixture.key] = fixture
        for key, raw in stored.get("channels", {}).items():
            if not isinstance(raw, str):
                continue
            try:
                payload = bytes.fromhex(raw)
            except ValueError:
                _LOGGER.warning("Skipping stored channels for %s", key)
                continue
            if payload:
                self._saved_channels[str(key)] = payload
        raw_looks = stored.get("looks")
        self._looks = {}
        if isinstance(raw_looks, dict):
            for name, look in raw_looks.items():
                if isinstance(name, str) and name.strip() and isinstance(look, dict):
                    self._looks[name] = look
        selected = stored.get("selected_look")
        if isinstance(selected, str) and selected in self._looks:
            self.selected_look = selected
        else:
            self.selected_look = None

    def _cache(self) -> dict[str, Any]:
        fixtures = {
            fixture.key: _fixture_to_dict(fixture)
            for fixture in self._fixtures.values()
            if fixture.source != "manual"
        }
        return {
            "supplies": self._supplies,
            "scanned_hosts": sorted(self._scanned_hosts),
            "ports": self._ports,
            "fixtures": fixtures,
            "channels": {
                key: payload.hex() for key, payload in self._saved_channels.items() if payload
            },
            "looks": self._looks,
            "selected_look": self.selected_look,
        }

    async def _save(self) -> None:
        await self._store.async_save(self._cache())

    def _register_supply_devices(self) -> None:
        registry = dr.async_get(self.hass)
        hosts = set(self.hosts)
        hosts.update(fixture.host for fixture in self._fixtures.values())
        for host in hosts:
            info = self._supplies.get(host) or {}
            identifier = self.supply_identifier(host)
            connections: set[tuple[str, str]] | None = None
            if mac := info.get("mac"):
                connections = {(dr.CONNECTION_NETWORK_MAC, str(mac))}
            kwargs: dict[str, Any] = {
                "config_entry_id": self.entry.entry_id,
                "identifiers": {(DOMAIN, identifier)},
                "name": self.supply_name(host),
                "manufacturer": info.get("manufacturer") or MANUFACTURER,
                "model": info.get("model") or "Data Enabler",
                "sw_version": info.get("firmware") or None,
                "serial_number": info.get("serial_hex"),
            }
            if connections:
                kwargs["connections"] = connections
            device = registry.async_get_or_create(**kwargs)
            self._supply_device_ids[host] = device.id

    def _register_fixture_devices(self) -> None:
        registry = dr.async_get(self.hass)
        for fixture in self._fixtures.values():
            kwargs: dict[str, Any] = {
                "config_entry_id": self.entry.entry_id,
                "identifiers": {(DOMAIN, fixture.key)},
                "name": fixture.display_name,
                "manufacturer": MANUFACTURER,
                "model": fixture.model,
                "serial_number": fixture.serial_hex,
            }
            via = self._supply_device_ids.get(fixture.host)
            if via:
                kwargs["via_device_id"] = via
            registry.async_get_or_create(**kwargs)

    def _persistent_unique_ids(self) -> set[str]:
        """Entity ids that must survive a restart's stale-entry cleanup."""
        keep = {fixture.key for fixture in self._fixtures.values()}
        for host in self.hosts:
            ident = self.supply_identifier(host)
            keep.add(self.button_unique_id(host))
            keep.add(self.all_fixtures_unique_id(host))
            keep.add(f"{self.all_fixtures_owner(host)}:{SUFFIX_EFFECT_SPEED}")
            keep.add(f"{ident}:{SUFFIX_LOOK}")
            keep.add(f"{ident}:{SUFFIX_POWER_TIME}")
            keep.add(f"{ident}:{SUFFIX_RAW}")
        for group in self.light_groups():
            keep.add(self.group_owner(group["id"]))
            keep.add(f"{self.group_owner(group['id'])}:{SUFFIX_EFFECT_SPEED}")
        for fixture in self._fixtures.values():
            if not fixture.serial_hex or fixture.source == "manual":
                continue
            key = fixture.serial_hex
            keep.add(f"{key}:{SUFFIX_DMX}")
            keep.add(f"{key}:{SUFFIX_STARTUP}")
            keep.add(f"{key}:{SUFFIX_RAW}")
            if fixture.type_code == TYPE_BLAST:
                keep.add(f"{key}:{SUFFIX_SCALE}")
        return keep

    def _drop_stale_registry_entries(self) -> None:
        keep_entities = self._persistent_unique_ids()
        entity_registry = er.async_get(self.hass)
        for entity in er.async_entries_for_config_entry(
            entity_registry, self.entry.entry_id
        ):
            if entity.unique_id not in keep_entities:
                entity_registry.async_remove(entity.entity_id)
        keep_devices = {fixture.key for fixture in self._fixtures.values()}
        keep_devices.update(self.supply_identifier(host) for host in self.hosts)
        keep_devices.update(
            self.supply_identifier(fixture.host) for fixture in self._fixtures.values()
        )
        device_registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(
            device_registry, self.entry.entry_id
        ):
            identifiers = {
                identifier
                for domain, identifier in device.identifiers
                if domain == DOMAIN
            }
            if identifiers and identifiers.isdisjoint(keep_devices):
                device_registry.async_remove_device(device.id)

    def _clear_issues(self) -> None:
        """Remove repair issues for every supply this entry has talked to."""
        hosts = set(self.hosts)
        hosts.update(self._supplies)
        for host in hosts:
            self._delete_issue("supply_unreachable", host)
            self._delete_issue("scan_failed", host)

    def _issue_id(self, kind: str, host: str) -> str:
        return f"{kind}_{host.replace('.', '_')}"

    def _create_issue(self, kind: str, host: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id(kind, host),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=kind,
            translation_placeholders={"host": host},
        )

    def _delete_issue(self, kind: str, host: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(kind, host))


type ColorKineticsConfigEntry = ConfigEntry[ColorKineticsHub]
