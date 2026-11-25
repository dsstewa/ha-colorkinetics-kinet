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
    DEFAULT_EFFECT_DIRECTION,
    DEFAULT_EFFECT_INTENSITY,
    DEFAULT_EFFECT_PALETTE,
    DEFAULT_EFFECT_SPEED,
    DOMAIN,
    KEEPALIVE_SECONDS,
    MANUFACTURER,
    MAX_FPS,
)
from .entry_data import (
    clamp_fade_seconds,
    fade_seconds_of,
    follow_of,
    hosts_of,
    manual_text_of,
    options_signature,
    output_settings_of,
    supplies_of,
)
from .manual import ManualFixture, ManualFixtureError, parse_manual_fixtures
from .effects import (
    EFFECT_NAMES,
    EFFECT_SOLID,
    EffectEngine,
    clip_intensity,
    fixture_channels,
    legacy_palette,
    normalize_direction,
    normalize_palette,
    resolve_effect,
)
from .fade import FadeBook
from .looks import find_look_name, normalize_look_name
from .order import effective_order
from .properties import (
    COMMON_FIXTURE_PROPS,
    GEN4_FIXTURE_PROPS,
    SUFFIX_ALL_FIXTURES,
    SUFFIX_ALL_OFF,
    SUFFIX_DMX,
    SUFFIX_DEFAULT_FADE,
    SUFFIX_EFFECT_DIRECTION,
    SUFFIX_EFFECT_INTENSITY,
    SUFFIX_EFFECT_PALETTE,
    SUFFIX_EFFECT_SPEED,
    SUFFIX_LOOK,
    SUFFIX_PANEL_BRIGHTNESS,
    SUFFIX_PANEL_EFFECT,
    SUFFIX_PANEL_SCENE,
    SUFFIX_PANEL_TARGET,
    SUFFIX_POWER_TIME,
    SUFFIX_RAW,
    SUFFIX_SCALE,
    SUFFIX_STARTUP,
    SUFFIX_STOP_EFFECT,
    SUPPLY_PROPS,
    TYPE_BLAST,
    model_name,
)
from .scenes import (
    EFFECT_NONE,
    MODE_EFFECT,
    MODE_IDLE,
    MODE_OFF,
    MODE_SCENE,
    TARGET_ALL,
    TARGET_ALL_LABEL,
    clamp_percent,
    default_panel,
    default_scenes,
    find_scene_name,
    normalize_effect_option,
    normalize_panel,
    normalize_scene,
    normalize_scene_map,
    percent_to_brightness,
    resolve_scene,
)
from .show import chain_members, retarget_origin, root_of, show_phase
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

STORAGE_VERSION = 3

Listener = Any


