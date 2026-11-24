"""Default fade, gamma curve, and color temperature."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.components.light import ColorMode
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory

from custom_components.color_kinetics.color_temp import clamp_kelvin, kelvin_to_rgb
from custom_components.color_kinetics.const import CONF_GROUPS, DOMAIN
from custom_components.color_kinetics.effects import EFFECT_CHASE
from custom_components.color_kinetics.fade import interpolate
from custom_components.color_kinetics.hub import ColorKineticsHub
from custom_components.color_kinetics.light import scale_rgb
from tests.fake_kinet import SUPPLY_HOST, SUPPLY_SERIAL
from tests.test_light import _entity_id, _entry, _setup

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "fake_udp")

KELVIN_SAMPLES = (2000, 2700, 4000, 6500)


async def _hold_frames(monkeypatch):
    """Stop the animation loop on its first sleep so a test can step the clock."""
    gate = asyncio.Event()

    async def hold(self, delay):
        await gate.wait()

    monkeypatch.setattr(ColorKineticsHub, "_sleep", hold)
    return gate


async def test_kelvin_ramps_from_warm_to_cool_and_clamps():
    colors = [kelvin_to_rgb(kelvin) for kelvin in KELVIN_SAMPLES]
    assert colors == [(255, 137, 14), (255, 167, 87), (255, 206, 166), (255, 254, 250)]
    blues = [color[2] for color in colors]
    greens = [color[1] for color in colors]
    assert blues == sorted(blues)
    assert len(set(blues)) == len(blues)
    assert greens == sorted(greens)
    for color in colors:
        assert max(color) == 255
        assert color[0] >= color[2]
    assert colors[0][2] < colors[-1][2]
    assert clamp_kelvin(100) == 2000
    assert clamp_kelvin(9000) == 6500
    assert clamp_kelvin("nope") == 2000
    assert kelvin_to_rgb(1500)[2] == 0
    assert max(kelvin_to_rgb(8000)) == 255
    assert kelvin_to_rgb(8000)[2] == 255


async def test_default_fade_when_transition_is_omitted(hass, monkeypatch):
    await _hold_frames(monkeypatch)
    entry = await _setup(hass, _entry(omit_default_fade=True))
    hub = entry.runtime_data
    fade = _entity_id(hass, "number", f"{SUPPLY_SERIAL}:default_fade")
    state = hass.states.get(fade)
    assert float(state.state) == 1.5
    assert state.attributes["min"] == 0
    assert state.attributes["max"] == 10
    assert state.attributes["step"] == 0.1
    assert state.attributes["unit_of_measurement"] == "s"
    registered = er.async_get(hass).async_get(fade)
    assert registered.entity_category == EntityCategory.CONFIG
    assert registered.original_icon == "mdi:timer-outline"

    one = _entity_id(hass, "light", "880055BE")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    assert hub.fades.contains("880055BE")
    _started, duration = hub.fades.started_at("880055BE")
    assert duration == pytest.approx(1.5)
    assert hass.states.get(one).state == STATE_ON


async def test_explicit_transition_overrides_the_default_and_zero_snaps(hass, monkeypatch):
    await _hold_frames(monkeypatch)
    entry = await _setup(hass, _entry(omit_default_fade=True))
    hub = entry.runtime_data
    one = _entity_id(hass, "light", "880055BE")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [255, 0, 0], "brightness": 255, "transition": 4},
        blocking=True,
    )
    assert hub.fades.started_at("880055BE")[1] == pytest.approx(4)

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [0, 0, 255], "brightness": 255, "transition": 0},
        blocking=True,
    )
    assert hub.fades.contains("880055BE") is False
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\xff"

    from homeassistant.components.light import DATA_COMPONENT

    light = hass.data[DATA_COMPONENT].get_entity(one)
    await light.async_turn_on(rgb_color=(0, 255, 0), brightness=128, transition="bad")
    assert hub.fades.contains("880055BE") is False
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == scale_rgb((0, 255, 0), 128)


async def test_turn_off_fades_to_zero_and_then_reports_off(hass, monkeypatch):
    await _hold_frames(monkeypatch)
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    one = _entity_id(hass, "light", "880055BE")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    await hass.services.async_call(
        "light",
        "turn_off",
        {"entity_id": one, "transition": 2},
        blocking=True,
    )
    assert hass.states.get(one).state == STATE_ON
    assert hub.fades.contains("880055BE")
    started, duration = hub.fades.started_at("880055BE")
    hub.render_fades(started + duration / 2)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) not in (b"\xff\x00\x00", b"\x00\x00\x00")
    assert hass.states.get(one).state == STATE_ON
    hub.render_fades(started + duration)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\x00"
    assert hass.states.get(one).state == STATE_OFF
    assert hub.fades.active is False


async def test_mid_fade_retarget_continues_from_the_current_level(hass, monkeypatch):
    await _hold_frames(monkeypatch)
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    one = _entity_id(hass, "light", "880055BE")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [0, 255, 0], "brightness": 255, "transition": 4},
        blocking=True,
    )
    started, duration = hub.fades.started_at("880055BE")
    hub.render_fades(started + duration / 2)
    midpoint = bytes(hub._buffers[SUPPLY_HOST][0:3])
    assert midpoint == interpolate(b"\xff\x00\x00", b"\x00\xff\x00", 0.5)

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [0, 0, 255], "brightness": 255, "transition": 4},
        blocking=True,
    )
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == midpoint
    restarted, _again = hub.fades.started_at("880055BE")
    hub.render_fades(restarted)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == midpoint
    hub.render_fades(restarted + 4)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\xff"


async def test_a_group_fades_every_member_from_the_same_instant(hass, monkeypatch):
    await _hold_frames(monkeypatch)
    entry = await _setup(
        hass,
        _entry(
            **{
                CONF_GROUPS: [
                    {"id": "oaks", "name": "Oaks", "members": ["880055BE", "88002CD9"]},
                ]
            }
        ),
    )
    hub = entry.runtime_data
    group = _entity_id(hass, "light", "group:oaks")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "rgb_color": [255, 0, 0], "brightness": 255, "transition": 0},
        blocking=True,
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "rgb_color": [0, 0, 255], "brightness": 255, "transition": 2},
        blocking=True,
    )
    first = hub.fades.started_at("880055BE")
    second = hub.fades.started_at("88002CD9")
    assert first == second
    assert first[1] == pytest.approx(2)
    started, duration = first
    hub.render_fades(started + duration / 2)
    expected = interpolate(b"\xff\x00\x00", b"\x00\x00\xff", 0.5)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == expected
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) == expected
    calls: list[str] = []
    hub.pump.mark_dirty = lambda host: calls.append(host)
    hub.render_fades(started + duration)
    assert calls == [SUPPLY_HOST]
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\xff"
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) == b"\x00\x00\xff"


async def test_default_fade_survives_a_reload(hass, monkeypatch):
    await _hold_frames(monkeypatch)
    entry = await _setup(hass, _entry())
    fade = _entity_id(hass, "number", f"{SUPPLY_SERIAL}:default_fade")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": fade, "value": 2.5}, blocking=True
    )
    assert entry.options["default_fade"] == 2.5
    await hass.services.async_call(
        "number", "set_value", {"entity_id": fade, "value": 2.5}, blocking=True
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    hub = entry.runtime_data
    assert hub.default_fade == pytest.approx(2.5)
    assert float(hass.states.get(fade).state) == pytest.approx(2.5)

    one = _entity_id(hass, "light", "880055BE")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    assert hub.fades.started_at("880055BE")[1] == pytest.approx(2.5)


async def test_an_effect_skips_the_default_fade(hass, monkeypatch):
    await _hold_frames(monkeypatch)
    entry = await _setup(hass, _entry(omit_default_fade=True))
    hub = entry.runtime_data
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": "Christmas", "brightness": 255},
        blocking=True,
    )
    assert hub.fades.active is False
    assert "880055BE" in hub.effects.owners
    hub.render_effects(0.0)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\xff\x00\x00"


async def test_color_mode_follows_the_last_command_and_restores(hass):
    entry = await _setup(hass, _entry())
    one = _entity_id(hass, "light", "880055BE")
    state = hass.states.get(one)
    assert set(state.attributes["supported_color_modes"]) == {"color_temp", "rgb"}
    assert state.attributes["min_color_temp_kelvin"] == 2000
    assert state.attributes["max_color_temp_kelvin"] == 6500

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [0, 0, 255], "brightness": 255},
        blocking=True,
    )
    state = hass.states.get(one)
    assert state.attributes["color_mode"] == ColorMode.RGB
    assert state.attributes["color_temp_kelvin"] is None
    assert state.attributes["rgb_color"] == (0, 0, 255)

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "hs_color": [120, 100], "brightness": 255},
        blocking=True,
    )
    assert hass.states.get(one).attributes["color_mode"] == ColorMode.RGB

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "xy_color": [0.3, 0.3], "brightness": 200},
        blocking=True,
    )
    assert hass.states.get(one).attributes["color_mode"] == ColorMode.RGB

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "color_temp_kelvin": 2700, "brightness": 255},
        blocking=True,
    )
    state = hass.states.get(one)
    assert state.attributes["color_mode"] == ColorMode.COLOR_TEMP
    assert state.attributes["color_temp_kelvin"] == 2700
    assert entry.runtime_data._buffers[SUPPLY_HOST][0:3] == bytes(kelvin_to_rgb(2700))

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    restored = hass.states.get(one)
    assert restored.state == STATE_ON
    assert restored.attributes["color_mode"] == ColorMode.COLOR_TEMP
    assert restored.attributes["color_temp_kelvin"] == 2700
    assert entry.runtime_data._buffers[SUPPLY_HOST][0:3] == bytes(kelvin_to_rgb(2700))


async def test_direct_kelvin_outside_the_slider_is_clamped(hass):
    await _setup(hass, _entry())
    from homeassistant.components.light import DATA_COMPONENT

    one = _entity_id(hass, "light", "880055BE")
    light = hass.data[DATA_COMPONENT].get_entity(one)
    await light.async_turn_on(color_temp_kelvin=10000, brightness=255)
    assert light.color_temp_kelvin == 6500
    await light.async_turn_on(color_temp_kelvin=100, brightness=255)
    assert light.color_temp_kelvin == 2000
    assert light.color_mode == ColorMode.COLOR_TEMP


async def test_a_group_passes_color_temperature_to_every_member(hass):
    entry = await _setup(
        hass,
        _entry(
            **{
                CONF_GROUPS: [
                    {"id": "oaks", "name": "Oaks", "members": ["880055BE", "88002CD9"]},
                ]
            }
        ),
    )
    group = _entity_id(hass, "light", "group:oaks")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "color_temp_kelvin": 4000, "brightness": 255},
        blocking=True,
    )
    expected = bytes(kelvin_to_rgb(4000))
    hub = entry.runtime_data
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == expected
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) == expected
    for entity_id in (group, _entity_id(hass, "light", "880055BE"), _entity_id(hass, "light", "88002CD9")):
        state = hass.states.get(entity_id)
        assert state.attributes["color_mode"] == ColorMode.COLOR_TEMP
        assert state.attributes["color_temp_kelvin"] == 4000


async def test_two_kelvins_fade_through_the_derived_rgb(hass, monkeypatch):
    await _hold_frames(monkeypatch)
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    one = _entity_id(hass, "light", "880055BE")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "color_temp_kelvin": 2000, "brightness": 255, "transition": 0},
        blocking=True,
    )
    warm = bytes(kelvin_to_rgb(2000))
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == warm
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "color_temp_kelvin": 6500, "brightness": 255, "transition": 2},
        blocking=True,
    )
    assert hass.states.get(one).attributes["color_mode"] == ColorMode.COLOR_TEMP
    assert hass.states.get(one).attributes["color_temp_kelvin"] == 6500
    cool = bytes(kelvin_to_rgb(6500))
    started, duration = hub.fades.started_at("880055BE")
    hub.render_fades(started + duration / 2)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == interpolate(warm, cool, 0.5)
    hub.render_fades(started + duration)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == cool


async def test_an_effect_uses_the_kelvin_color(hass):
    entry = await _setup(hass, _entry())
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "color_temp_kelvin": 2700, "brightness": 255},
        blocking=True,
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": EFFECT_CHASE, "brightness": 255},
        blocking=True,
    )
    entry.runtime_data.render_effects(0.0)
    assert bytes(entry.runtime_data._buffers[SUPPLY_HOST][0:3]) == bytes(kelvin_to_rgb(2700))
