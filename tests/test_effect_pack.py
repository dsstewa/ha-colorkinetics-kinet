"""Clean-room effect pack: frames, controls, aliases, and the idle loop."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er

from custom_components.color_kinetics.const import MAX_FPS
from custom_components.color_kinetics.effects import (
    DIRECTION_REVERSE,
    EFFECT_ALIASES,
    EFFECT_CANDY_CANE,
    EFFECT_CHASE,
    EFFECT_COLOR_CHASE,
    EFFECT_COLOR_WHEEL,
    EFFECT_GRADIENT,
    EFFECT_NAMES,
    EFFECT_RAINBOW,
    EFFECT_RAINBOW_ALL,
    EFFECT_RAINBOW_CYCLE,
    EFFECT_RED_WHITE_BLUE,
    EFFECT_SOLID,
    EFFECT_STATIC,
    EFFECT_WARM_TWINKLE,
    INTENSITY_NOTES,
    PALETTE_CUSTOM,
    PALETTE_FIRE,
    PALETTE_JULY,
    PALETTE_NAMES,
    PALETTE_OCEAN,
    PALETTE_WARM,
    EffectEngine,
    clip_intensity,
    cycle_seconds,
    fixture_channels,
    legacy_palette,
    normalize_direction,
    normalize_palette,
    resolve_effect,
)
from tests.fake_kinet import SUPPLY_HOST, SUPPLY_SERIAL
from tests.test_light import _entity_id, _entry, _setup

ROOT = Path(__file__).resolve().parents[1]
RGB = (255, 0, 0)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "fake_udp")

# Frozen 8-pixel frames at phase 0.25, default intensity, Custom, Forward.
# Regenerated from fixture_channels; the assertions below also check the relationships.
_PHASE = 0.25


def _row(effect: str, phase: float = _PHASE, **kwargs) -> tuple[bytes, ...]:
    return tuple(
        fixture_channels(effect, index, 8, phase, RGB, 255, **kwargs) for index in range(8)
    )


def test_every_effect_frame_is_deterministic_and_intensity_is_bounded():
    """The same inputs always draw the same eight pixels, and 0 and 99 clip."""
    assert set(INTENSITY_NOTES) == set(EFFECT_NAMES)
    assert len(EFFECT_NAMES) == 29
    assert clip_intensity(0) == 1
    assert clip_intensity(0.4) == 1
    assert clip_intensity(10.4) == 10
    assert clip_intensity(99) == 10
    assert clip_intensity("nope") == 5
    assert normalize_palette("ocean") == PALETTE_OCEAN
    assert normalize_palette("nope") == PALETTE_CUSTOM
    assert normalize_direction("backward") == DIRECTION_REVERSE
    assert normalize_direction(None) == "Forward"
    for effect in EFFECT_NAMES:
        forward = _row(effect)
        again = _row(effect)
        assert forward == again
        assert all(len(pixel) == 3 for pixel in forward)
        assert any(pixel != b"\x00\x00\x00" for pixel in forward)
        assert _row(effect, intensity=1) == _row(effect, intensity=0)
        assert _row(effect, intensity=10) == _row(effect, intensity=99)
        if effect == EFFECT_SOLID:
            assert _row(effect, intensity=1) == _row(effect, intensity=10)
        else:
            # Twinkle fade is at the bottom of its fade at phase 0.25, so check another.
            changed = any(
                _row(effect, phase, intensity=1) != _row(effect, phase, intensity=10)
                for phase in (0.0625, 0.25, 0.5, 0.8, 0.81)
            )
            assert changed, effect
        if effect in {EFFECT_SOLID, EFFECT_GRADIENT}:
            assert _row(effect, 0.0) == _row(effect, 0.5) == _row(effect, 0.81)
        else:
            moved = any(
                _row(effect, 0.0) != _row(effect, phase) for phase in (0.0625, 0.1, 0.25, 0.5, 0.81)
            )
            assert moved, effect
        assert _row(effect, direction=DIRECTION_REVERSE) == _row(effect, direction="reverse")
        if effect != EFFECT_SOLID:
            # A three-color palette makes a reversed cycle land on a different stop.
            changed = any(
                _row(effect, phase, direction=DIRECTION_REVERSE, palette=PALETTE_FIRE)
                != _row(effect, phase, palette=PALETTE_FIRE)
                for phase in (0.1, 0.25, 0.5, 0.8)
            )
            assert changed, effect


def test_palettes_recolor_effects_that_read_them_and_holiday_colors_stay():
    """Fire and Ocean disagree on a palette effect. Christmas stays red and green."""
    fire = _row("Fade", palette=PALETTE_FIRE)
    ocean = _row("Fade", palette=PALETTE_OCEAN)
    assert fire != ocean
    assert _row("Palette flow", palette=PALETTE_JULY)[0] != _row("Palette flow", palette=PALETTE_OCEAN)[0]
    christmas = _row("Christmas", 0.0)
    assert christmas[0] == b"\xff\x00\x00"
    assert christmas[1] == b"\x00\xff\x00"
    assert _row("Christmas", 0.0, palette=PALETTE_OCEAN)[0] == b"\xff\x00\x00"
    assert _row("Rainbow", 0.0)[0] == b"\xff\x00\x00"
    assert _row("Rainbow", 0.0)[3] == b"\xff\x00\x00"
    cycle = _row("Rainbow cycle", 0.0)
    assert cycle[0] == b"\xff\x00\x00"
    assert cycle[4] == b"\x00\xff\xff"
    assert fixture_channels("Rainbow cycle", 1, 2, 0.0, RGB, 255) == b"\x00\xff\xff"
    chase = _row("Chase color", 0.0)
    assert chase[0] == RGB[0].to_bytes(1, "big") + b"\x00\x00"
    assert chase[1] == b"\x00\x00\x00"
    assert _row("Chase color", 0.5)[4] == b"\xff\x00\x00"
    assert _row("Chase color", 0.0, intensity=10)[7] != b"\x00\x00\x00"
    breathe = fixture_channels("Breathe", 0, 1, 0.0, RGB, 255)
    assert breathe == b"\x33\x00\x00"
    assert fixture_channels("Breathe", 0, 1, 0.5, RGB, 255) == b"\xff\x00\x00"
    assert fixture_channels("Solid", 0, 1, 0.2, (0, 255, 0), 255) == b"\x00\xff\x00"


def test_old_names_resolve_and_default_frames_still_match():
    """A saved scene that sends a 0.1.x name still draws that effect's old frame."""
    assert resolve_effect(EFFECT_STATIC) == EFFECT_SOLID
    assert resolve_effect(EFFECT_COLOR_WHEEL) == EFFECT_RAINBOW_ALL
    assert resolve_effect(EFFECT_RAINBOW) == EFFECT_RAINBOW_CYCLE
    assert resolve_effect(EFFECT_COLOR_CHASE) == EFFECT_CHASE
    assert resolve_effect(EFFECT_WARM_TWINKLE) == "Twinkle"
    assert resolve_effect(EFFECT_CANDY_CANE) == "Christmas chase"
    assert resolve_effect(EFFECT_RED_WHITE_BLUE) == "Palette flow"
    assert resolve_effect("Bogus") == "Bogus"
    assert legacy_palette(EFFECT_WARM_TWINKLE) == PALETTE_WARM
    assert legacy_palette(EFFECT_RED_WHITE_BLUE) == PALETTE_JULY
    assert legacy_palette(EFFECT_CANDY_CANE) is None
    for old, new in EFFECT_ALIASES.items():
        if old in {EFFECT_WARM_TWINKLE, EFFECT_CANDY_CANE, EFFECT_RED_WHITE_BLUE, EFFECT_STATIC}:
            continue
        assert _row(old, 0.0) == _row(new, 0.0)
        assert _row(old, 0.5) == _row(new, 0.5)
    cane = _row(EFFECT_CANDY_CANE, 0.0)
    assert cane[:4] == (b"\xff\x00\x00", b"\xff\x00\x00", b"\xff\xff\xff", b"\xff\xff\xff")
    flag = [fixture_channels(EFFECT_RED_WHITE_BLUE, index, 3, 0.0, RGB, 255) for index in range(3)]
    assert flag == [b"\xff\x00\x00", b"\xff\xff\xff", b"\x00\x00\xff"]
    sparkle = fixture_channels(EFFECT_WARM_TWINKLE, 0, 4, 0.0, RGB, 255)
    assert sparkle == b"\xff\xb4\x50"
    assert "Color wheel" not in EFFECT_NAMES
    assert "Warm twinkle" not in EFFECT_NAMES
    readme = (ROOT / "README.md").read_text()
    for name in EFFECT_NAMES:
        assert name in readme
    assert "type: tile" in readme
    assert "Effect intensity" in readme