class ColorKineticsStore(Store[dict[str, Any]]):
    """Fixture cache.

    Version 2 adds named looks beside the saved channels. Version 3 adds
    per-hub scenes and the control-panel selection.
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep an older cache and add the maps it does not have yet."""
        if isinstance(old_data, dict):
            data = dict(old_data)
        else:
            data = {}
        if old_major_version < 2 and not isinstance(data.get("looks"), dict):
            data["looks"] = {}
        if old_major_version < STORAGE_VERSION:
            if not isinstance(data.get("looks"), dict):
                data["looks"] = {}
            if not isinstance(data.get("scenes"), dict):
                data["scenes"] = {}
            if not isinstance(data.get("panel"), dict):
                data["panel"] = {}
            if not isinstance(data.get("scene_hosts"), list):
                data["scene_hosts"] = []
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
        self._intensities: dict[str, float] = {}
        self._palettes: dict[str, str] = {}
        self._directions: dict[str, str] = {}
        self._looks: dict[str, dict[str, Any]] = {}
        self.selected_look: str | None = None
        self._look_listeners: list[Listener] = []
        self._scenes: dict[str, dict[str, dict[str, Any]]] = {}
        self._scene_hosts: list[str] = []
        self._panel: dict[str, dict[str, Any]] = {}
        self._panel_explicit: set[str] = set()
        self._panel_touched: set[str] = set()
        self._panel_applied: set[str] = set()
        self._restore_seeds: dict[str, dict[str, Any]] = {}
        self._panel_listeners: list[Listener] = []
        self._show_clocks: dict[str, tuple[float, float]] = {}
        self._adopted_clocks: set[str] = set()
        self._sync_depth = 0
        self._applying = False
        self._publish_tasks: set[asyncio.Task[Any]] = set()
        self._task_serial = 0
        self._followed_entry = follow_of(entry)
        self._command_epoch: dict[str, int] = {}
        self._epoch = 0
        self._deferred: list[_DeferredEffects] = []
        self.fixture_lights: dict[str, Any] = {}
        self.supply_lights: dict[str, Any] = {}
        self.group_lights: dict[str, Any] = {}
        self.speed_entities: dict[str, Any] = {}
        self.intensity_entities: dict[str, Any] = {}
        self.palette_entities: dict[str, Any] = {}
        self.direction_entities: dict[str, Any] = {}
        self.fade_entities: dict[str, Any] = {}
        self._default_fade = fade_seconds_of(entry)
        self._options_signature = options_signature(dict(entry.options))
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
        """Update the speed. A running effect uses it on the next frame.

        The panel target's speed is also the show speed. The leader retargets
        the shared origin so the phase does not jump, then followers copy it.
        """
        self._speeds[owner] = float(speed)
        run = self.effects.runs.get(owner)
        if run is not None:
            run.speed = float(speed)
        host = self._host_for_panel_owner(owner)
        if host is None or self._syncing or self._applying:
            return
        if self.panel_state(host).get("mode") != MODE_EFFECT and host not in self._show_clocks:
            return
        self._retarget_show_clock(host, float(speed))
        self._schedule_publish(host)

    def effect_intensity(self, owner: str) -> float:
        """Intensity slider for an effect owner. Missing sliders use 5."""
        return float(self._intensities.get(owner, DEFAULT_EFFECT_INTENSITY))

    def set_effect_intensity(self, owner: str, intensity: float) -> None:
        """Update intensity. The cycle position stays where it is."""
        level = float(clip_intensity(intensity))
        self._intensities[owner] = level
        run = self.effects.runs.get(owner)
        if run is not None:
            run.intensity = level

    def effect_palette(self, owner: str) -> str:
        """Palette selected for an effect owner."""
        return normalize_palette(self._palettes.get(owner, DEFAULT_EFFECT_PALETTE))

    def set_effect_palette(self, owner: str, palette: str) -> None:
        """Update the palette. The cycle position stays where it is."""
        name = normalize_palette(palette)
        self._palettes[owner] = name
        run = self.effects.runs.get(owner)
        if run is not None:
            run.palette = name

    def effect_direction(self, owner: str) -> str:
        """Forward or Reverse for an effect owner."""
        return normalize_direction(self._directions.get(owner, DEFAULT_EFFECT_DIRECTION))

    def set_effect_direction(self, owner: str, direction: str) -> None:
        """Update direction along the fixture order. Phase stays put."""
        name = normalize_direction(direction)
        self._directions[owner] = name
        run = self.effects.runs.get(owner)
        if run is not None:
            run.direction = name

    def _effect_style(self, owner: str) -> dict[str, Any]:
        """Intensity, palette, and direction currently selected for ``owner``."""
        return {
            "intensity": self.effect_intensity(owner),
            "palette": self.effect_palette(owner),
            "direction": self.effect_direction(owner),
        }

    @property
    def default_fade(self) -> float:
        """Seconds to fade when a light command omits ``transition``. 0 snaps."""
        return self._default_fade

    def set_default_fade(self, seconds: Any) -> None:
        """Store the default fade and show it on each Data Enabler number."""
        self._default_fade = clamp_fade_seconds(seconds)
        for entity in self.fade_entities.values():
            entity.apply_fade(self._default_fade)

    def absorb_fade_option(self) -> bool:
        """Apply a fade or follow-hub change without reloading. False reloads."""
        if self._stopped:
            return False
        signature = options_signature(dict(self.entry.options))
        if signature != self._options_signature:
            return False
        previous = self._followed_entry
        self.set_default_fade(fade_seconds_of(self.entry))
        current = follow_of(self.entry)
        if current != previous:
            self._followed_entry = current
            self.note_follow_option()
        return True

    @property
    def _syncing(self) -> bool:
        """True while one or more leader mirrors are being applied.

        A counter, not a bool: the first accept to finish must not clear the
        flag while another accept is still inside ``async_turn_on``.
        """
        return self._sync_depth > 0

    def effect_clock(self) -> float:
        """Monotonic time base for effect frames and fades."""
        return time.monotonic()

    def show_clock(self) -> float:
        """Wall time for a multi-hub show.

        Monotonic time starts over when Home Assistant restarts, so a shared
        show uses the wall clock. Every hub passes the same origin and reads
        the same formula, which keeps the phase together.
        """
        return time.time()

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

    def start_fade(
        self, key: str, target: bytes, duration: float, now: float | None = None
    ) -> bool:
        """Fade one fixture toward ``target`` through the frame loop.

        A zero duration, or a target that is already showing, writes at once
        and returns false. An unavailable fixture is left alone so its saved
        bytes stay put. ``now`` is shared by a group so every member starts
        together. One later frame tick sends one datagram for the supply.
        """
        fixture = self.get_fixture(key)
        if (
            fixture is None
            or fixture.dmx_start is None
            or fixture.host not in self.hosts
            or not self.is_available(fixture)
        ):
            self.fades.cancel([key])
            return False
        current = self._fixture_channel_bytes(fixture)
        moment = self.effect_clock() if now is None else float(now)
        if not self.fades.start(key, current, bytes(target), moment, duration):
            self.fades.cancel([key])
            self.write_channels(fixture.host, fixture.dmx_start, bytes(target))
            return False
        self.ensure_effect_loop()
        return True

    def render_fades(self, now: float) -> None:
        """Write one fade step for every fixture, then one send per supply.

        A fade that reaches its target is removed. A turn-off fade then marks
        that light off. Effect-owned fixtures are not overwritten.
        """
        dirty: set[str] = set()
        finished: list[str] = []
        for key, payload in self.fades.frames(now).items():
            if key in self.effects.owners:
                self.fades.cancel([key])
                continue
            fixture = self.get_fixture(key)
            if fixture is None or fixture.dmx_start is None or not self.is_available(fixture):
                if not self.fades.contains(key):
                    finished.append(key)
                continue
            if self.write_channels(fixture.host, fixture.dmx_start, payload, schedule=False):
                dirty.add(fixture.host)
            if not self.fades.contains(key):
                finished.append(key)
        for host in dirty:
            self.pump.mark_dirty(host)
        for key in finished:
            light = self.fixture_lights.get(key)
            if light is not None:
                light.complete_fade()
        if finished:
            self.notify_light_states()

    def render_effects(self, now: float) -> None:
        """Write one effect frame into the buffers and schedule a send."""

        def dmx_of(key: str):
            fixture = self.get_fixture(key)
            return fixture.host, fixture.dmx_start  # type: ignore[union-attr]

        def available(key: str) -> bool:
            fixture = self.get_fixture(key)
            return bool(fixture and self.is_available(fixture))

        for host, regions in self.effects.frames(
            now, dmx_of, available, syncs=self._effect_syncs()
        ).items():
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
                rgb = tuple(int(channel) for channel in run.rgb)
            fixtures[str(key)] = {
                "is_on": is_on,
                "brightness": brightness,
                "rgb": [rgb[0], rgb[1], rgb[2]],
                "color_mode": light.color_mode,
                "color_temp_kelvin": light.color_temp_kelvin,
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
            style = self._effect_style(light._owner_id)
            if pending is not None:
                if pending.get("intensity") is not None:
                    style["intensity"] = clip_intensity(pending["intensity"])
                if pending.get("palette"):
                    style["palette"] = normalize_palette(str(pending["palette"]))
                if pending.get("direction"):
                    style["direction"] = normalize_direction(str(pending["direction"]))
            lights[str(light._owner_id)] = {
                "effect": effect,
                "speed": speed,
                "intensity": style["intensity"],
                "palette": style["palette"],
                "direction": style["direction"],
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

    def _effect_first_frames(
        self, raw_lights: dict[str, Any], allowed: set[str]
    ) -> dict[str, bytes]:
        """First frame of each saved animated effect, keyed by fixture.

        Recall fades an effect-owned fixture toward this, not toward a color
        the fixture entity still had from before the effect.
        """
        frames: dict[str, bytes] = {}
        for owner, saved in raw_lights.items():
            if not isinstance(saved, dict):
                continue
            raw_effect = str(saved.get("effect") or EFFECT_SOLID)
            effect = resolve_effect(raw_effect)
            if effect not in EFFECT_NAMES or effect == EFFECT_SOLID:
                continue
            light = self._light_for_owner(str(owner))
            if light is None:
                continue
            members = [member for member in light._members() if member._key in allowed]
            rgb = tuple(int(channel) for channel in (saved.get("rgb") or (255, 255, 255)))
            try:
                brightness = int(saved.get("brightness") if saved.get("brightness") is not None else 255)
            except (TypeError, ValueError):
                brightness = 255
            intensity = saved.get("intensity", DEFAULT_EFFECT_INTENSITY)
            palette = legacy_palette(raw_effect) or saved.get("palette") or DEFAULT_EFFECT_PALETTE
            direction = saved.get("direction", DEFAULT_EFFECT_DIRECTION)
            count = len(members)
            for index, member in enumerate(members):
                frames[member._key] = fixture_channels(
                    effect,
                    index,
                    count,
                    0.0,
                    rgb,
                    brightness,
                    intensity,
                    str(palette),
                    str(direction),
                )
        return frames

    def _apply_saved_speed(self, owner: str, speed: float) -> None:
        self.set_effect_speed(owner, speed)
        slider = self.speed_entities.get(owner)
        if slider is not None:
            slider.apply_external_speed(speed)

    def _apply_saved_style(self, owner: str, saved: dict[str, Any]) -> None:
        """Restore intensity, palette, and direction. Missing keys keep the current values."""
        raw_effect = str(saved.get("effect") or "")
        forced = legacy_palette(raw_effect)
        if saved.get("intensity") is not None:
            try:
                level = float(saved["intensity"])
            except (TypeError, ValueError):
                level = float(DEFAULT_EFFECT_INTENSITY)
            self.set_effect_intensity(owner, level)
            slider = self.intensity_entities.get(owner)
            if slider is not None:
                slider.apply_external_speed(self.effect_intensity(owner))
        palette = forced if forced is not None else saved.get("palette")
        if palette:
            self.set_effect_palette(owner, str(palette))
            picker = self.palette_entities.get(owner)
            if picker is not None:
                picker.apply_external(self.effect_palette(owner))
        if saved.get("direction"):
            self.set_effect_direction(owner, str(saved["direction"]))
            picker = self.direction_entities.get(owner)
            if picker is not None:
                picker.apply_external(self.effect_direction(owner))

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
        raw_lights = look.get("lights") if isinstance(look, dict) else None
        if not isinstance(raw_lights, dict):
            raw_lights = {}
        allowed = set(keys)
        first_frames = self._effect_first_frames(raw_lights, allowed)
        for light, saved in present:
            light.apply_look(saved, duration, first_frames.get(light._key))
        deferred: list[tuple[str, dict[str, Any]]] = []
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
            self._apply_saved_style(light._owner_id, saved)
            effect = resolve_effect(str(saved.get("effect") or EFFECT_SOLID))
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
        self._default_fade = fade_seconds_of(self.entry)
        self._options_signature = options_signature(dict(self.entry.options))
        self._followed_entry = follow_of(self.entry)
        self.pump.start()

    async def async_stop(self) -> None:
        """Stop sending, close the socket, and drop repair issues. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        pending = list(self._publish_tasks)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._publish_tasks.clear()
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
            await self.async_restore_panel()
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
            await self.async_restore_panel()

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
        self._scenes = {}
        raw_scenes = stored.get("scenes")
        if isinstance(raw_scenes, dict):
            for host, scenes in raw_scenes.items():
                self._scenes[str(host)] = normalize_scene_map(scenes)
        raw_ready = stored.get("scene_hosts")
        self._scene_hosts = [str(host) for host in raw_ready] if isinstance(raw_ready, list) else []
        self._panel = {}
        self._panel_explicit = set()
        self._show_clocks = {}
        raw_panel = stored.get("panel")
        if isinstance(raw_panel, dict):
            for host, item in raw_panel.items():
                panel = normalize_panel(item)
                self._panel[str(host)] = panel
                self._panel_explicit.add(str(host))
                if panel.get("show_origin") is not None and panel.get("show_speed") is not None:
                    self._show_clocks[str(host)] = (
                        float(panel["show_origin"]),
                        float(panel["show_speed"]),
                    )
                if panel.get("show_adopted"):
                    self._adopted_clocks.add(str(host))
        self._ensure_default_scenes()
        for host in self.hosts:
            self._panel.setdefault(host, default_panel())

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
            "scenes": self._scenes,
            "scene_hosts": list(self._scene_hosts),
            "panel": {
                host: self._panel[host]
                for host in self._panel
                if host in self._panel_explicit or host in self._panel_touched
            },
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
            keep.add(f"{self.all_fixtures_owner(host)}:{SUFFIX_EFFECT_INTENSITY}")
            keep.add(f"{self.all_fixtures_owner(host)}:{SUFFIX_EFFECT_PALETTE}")
            keep.add(f"{self.all_fixtures_owner(host)}:{SUFFIX_EFFECT_DIRECTION}")
            keep.add(f"{ident}:{SUFFIX_LOOK}")
            keep.add(f"{ident}:{SUFFIX_PANEL_EFFECT}")
            keep.add(f"{ident}:{SUFFIX_PANEL_SCENE}")
            keep.add(f"{ident}:{SUFFIX_PANEL_TARGET}")
            keep.add(f"{ident}:{SUFFIX_PANEL_BRIGHTNESS}")
            keep.add(f"{ident}:{SUFFIX_STOP_EFFECT}")
            keep.add(f"{ident}:{SUFFIX_ALL_OFF}")
            keep.add(f"{ident}:{SUFFIX_DEFAULT_FADE}")
            keep.add(f"{ident}:{SUFFIX_POWER_TIME}")
            keep.add(f"{ident}:{SUFFIX_RAW}")
        for group in self.light_groups():
            keep.add(self.group_owner(group["id"]))
            keep.add(f"{self.group_owner(group['id'])}:{SUFFIX_EFFECT_SPEED}")
            keep.add(f"{self.group_owner(group['id'])}:{SUFFIX_EFFECT_INTENSITY}")
            keep.add(f"{self.group_owner(group['id'])}:{SUFFIX_EFFECT_PALETTE}")
            keep.add(f"{self.group_owner(group['id'])}:{SUFFIX_EFFECT_DIRECTION}")
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

    def _ensure_default_scenes(self) -> None:
        """Give each supply the built-in scenes once.

        A host stays in ``_scene_hosts`` after the user deletes a scene, so
        the defaults are not put back on the next start.
        """
        for host in self.hosts:
            if host in self._scene_hosts:
                self._scenes.setdefault(host, {})
                continue
            self._scenes[host] = default_scenes()
            self._scene_hosts.append(host)

    def panel_state(self, host: str) -> dict[str, Any]:
        """Copy of the control-panel record for one Data Enabler."""
        return dict(self._panel.get(host) or default_panel())

    def scene_names(self, host: str) -> list[str]:
        """Scene names for one supply, sorted without regard to case."""
        return sorted(self._scenes.get(host, {}), key=str.casefold)

    def scene_record(self, host: str, name: str) -> dict[str, Any] | None:
        """The stored scene, matched without regard to case."""
        stored = find_scene_name(self._scenes.get(host, {}), name)
        if stored is None:
            return None
        return self._scenes[host][stored]

    def panel_target(self, host: str) -> str:
        """Group id, or all fixtures. A deleted group falls back to all."""
        target = str(self.panel_state(host).get("target") or TARGET_ALL)
        if target == TARGET_ALL:
            return TARGET_ALL
        if any(group["id"] == target for group in self.light_groups()):
            return target
        return TARGET_ALL

    def target_options(self, host: str) -> list[str]:
        """Labels for the target selector. The value stored is the group id."""
        del host
        labels = [TARGET_ALL_LABEL]
        for group in self.light_groups():
            labels.append(self._group_label(group))
        return labels

    def target_option(self, host: str) -> str:
        """Label currently selected for one supply."""
        target = self.panel_target(host)
        if target == TARGET_ALL:
            return TARGET_ALL_LABEL
        for group in self.light_groups():
            if group["id"] == target:
                return self._group_label(group)
        return TARGET_ALL_LABEL

    def _group_label(self, group: dict[str, Any]) -> str:
        name = str(group.get("name") or "Group")
        if name == TARGET_ALL_LABEL:
            return f"{name} ({str(group['id'])[:4]})"
        return name

    def _target_id_from_option(self, option: str) -> str:
        if option == TARGET_ALL_LABEL:
            return TARGET_ALL
        for group in self.light_groups():
            if self._group_label(group) == option:
                return str(group["id"])
        return TARGET_ALL

    def _panel_owner(self, host: str) -> str:
        """Effect owner the panel is driving on this supply."""
        target = self.panel_target(host)
        if target == TARGET_ALL:
            return self.all_fixtures_owner(host)
        return self.group_owner(target)

    def _host_for_panel_owner(self, owner: str) -> str | None:
        """Supply whose panel target uses ``owner``, if any."""
        for host in self.hosts:
            if self._panel_owner(host) == owner:
                return host
        return None

    def _target_light(self, host: str):
        """All-fixtures or group light the panel currently addresses."""
        target = self.panel_target(host)
        if target == TARGET_ALL:
            return self.supply_lights.get(host)
        return self.group_lights.get(target)

    def target_keys(self, host: str) -> list[str]:
        """Fixture keys the panel target will paint, in show order."""
        light = self._target_light(host)
        if light is None:
            return []
        return [member._key for member in light._members()]

    def add_panel_listener(self, listener: Listener) -> Any:
        """Subscribe to panel changes. Returns an unsubscribe callback."""
        self._panel_listeners.append(listener)

        def _remove() -> None:
            if listener in self._panel_listeners:
                self._panel_listeners.remove(listener)

        return _remove

    def notify_panel(self) -> None:
        """Tell the control-panel entities to read the hub again."""
        for listener in list(self._panel_listeners):
            listener()

    def adopt_restore(self, host: str, **seed: Any) -> None:
        """Remember an entity restore when storage has no panel for this supply.

        Storage wins once a panel record has been saved. The entity restore
        only fills a hub that was upgraded before the panel existed.
        """
        if host in self._panel_explicit or self._stopped:
            return
        bucket = self._restore_seeds.setdefault(host, {})
        for key, value in seed.items():
            if value is not None:
                bucket[key] = value

    def _merge_seeds(self) -> None:
        for host, seed in self._restore_seeds.items():
            if host in self._panel_explicit:
                continue
            state = self.panel_state(host)
            if "brightness" in seed:
                state["brightness"] = clamp_percent(seed["brightness"])
            effect = seed.get("effect")
            scene = seed.get("scene")
            if effect and normalize_effect_option(str(effect)) != EFFECT_NONE:
                state["effect"] = normalize_effect_option(str(effect))
                state["scene"] = None
                state["mode"] = MODE_EFFECT
            elif scene:
                stored = find_scene_name(self._scenes.get(host, {}), str(scene))
                if stored is not None:
                    state["scene"] = stored
                    state["effect"] = EFFECT_NONE
                    state["mode"] = MODE_SCENE
            self._panel[host] = normalize_panel(state)
            self._panel_touched.add(host)
        self._restore_seeds.clear()

    def _can_apply(self, host: str) -> bool:
        """False until the target light exists and a pending show has fixtures."""
        if self._target_light(host) is None:
            return False
        state = self.panel_state(host)
        if state["mode"] in (MODE_IDLE, MODE_OFF):
            return True
        if self.target_keys(host):
            return True
        return host in self._scanned_hosts

    async def async_restore_panel(self) -> None:
        """Apply the stored effect or scene once the lights exist.

        Called after the platforms are up, and again after a scan, so a
        first-time discovery still resumes the selection.
        """
        if self._stopped:
            return
        self._merge_seeds()
        for host in self.hosts:
            if host in self._panel_applied or not self._can_apply(host):
                continue
            await self._apply_mode(host, restart_clock=False)
            self._panel_applied.add(host)
            self._panel_touched.add(host)
        if self._panel_touched:
            await self._save()
        self.notify_panel()
        if not self._syncing:
            await self.async_pull_leader()
            await self.async_publish_followers()

    async def _apply_mode(self, host: str, *, restart_clock: bool) -> None:
        state = self.panel_state(host)
        if self._stopped:
            return
        if state["mode"] == MODE_EFFECT and state["effect"] != EFFECT_NONE:
            await self._run_effect(host, str(state["effect"]), restart_clock=restart_clock)
            return
        if state["mode"] == MODE_SCENE and state.get("scene"):
            await self._run_scene(host, str(state["scene"]))
            return
        if state["mode"] == MODE_OFF:
            await self._apply_all_off(host)
            return
        self._stop_target_effect(host)

    async def _run_effect(self, host: str, effect: str, *, restart_clock: bool) -> None:
        light = self._target_light(host)
        if light is None:
            return
        speed = self.effect_speed(light._owner_id)
        if restart_clock or host not in self._show_clocks:
            self._adopted_clocks.discard(host)
            self._show_clocks[host] = (self.show_clock(), float(speed))
            self._store_clock(host)
        level = percent_to_brightness(self.panel_state(host)["brightness"])
        self._applying = True
        try:
            if effect == EFFECT_SOLID and level == 0:
                await light.async_turn_off()
                return
            paint = level if level > 0 else 1
            await light.async_turn_on(effect=effect, brightness=paint)
            if level == 0:
                run = self.effects.runs.get(light._owner_id)
                if run is not None:
                    run.brightness = 0
                light._attr_brightness = 0
                light.publish()
        finally:
            self._applying = False

    async def _run_scene(self, host: str, name: str) -> bool:
        record = self.scene_record(host, name)
        light = self._target_light(host)
        if record is None or light is None:
            return False
        self.effects.stop(light._owner_id)
        light._attr_effect = EFFECT_SOLID
        members = list(light._members())
        keys = [member._key for member in members]
        assignments = {
            key: (rgb, pct)
            for key, rgb, pct in resolve_scene(record, keys, offset=self._scene_offset(host))
        }
        self.note_command(keys)
        for member in members:
            found = assignments.get(member._key)
            if found is None:
                continue
            rgb, pct = found
            member.apply_panel_color(rgb, percent_to_brightness(pct))
        light._attr_effect = EFFECT_SOLID
        light._attr_is_on = any(member.is_on for member in members)
        light.publish()
        self.notify_light_states()
        return True

    def _scene_offset(self, host: str) -> int:
        """Fixtures ahead of this supply in the combined show order."""
        row = self._show_row(host)
        if len(row) < 2:
            return 0
        count = 0
        for hub, member_host in row:
            if hub is self and member_host == host:
                return count
            count += len(hub.target_keys(member_host))
        return 0

    def _stop_target_effect(self, host: str) -> None:
        """Stop the panel target. The channel bytes stay as they are."""
        light = self._target_light(host)
        owner = light._owner_id if light is not None else self._panel_owner(host)
        self.effects.stop(owner)
        if light is None:
            return
        light._attr_effect = EFFECT_SOLID
        light.publish()

    def _update_panel(self, host: str, **changes: Any) -> None:
        state = self.panel_state(host)
        state.update(changes)
        self._panel[host] = normalize_panel(state)
        self._store_clock(host)

    def _store_clock(self, host: str) -> None:
        state = self.panel_state(host)
        clock = self._show_clocks.get(host)
        if clock is None:
            state["show_origin"] = None
            state["show_speed"] = None
        else:
            state["show_origin"] = float(clock[0])
            state["show_speed"] = float(clock[1])
        state["show_adopted"] = host in self._adopted_clocks
        self._panel[host] = state

    def _retarget_show_clock(self, host: str, new_speed: float) -> None:
        """Keep the show phase when the leader's speed slider moves."""
        now = self.show_clock()
        current = self._show_clocks.get(host)
        if current is None:
            self._show_clocks[host] = (now, float(new_speed))
        else:
            origin, speed = current
            if float(new_speed) != float(speed):
                origin = retarget_origin(now, origin, speed, float(new_speed))
            self._show_clocks[host] = (float(origin), float(new_speed))
        self._store_clock(host)

    async def _commit_panel(self, host: str) -> None:
        if self._stopped:
            return
        self._panel_touched.add(host)
        self._panel_applied.add(host)
        self.notify_panel()
        await self._save()
        if self._stopped or self._syncing:
            return
        await self.async_publish_host(host)

    async def async_select_effect(self, host: str, effect: str) -> None:
        """Run ``effect`` on the panel target. None leaves the current colors."""
        chosen = normalize_effect_option(effect)
        previous = self.panel_state(host)
        if chosen == EFFECT_NONE:
            self._stop_target_effect(host)
            self._update_panel(host, effect=EFFECT_NONE, scene=None, mode=MODE_IDLE)
            await self._commit_panel(host)
            return
        restart = previous.get("effect") != chosen or previous.get("mode") != MODE_EFFECT
        self._update_panel(host, effect=chosen, scene=None, mode=MODE_EFFECT)
        await self._run_effect(host, chosen, restart_clock=restart)
        await self._commit_panel(host)

    async def async_select_scene(self, host: str, name: str) -> None:
        """Paint a stored scene and stop whatever effect was running."""
        record_name = find_scene_name(self._scenes.get(host, {}), name)
        record = None if record_name is None else self._scenes[host][record_name]
        if record_name is None or record is None:
            return
        self._stop_target_effect(host)
        self._update_panel(
            host,
            effect=EFFECT_NONE,
            scene=record_name,
            mode=MODE_SCENE,
            brightness=clamp_percent(record.get("brightness"), fallback=100),
        )
        await self._run_scene(host, record_name)
        await self._commit_panel(host)

    async def async_select_target(self, host: str, option: str) -> None:
        """Move the running effect or scene onto another group."""
        previous_owner = self._panel_owner(host)
        target = self._target_id_from_option(option)
        self._update_panel(host, target=target)
        if previous_owner != self._panel_owner(host):
            self.effects.stop(previous_owner)
            previous = self._light_for_owner(previous_owner)
            if previous is not None:
                previous._attr_effect = EFFECT_SOLID
                previous.publish()
        await self._apply_mode(host, restart_clock=False)
        await self._commit_panel(host)

    async def async_set_brightness(self, host: str, percent: float) -> None:
        """Set the panel level and push it to the current target."""
        level = clamp_percent(percent)
        self._update_panel(host, brightness=level)
        await self._apply_brightness_level(host, level)
        await self._commit_panel(host)

    async def _apply_brightness_level(self, host: str, percent: int) -> None:
        light = self._target_light(host)
        if light is None:
            return
        level = percent_to_brightness(percent)
        state = self.panel_state(host)
        if state["mode"] == MODE_EFFECT:
            run = self.effects.runs.get(light._owner_id)
            if run is not None:
                run.brightness = level
            light._attr_brightness = level
            if level > 0:
                light._attr_is_on = True
            light._remember_members()
            light.publish()
            self.notify_light_states()
            return
        for member in light._members():
            if state["mode"] != MODE_SCENE and not member.is_on:
                continue
            member._attr_brightness = level
            member._attr_is_on = level > 0
            member._push()
            if member.hass is not None:
                member.async_write_ha_state()
        light._attr_brightness = level
        light.publish()
        self.notify_light_states()

    async def async_stop_effect(self, host: str) -> None:
        """Stop the animation and leave the colors where they are."""
        state = self.panel_state(host)
        self._stop_target_effect(host)
        if state["mode"] == MODE_EFFECT:
            self._update_panel(host, effect=EFFECT_NONE, mode=MODE_IDLE)
        else:
            self._update_panel(host, effect=EFFECT_NONE)
        await self._commit_panel(host)

    async def _apply_all_off(self, host: str) -> None:
        """Turn the target off. Callers publish the off mode themselves."""
        if self._stopped:
            return
        light = self._target_light(host)
        self.effects.stop(light._owner_id if light is not None else self._panel_owner(host))
        self._applying = True
        try:
            if light is not None and not self._stopped:
                await light.async_turn_off()
        finally:
            self._applying = False

    async def async_all_off(self, host: str) -> None:
        """Turn the panel target off. The fade is the supply's default fade."""
        self._update_panel(host, effect=EFFECT_NONE, scene=None, mode=MODE_OFF)
        await self._apply_all_off(host)
        await self._commit_panel(host)

    async def async_upsert_scene(
        self,
        host: str,
        name: str,
        scene: dict[str, Any],
        *,
        previous: str | None = None,
    ) -> str | None:
        """Store a scene. Returns an error key, or None when it was saved."""
        clean = str(name).strip()
        if not clean:
            return "invalid_scene"
        normalized = normalize_scene(scene)
        if normalized is None:
            return "invalid_scene"
        current = self._scenes.setdefault(host, {})
        prior = find_scene_name(current, previous) if previous else None
        conflict = find_scene_name(current, clean)
        if conflict is not None and conflict != prior:
            return "duplicate_scene"
        if prior is not None and prior != clean:
            current.pop(prior, None)
            if self.panel_state(host).get("scene") == prior:
                self._update_panel(host, scene=clean)
        current[clean] = normalized
        if host not in self._scene_hosts:
            self._scene_hosts.append(host)
        self._panel_touched.add(host)
        await self._save()
        self.notify_panel()
        return None

    async def async_delete_scene(self, host: str, name: str) -> None:
        """Remove a scene. Deleting the selected one leaves the colors up."""
        current = self._scenes.setdefault(host, {})
        stored = find_scene_name(current, name)
        if stored is None:
            return
        current.pop(stored, None)
        if self.panel_state(host).get("scene") == stored:
            self._update_panel(host, scene=None, effect=EFFECT_NONE, mode=MODE_IDLE)
        if host not in self._scene_hosts:
            self._scene_hosts.append(host)
        await self._save()
        self.notify_panel()

    def _follow_links(self) -> dict[str, str]:
        links: dict[str, str] = {}
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            links[entry.entry_id] = follow_of(entry)
        links.setdefault(self.entry.entry_id, follow_of(self.entry))
        return links

    def _hub_for(self, entry_id: str):
        if entry_id == self.entry.entry_id:
            return self
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return None
        hub = getattr(entry, "runtime_data", None)
        if hub is None or getattr(hub, "_stopped", False):
            return None
        return hub

    def _direct_follower_hubs(self) -> list[Any]:
        found = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if follow_of(entry) != self.entry.entry_id:
                continue
            hub = self._hub_for(entry.entry_id)
            if hub is not None and hub is not self:
                found.append(hub)
        found.sort(key=lambda hub: hub.entry.entry_id)
        return found

    def _show_row(self, host: str) -> list[tuple[Any, str]]:
        """Leader first, then followers, paired by supply index."""
        if host not in self.hosts:
            return []
        index = self.hosts.index(host)
        links = self._follow_links()
        root = root_of(links, self.entry.entry_id)
        row: list[tuple[Any, str]] = []
        for entry_id in chain_members(links, root):
            hub = self._hub_for(entry_id)
            if hub is None or index >= len(hub.hosts):
                continue
            row.append((hub, hub.hosts[index]))
        return row

    def _effect_syncs(self) -> dict[str, tuple[Any, float]]:
        """Shared phase and combined index for each panel effect in a show.

        A follower that copied the leader's clock keeps that phase when it is
        the only hub left running. A solo effect that never followed anyone
        still uses ``EffectRun.phase_at``.
        """
        syncs: dict[str, tuple[Any, float]] = {}
        for host in self.hosts:
            if self.panel_state(host).get("mode") != MODE_EFFECT:
                continue
            row = self._show_row(host)
            if len(row) >= 2:
                root_hub, root_host = row[0]
                clock = root_hub._show_clocks.get(root_host)
                if clock is None:
                    continue
                origin, speed = clock
                phase = show_phase(root_hub.show_clock(), speed, origin)
                local: dict[str, int] = {}
                cursor = 0
                for hub, member_host in row:
                    for key in hub.target_keys(member_host):
                        if hub is self:
                            local[key] = cursor
                        cursor += 1
                total = cursor
            elif host in self._adopted_clocks and host in self._show_clocks:
                origin, speed = self._show_clocks[host]
                phase = show_phase(self.show_clock(), speed, origin)
                local = {
                    key: index for index, key in enumerate(self.target_keys(host))
                }
                total = len(local)
            else:
                continue
            if total <= 0:
                continue

            def _placement(key: str, _local: dict[str, int] = local, _total: int = total):
                index = _local.get(key)
                if index is None:
                    return None
                return index, _total

            syncs[self._panel_owner(host)] = (_placement, phase)
        return syncs

    def _spawn(self, coro: Any, name: str) -> None:
        """Track a publish task so unload cancels it before it can save."""
        if self._stopped:
            coro.close()
            return
        self._task_serial += 1
        task = self.entry.async_create_background_task(
            self.hass, coro, f"{name} {self._task_serial}"
        )
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    def note_follow_option(self) -> None:
        """A follow-hub change was stored. Pull the leader without reloading."""
        if self._stopped:
            return
        self._spawn(self.async_pull_leader(), "color_kinetics follow")

    def _schedule_publish(self, host: str) -> None:
        if self._syncing or self._stopped or self._applying:
            return
        if not self._direct_follower_hubs():
            return
        self._spawn(self.async_publish_host(host), "color_kinetics publish")

    def _apply_speed_entity(self, owner: str, speed: float) -> None:
        slider = self.speed_entities.get(owner)
        if slider is not None:
            slider.apply_external_speed(speed)
            return
        self.set_effect_speed(owner, speed)

    async def async_pull_leader(self) -> None:
        """Copy the leader's panel when this entry follows one."""
        leader = self._hub_for(follow_of(self.entry))
        if leader is None:
            return
        for index, host in enumerate(leader.hosts):
            if index >= len(self.hosts):
                break
            await leader.async_publish_host(host)

    async def async_publish_followers(self) -> None:
        for host in self.hosts:
            await self.async_publish_host(host)

    async def async_publish_host(self, host: str, *, _seen: set[str] | None = None) -> None:
        """Push this supply's panel to entries that follow us, then onward."""
        if self._stopped or host not in self.hosts:
            return
        seen = set(_seen or ())
        if self.entry.entry_id in seen:
            return
        seen.add(self.entry.entry_id)
        index = self.hosts.index(host)
        for follower in self._direct_follower_hubs():
            if index >= len(follower.hosts):
                continue
            try:
                await follower.async_accept(self, host, follower.hosts[index])
                await follower.async_publish_host(follower.hosts[index], _seen=seen)
            except Exception:  # noqa: BLE001 - one follower must not stop the rest
                _LOGGER.exception("Could not mirror %s onto a following Data Enabler", host)

    async def async_accept(self, leader: Any, leader_host: str, host: str) -> None:
        """Mirror one supply of ``leader`` onto ``host``."""
        if self._stopped or host not in self.hosts or leader is None:
            return
        state = leader.panel_state(leader_host)
        if state.get("mode") == MODE_SCENE:
            mirrored = find_scene_name(self._scenes.get(host, {}), str(state.get("scene") or ""))
            if mirrored is None:
                state = dict(state)
                state["mode"] = MODE_IDLE
                state["scene"] = None
                state["effect"] = EFFECT_NONE
            else:
                state = dict(state)
                state["scene"] = mirrored
        self._sync_depth += 1
        try:
            if self._stopped:
                return
            self._panel[host] = normalize_panel(state)
            self._panel_touched.add(host)
            self._panel_applied.add(host)
            speed = leader.effect_speed(leader._panel_owner(leader_host))
            self._apply_speed_entity(self._panel_owner(host), speed)
            if leader_host in leader._show_clocks:
                self._show_clocks[host] = tuple(leader._show_clocks[leader_host])
                self._adopted_clocks.add(host)
                self._store_clock(host)
            elif state.get("mode") != MODE_EFFECT:
                self._adopted_clocks.discard(host)
            await self._apply_mode(host, restart_clock=False)
            if self._stopped:
                return
            if (
                state.get("mode") == MODE_SCENE
                and not self._scene_brightness_matches(host)
            ):
                await self._apply_brightness_level(host, int(self.panel_state(host)["brightness"]))
        finally:
            self._sync_depth = max(0, self._sync_depth - 1)
        if self._stopped:
            return
        self.notify_panel()
        await self._save()

    def _scene_brightness_matches(self, host: str) -> bool:
        state = self.panel_state(host)
        record = self.scene_record(host, str(state.get("scene") or ""))
        if record is None:
            return True
        return clamp_percent(record.get("brightness"), fallback=100) == int(state["brightness"])

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