def test_restarting_the_same_effect_keeps_phase_and_skips_unavailable():
    """Intensity and a repeated start do not jump the cycle, and a dark fixture is omitted."""
    engine = EffectEngine()
    order = ["A", "B", "C", "D"]
    engine.start("all", EFFECT_COLOR_WHEEL, order, RGB, 255, 1)
    assert engine.runs["all"].effect == EFFECT_RAINBOW_ALL
    origin = engine.runs["all"].phase_origin
    engine.runs["all"].intensity = 9
    engine.runs["all"].palette = PALETTE_FIRE
    engine.runs["all"].direction = DIRECTION_REVERSE
    assert engine.runs["all"].phase_origin == origin
    engine.start("all", "Rainbow", order, RGB, 255, 10, 2, PALETTE_OCEAN, DIRECTION_REVERSE)
    assert engine.runs["all"].phase_origin == origin
    assert engine.runs["all"].phase_speed == 1
    assert engine.runs["all"].palette == PALETTE_OCEAN

    def locate(key):
        return "host", order.index(key) * 3

    full = engine.frames(cycle_seconds(1) / 4, locate, lambda _key: True)
    skipped = engine.frames(cycle_seconds(1) / 4, locate, lambda key: key != "B")
    assert [start for start, _payload in full["host"]] == [0, 3, 6, 9]
    assert [start for start, _payload in skipped["host"]] == [0, 6, 9]
    assert engine.frames(0.0, lambda _key: None, lambda _key: True) == {}
    engine.start("all", "Not an effect", order, RGB, 255, 5)
    assert engine.active is False


async def test_controls_persist_in_the_entities_and_a_color_stops_the_effect(hass, fake_udp):
    """Speed, intensity, palette, and direction are configuration entities on the supply."""
    del fake_udp
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    owner = hub.all_fixtures_owner(SUPPLY_HOST)
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    state = hass.states.get(group)
    assert state.attributes["effect_list"] == list(EFFECT_NAMES)
    registry = er.async_get(hass)
    expected = {
        "effect_speed": ("number", "mdi:speedometer"),
        "effect_intensity": ("number", "mdi:brightness-7"),
        "effect_palette": ("select", "mdi:palette-swatch"),
        "effect_direction": ("select", "mdi:arrow-left-right"),
    }
    for suffix, (domain, icon) in expected.items():
        entity_id = _entity_id(hass, domain, f"{owner}:{suffix}")
        assert entity_id is not None
        assert entity_id.endswith(f"_{suffix}")
        registered = registry.async_get(entity_id)
        assert registered.entity_category == EntityCategory.CONFIG
        assert registered.original_icon == icon
        assert hass.states.get(entity_id).attributes["icon"] == icon

    palette = _entity_id(hass, "select", f"{owner}:effect_palette")
    direction = _entity_id(hass, "select", f"{owner}:effect_direction")
    intensity = _entity_id(hass, "number", f"{owner}:effect_intensity")
    assert hass.states.get(palette).state == PALETTE_CUSTOM
    assert hass.states.get(direction).state == "Forward"
    assert float(hass.states.get(intensity).state) == 5
    assert hass.states.get(palette).attributes["options"] == list(PALETTE_NAMES)

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": EFFECT_COLOR_WHEEL, "brightness": 255},
        blocking=True,
    )
    assert hass.states.get(group).attributes["effect"] == EFFECT_RAINBOW_ALL
    hub.render_effects(0.0)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\xff\x00\x00"
    origin = hub.effects.runs[owner].phase_origin
    await hass.services.async_call(
        "select", "select_option", {"entity_id": palette, "option": PALETTE_FIRE}, blocking=True
    )
    await hass.services.async_call(
        "select", "select_option", {"entity_id": direction, "option": "Reverse"}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": intensity, "value": 9}, blocking=True
    )
    assert hub.effect_palette(owner) == PALETTE_FIRE
    assert hub.effect_direction(owner) == DIRECTION_REVERSE
    assert hub.effect_intensity(owner) == 9
    assert hub.effects.runs[owner].phase_origin == origin
    assert hub.effects.runs[owner].palette == PALETTE_FIRE
    assert hub.effects.runs[owner].direction == DIRECTION_REVERSE
    assert hub.effects.runs[owner].intensity == 9

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "rgb_color": [0, 0, 255], "brightness": 255},
        blocking=True,
    )
    assert owner not in hub.effects.runs
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\xff"
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": group}, blocking=True
    )
    assert hub.effects.active is False


async def test_unavailable_fixture_is_not_zeroed_by_a_new_effect(hass):
    """A fixture that drops out keeps the color already in the frame."""
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    one = _entity_id(hass, "light", "880055BE")
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\xff\x00\x00"
    fixture = hub.get_fixture("880055BE")
    hub._fixtures["880055BE"] = replace(fixture, present=False)
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": "Meteor", "brightness": 255},
        blocking=True,
    )
    hub.render_effects(0.0)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\xff\x00\x00"
    assert "880055BE" not in hub.effects.owners


async def test_effect_loop_sleeps_instead_of_spinning(hass, monkeypatch):
    """A running effect waits out the frame time, then the loop goes idle."""
    delays: list[float] = []

    async def record(self, delay):
        delays.append(delay)
        if len(delays) >= 3:
            self.effects.stop(self.all_fixtures_owner(SUPPLY_HOST))
        await asyncio.sleep(0)

    monkeypatch.setattr(
        "custom_components.color_kinetics.hub.ColorKineticsHub._sleep", record
    )
    entry = await _setup(hass, _entry())
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": "Twinkle"},
        blocking=True,
    )
    for _ in range(20):
        if len(delays) >= 3:
            break
        await asyncio.sleep(0)
    assert delays
    assert all(delay == pytest.approx(1 / MAX_FPS) for delay in delays)
    assert min(delays) > 0
    hub = entry.runtime_data
    await asyncio.sleep(0)
    assert hub.effects.active is False


async def test_alias_selects_a_palette_and_a_group_gets_the_controls(hass):
    """Red white blue becomes Palette flow on July 4th. A group has its own sliders."""
    entry = await _setup(
        hass,
        _entry(
            **{
                "groups": [
                    {"id": "oaks", "name": "Oaks", "members": ["880055BE", "88002CD9"]},
                ]
            }
        ),
    )
    hub = entry.runtime_data
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": EFFECT_RED_WHITE_BLUE, "brightness": 255},
        blocking=True,
    )
    owner = hub.all_fixtures_owner(SUPPLY_HOST)
    assert hub.effects.runs[owner].effect == "Palette flow"
    assert hub.effect_palette(owner) == PALETTE_JULY
    assert hass.states.get(_entity_id(hass, "select", f"{owner}:effect_palette")).state == PALETTE_JULY
    oaks = _entity_id(hass, "number", "group:oaks:effect_intensity")
    assert hass.states.get(oaks).name.endswith("effect intensity")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": oaks, "value": 3}, blocking=True
    )
    assert hub.effect_intensity("group:oaks") == 3
