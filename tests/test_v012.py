"""Holiday effects, saved looks, transitions, and the sunset blueprint."""

from __future__ import annotations

import asyncio
from pathlib import Path

import probatio
import pytest
from homeassistant.components.light import LightEntityFeature
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from custom_components.color_kinetics.const import DOMAIN, KEEPALIVE_SECONDS, MAX_FPS
from custom_components.color_kinetics.effects import (
    EFFECT_CANDY_CANE,
    EFFECT_CHRISTMAS,
    EFFECT_HALLOWEEN,
    EFFECT_RED_WHITE_BLUE,
    EFFECT_WARM_TWINKLE,
    EffectEngine,
    fixture_channels,
    twinkle_is_sparkle,
)
from custom_components.color_kinetics.fade import FadeBook, interpolate
from custom_components.color_kinetics.hub import STORAGE_VERSION, ColorKineticsHub, ColorKineticsStore
from custom_components.color_kinetics.looks import find_look_name, normalize_look_name
from custom_components.color_kinetics.services import select_hubs
from tests.fake_kinet import SUPPLY_HOST, SUPPLY_SERIAL
from tests.test_light import _entity_id, _entry, _setup, _settle

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "blueprints" / "automation" / "color_kinetics" / "sunset_to_bedtime.yaml"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "fake_udp")


def _dmx(order):
    def locate(key):
        return "host", order.index(key) * 3

    return locate


def test_holiday_effects_follow_order_phase_and_speed():
    """Each holiday effect is a pure function of order, phase, and brightness."""
    assert fixture_channels(EFFECT_CHRISTMAS, 0, 4, 0.0, (1, 2, 3), 255) == b"\xff\x00\x00"
    assert fixture_channels(EFFECT_CHRISTMAS, 1, 4, 0.0, (1, 2, 3), 255) == b"\x00\xff\x00"
    assert fixture_channels(EFFECT_CHRISTMAS, 0, 4, 0.5, (1, 2, 3), 255) == b"\x00\xff\x00"
    assert fixture_channels(EFFECT_CHRISTMAS, 1, 4, 0.5, (1, 2, 3), 255) == b"\xff\x00\x00"
    assert fixture_channels(EFFECT_CHRISTMAS, 0, 4, 0.0, (1, 2, 3), 128) == b"\x80\x00\x00"

    assert fixture_channels(EFFECT_HALLOWEEN, 0, 4, 0.0, (9, 9, 9), 255) == b"\xff\x50\x00"
    assert fixture_channels(EFFECT_HALLOWEEN, 1, 4, 0.0, (9, 9, 9), 255) == b"\x80\x00\xff"
    assert fixture_channels(EFFECT_HALLOWEEN, 0, 4, 0.5, (9, 9, 9), 255) == b"\x80\x00\xff"

    cane = [fixture_channels(EFFECT_CANDY_CANE, index, 4, 0.0, (0, 0, 0), 255) for index in range(4)]
    assert cane == [b"\xff\x00\x00", b"\xff\x00\x00", b"\xff\xff\xff", b"\xff\xff\xff"]
    moved = [fixture_channels(EFFECT_CANDY_CANE, index, 4, 0.25, (0, 0, 0), 255) for index in range(4)]
    assert moved == [b"\xff\xff\xff", b"\xff\x00\x00", b"\xff\x00\x00", b"\xff\xff\xff"]
    assert moved != cane

    flag = [fixture_channels(EFFECT_RED_WHITE_BLUE, index, 3, 0.0, (0, 0, 0), 255) for index in range(3)]
    assert flag == [b"\xff\x00\x00", b"\xff\xff\xff", b"\x00\x00\xff"]
    stepped = [
        fixture_channels(EFFECT_RED_WHITE_BLUE, index, 3, 1 / 3, (0, 0, 0), 255) for index in range(3)
    ]
    assert stepped == [b"\x00\x00\xff", b"\xff\x00\x00", b"\xff\xff\xff"]

    assert twinkle_is_sparkle(0, 0.0) is True
    assert twinkle_is_sparkle(1, 0.0) is False
    assert twinkle_is_sparkle(0, 0.125) is False
    sparkle = fixture_channels(EFFECT_WARM_TWINKLE, 0, 4, 0.0, (1, 2, 3), 255)
    dim = fixture_channels(EFFECT_WARM_TWINKLE, 1, 4, 0.0, (1, 2, 3), 255)
    assert sparkle == b"\xff\xb4\x50"
    assert dim != sparkle
    assert dim != b"\x00\x00\x00"
    assert twinkle_is_sparkle(1, 0.375) is True
    assert fixture_channels(EFFECT_WARM_TWINKLE, 1, 4, 0.375, (1, 2, 3), 255) == sparkle

    order = ["A", "B", "C", "D"]
    engine = EffectEngine()
    engine.start("group", EFFECT_CHRISTMAS, order, (0, 0, 0), 255, 1)
    slow = engine.frames(0.3, _dmx(order), lambda _key: True)
    engine.runs["group"].speed = 10
    fast = engine.frames(0.3, _dmx(order), lambda _key: True)
    assert slow != fast
    engine.runs["group"].speed = 1
    half = engine.frames(3.0, _dmx(order), lambda _key: True)
    engine.runs["group"].speed = 10
    assert engine.frames(0.3, _dmx(order), lambda _key: True) == half

    reversed_order = list(reversed(order))
    engine.start("group", EFFECT_CHRISTMAS, reversed_order, (0, 0, 0), 255, 1)
    flipped = engine.frames(0.0, _dmx(reversed_order), lambda _key: True)
    forward = EffectEngine()
    forward.start("group", EFFECT_CHRISTMAS, order, (0, 0, 0), 255, 1)
    original = forward.frames(0.0, _dmx(order), lambda _key: True)
    original_by_start = {start: payload for start, payload in original["host"]}
    flipped_by_start = {start: payload for start, payload in flipped["host"]}
    # A is index 0 (red) until the order is reversed, when it is index 3 (green).
    assert original_by_start[0] == b"\xff\x00\x00"
    assert flipped_by_start[9] == b"\x00\xff\x00"

    def skip_b(key):
        return key != "B"

    squeezed = forward.frames(0.0, _dmx(order), skip_b)
    assert [start for start, _payload in squeezed["host"]] == [0, 6, 9]
    assert squeezed["host"][0][1] == b"\xff\x00\x00"
    assert squeezed["host"][1][1] == b"\x00\xff\x00"


def test_fade_interpolation_and_cancellation():
    assert interpolate(b"\xff\x00\x00", b"\x00\xff\x00", 0) == b"\xff\x00\x00"
    assert interpolate(b"\xff\x00\x00", b"\x00\xff\x00", 1) == b"\x00\xff\x00"
    assert interpolate(b"\xff\x00\x00", b"\x00\xff\x00", 0.5) == b"\x80\x80\x00"
    assert interpolate(b"\xff\x00\x00", b"\x00\xff\x00", 2) == b"\x00\xff\x00"
    assert interpolate(b"\xff\x00\x00", b"\x00\xff\x00", -1) == b"\xff\x00\x00"

    book = FadeBook()
    assert book.start("a", b"\x00\x00\x00", b"\x00\x00\x00", 0, 1) is False
    assert book.start("a", b"\x00\x00\x00", b"\xff\x00\x00", 0, 0) is False
    assert book.active is False
    assert book.start("a", b"\x00\x00\x00", b"\xff\x00\x00", 10, 2) is True
    assert book.start("b", b"\x00\x00\x00", b"\x00\x00\xff", 10, 2) is True
    assert book.frames(10)["a"] == b"\x00\x00\x00"
    assert book.frames(11)["a"] == interpolate(b"\x00\x00\x00", b"\xff\x00\x00", 0.5)
    book.cancel(["a"])
    assert book.contains("a") is False
    assert book.contains("b") is True
    assert "a" not in book.frames(11)
    finished = book.frames(12)
    assert finished["b"] == b"\x00\x00\xff"
    assert book.active is False
    assert book.started_at("b") is None
    book.start("c", b"\x00\x00\x00", b"\x01\x00\x00", 0, 1)
    book.clear()
    assert book.active is False


def test_look_names_match_without_case():
    assert normalize_look_name("  Evening  ") == "Evening"
    looks = {"Evening": {}}
    assert find_look_name(looks, " evening ") == "Evening"
    assert find_look_name(looks, "   ") is None
    assert find_look_name(looks, "Morning") is None


def test_select_hubs_requires_a_device_when_two_entries_are_loaded():
    class _Hub:
        def __init__(self, device_id):
            self.supply_device_ids = {"host": device_id}

    first = _Hub("device-a")
    second = _Hub("device-b")
    assert select_hubs([first], None) == [first]
    with pytest.raises(ServiceValidationError) as err:
        select_hubs([first, second], None)
    assert err.value.translation_key == "look_target"
    assert select_hubs([first, second], "device-b") == [second]
    assert select_hubs([first, second], ["device-a"]) == [first]
    with pytest.raises(ServiceValidationError):
        select_hubs([first], "missing")
    with pytest.raises(ServiceValidationError):
        select_hubs([], None)


async def test_store_migration_adds_looks_and_keeps_channels(hass, hass_storage):
    key = "color_kinetics_look-migration"
    await Store(hass, 1, key).async_save(
        {"channels": {"880055BE": "00ff00"}, "fixtures": {}, "supplies": {}}
    )
    store = ColorKineticsStore(hass, STORAGE_VERSION, key)
    loaded = await store.async_load()
    assert loaded["looks"] == {}
    assert loaded["channels"]["880055BE"] == "00ff00"
    assert hass_storage[key]["version"] == STORAGE_VERSION
    assert await store._async_migrate_func(1, 1, None) == {"looks": {}}
    assert (await store._async_migrate_func(1, 1, {"looks": []}))["looks"] == {}
    kept = await store._async_migrate_func(2, 1, {"looks": {"Porch": {}}, "channels": {}})
    assert kept["looks"] == {"Porch": {}}


async def test_christmas_follows_fixture_order_and_a_manual_color_wins(hass):
    entry = await _setup(hass, _entry())
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    state = hass.states.get(group)
    assert "Christmas" in state.attributes["effect_list"]
    assert "Warm twinkle" in state.attributes["effect_list"]
    assert state.attributes["supported_features"] & LightEntityFeature.TRANSITION
    assert state.attributes["supported_features"] & LightEntityFeature.EFFECT
    one = _entity_id(hass, "light", "880055BE")
    assert hass.states.get(one).attributes["supported_features"] & LightEntityFeature.TRANSITION

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": EFFECT_CHRISTMAS, "brightness": 255},
        blocking=True,
    )
    hub = entry.runtime_data
    hub.render_effects(0.0)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\xff\x00\x00"
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) == b"\x00\xff\x00"
    assert "880055BE" in hub.effects.owners

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [0, 0, 255], "brightness": 255},
        blocking=True,
    )
    hub.render_effects(0.0)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\xff"
    assert "880055BE" not in hub.effects.owners
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) == b"\xff\x00\x00"
    assert hass.states.async_entity_ids("automation") == []


async def test_looks_round_trip_across_reload_and_missing_fixtures(hass, hass_storage):
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
    select_id = _entity_id(hass, "select", f"{SUPPLY_SERIAL}:look")
    assert hass.states.get(select_id).attributes["options"] == []
    one = _entity_id(hass, "light", "880055BE")
    other = _entity_id(hass, "light", "88002CD9")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [255, 0, 0], "brightness": 128},
        blocking=True,
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": other, "rgb_color": [0, 255, 0], "brightness": 255},
        blocking=True,
    )
    speed = _entity_id(hass, "number", f"{SUPPLY_SERIAL}:all_fixtures:effect_speed")
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": speed, "value": 2}, blocking=True
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": EFFECT_CHRISTMAS, "brightness": 255},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "save_look", {"name": "  Evening  "}, blocking=True
    )
    hub = entry.runtime_data
    assert hub.look_names() == ["Evening"]
    assert set(hub._looks["Evening"]["fixtures"]) >= {"880055BE", "88002CD9"}
    assert "Renamed oak" not in hub._looks["Evening"]["fixtures"]
    owner = hub.all_fixtures_owner(SUPPLY_HOST)
    assert hub._looks["Evening"]["lights"][owner]["effect"] == EFFECT_CHRISTMAS
    assert hub._looks["Evening"]["lights"][owner]["speed"] == 2
    assert hass.states.get(select_id).state == "Evening"

    with pytest.raises(ServiceValidationError) as duplicate:
        await hass.services.async_call(
            DOMAIN, "save_look", {"name": "evening"}, blocking=True
        )
    assert duplicate.value.translation_key == "duplicate_look"
    with pytest.raises(probatio.Invalid):
        await hass.services.async_call(DOMAIN, "save_look", {"name": "   "}, blocking=True)

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "880055BE"), entry.entry_id
    )
    dr.async_get(hass).async_update_device(device.id, name_by_user="Renamed oak")

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": "Solid", "rgb_color": [0, 0, 255], "brightness": 255},
        blocking=True,
    )
    assert hass.states.get(one).attributes["rgb_color"] == (0, 0, 255)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    await _settle(hass, entry)
    hub = entry.runtime_data
    assert "Evening" in hub.look_names()
    key = f"color_kinetics_{entry.entry_id}"
    assert hass_storage[key]["version"] == STORAGE_VERSION
    assert "Evening" in hass_storage[key]["data"]["looks"]
    assert hass_storage[key]["data"]["selected_look"] == "Evening"
    assert hass.states.get(_entity_id(hass, "select", f"{SUPPLY_SERIAL}:look")).state == "Evening"

    hub._looks["Evening"]["fixtures"].pop("88002CD9")
    hub._looks["Evening"]["fixtures"]["FFFFFFFF"] = {
        "is_on": True,
        "brightness": 255,
        "rgb": [1, 2, 3],
    }
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(hass, "light", "88002CD9"), "rgb_color": [0, 0, 255]},
        blocking=True,
    )
    before_missing = bytes(hub._buffers[SUPPLY_HOST][3:6])
    await hass.services.async_call(
        DOMAIN, "recall_look", {"name": "EVENING"}, blocking=True
    )
    restored = hass.states.get(_entity_id(hass, "light", "880055BE"))
    assert restored.state == STATE_ON
    assert restored.attributes["rgb_color"] == (255, 0, 0)
    assert restored.attributes["brightness"] == 255
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) == before_missing
    hub.render_effects(0.0)
    assert hub.effects.owners.get("880055BE") == hub.all_fixtures_owner(SUPPLY_HOST)
    assert hub.effect_speed(hub.all_fixtures_owner(SUPPLY_HOST)) == 2
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\xff\x00\x00"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": _entity_id(hass, "select", f"{SUPPLY_SERIAL}:look"), "option": "Evening"},
        blocking=True,
    )
    assert hass.states.get(_entity_id(hass, "select", f"{SUPPLY_SERIAL}:look")).state == "Evening"

    with pytest.raises(ServiceValidationError) as missing:
        await hass.services.async_call(
            DOMAIN, "recall_look", {"name": "No such look"}, blocking=True
        )
    assert missing.value.translation_key == "unknown_look"
    await hass.services.async_call(
        DOMAIN, "delete_look", {"name": "evening"}, blocking=True
    )
    assert hub.look_names() == []
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "delete_look", {"name": "Evening"}, blocking=True
        )

    await entry.runtime_data._save()
    assert "looks" in hass_storage[key]["data"]
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
    assert not hass.services.has_service(DOMAIN, "save_look")


async def _hold_frames(monkeypatch):
    """Stop the animation loop on its first sleep so a test can step the clock."""
    gate = asyncio.Event()

    async def hold(self, delay):
        self._held_delays = getattr(self, "_held_delays", [])
        self._held_delays.append(delay)
        await gate.wait()

    monkeypatch.setattr(ColorKineticsHub, "_sleep", hold)
    return gate


async def test_transitions_interpolate_and_a_new_command_cancels_one_fixture(hass, monkeypatch):
    await _hold_frames(monkeypatch)
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    assert hub.pump._min_interval == pytest.approx(1 / MAX_FPS)
    assert hub.pump._keepalive == pytest.approx(KEEPALIVE_SECONDS)
    assert hub._frame_delay() == pytest.approx(1 / MAX_FPS)

    one = _entity_id(hass, "light", "880055BE")
    other = _entity_id(hass, "light", "88002CD9")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": other, "rgb_color": [0, 0, 255], "brightness": 255},
        blocking=True,
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {
            "entity_id": [one, other],
            "rgb_color": [0, 255, 0],
            "brightness": 255,
            "transition": 4,
        },
        blocking=True,
    )
    assert hub.fades.contains("880055BE")
    assert hub.fades.contains("88002CD9")
    for _ in range(20):
        if getattr(hub, "_held_delays", None):
            break
        await asyncio.sleep(0)
    started, duration = hub.fades.started_at("880055BE")
    assert duration == pytest.approx(4)
    hub.render_fades(started)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\xff\x00\x00"
    hub.render_fades(started + 2)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x80\x80\x00"
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) == interpolate(b"\x00\x00\xff", b"\x00\xff\x00", 0.5)

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [0, 0, 255], "brightness": 255},
        blocking=True,
    )
    assert hub.fades.contains("880055BE") is False
    assert hub.fades.contains("88002CD9") is True
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\xff"
    other_started, other_duration = hub.fades.started_at("88002CD9")
    hub.render_fades(other_started + other_duration / 2)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\xff"
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) != b"\x00\x00\xff"
    hub.render_fades(other_started + other_duration)
    assert hub.fades.active is False
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) == b"\x00\xff\x00"
    assert len(hub._buffers[SUPPLY_HOST]) >= 24 or hub._outgoing_length(SUPPLY_HOST) >= 24


async def test_all_fixtures_fades_and_a_taken_fixture_is_not_reclaimed(hass, monkeypatch):
    """A group transition fades every member. Recall will not steal one back."""
    await _hold_frames(monkeypatch)
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    assert "Halloween" in hass.states.get(group).attributes["effect_list"]
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
        {
            "entity_id": group,
            "rgb_color": [0, 0, 255],
            "brightness": 255,
            "transition": 2,
        },
        blocking=True,
    )
    started, duration = hub.fades.started_at("880055BE")
    hub.render_fades(started + duration / 2)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == interpolate(
        b"\xff\x00\x00", b"\x00\x00\xff", 0.5
    )
    await hass.services.async_call(
        "light",
        "turn_off",
        {"entity_id": group, "transition": 2},
        blocking=True,
    )
    assert hub.fades.contains("880055BE")
    off_started, off_duration = hub.fades.started_at("880055BE")
    hub.render_fades(off_started + off_duration)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\x00"

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": EFFECT_HALLOWEEN, "brightness": 255},
        blocking=True,
    )
    await hass.services.async_call(DOMAIN, "save_look", {"name": "Spooky"}, blocking=True)
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [0, 255, 0], "brightness": 255},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "recall_look", {"name": "Spooky", "transition": 5}, blocking=True
    )
    assert hub._deferred
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [255, 255, 0], "brightness": 255},
        blocking=True,
    )
    assert hub.fades.contains("880055BE") is False
    hub.render_frame(min(item.deadline for item in hub._deferred))
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\xff\xff\x00"
    assert hub.effects.owners.get("880055BE") != hub.all_fixtures_owner(SUPPLY_HOST)
    assert hub.all_fixtures_owner(SUPPLY_HOST) in hub.effects.owners.values()


async def test_transition_frame_rate_is_capped_and_recall_fades(hass, monkeypatch):
    delays: list[float] = []

    async def record(self, delay):
        delays.append(delay)
        if len(delays) >= 3:
            self.fades.clear()
            self._deferred.clear()
        await asyncio.sleep(0)

    monkeypatch.setattr(ColorKineticsHub, "_sleep", record)
    entry = await _setup(hass, _entry())
    one = _entity_id(hass, "light", "880055BE")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [255, 0, 0], "brightness": 255, "transition": 10},
        blocking=True,
    )
    for _ in range(10):
        if len(delays) >= 3:
            break
        await asyncio.sleep(0)
    assert delays
    assert all(delay == pytest.approx(1 / MAX_FPS) for delay in delays)
    assert entry.runtime_data.pump._keepalive == pytest.approx(KEEPALIVE_SECONDS)

    gate = asyncio.Event()

    async def hold(self, delay):
        await gate.wait()

    monkeypatch.setattr(ColorKineticsHub, "_sleep", hold)
    await hass.services.async_call(
        DOMAIN, "save_look", {"name": "Red"}, blocking=True
    )
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": EFFECT_CHRISTMAS},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "save_look", {"name": "Holiday"}, blocking=True
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [0, 0, 255], "brightness": 255},
        blocking=True,
    )
    hub = entry.runtime_data
    # The loop task was started under the previous sleep. Replace it so the
    # deferred effect is stepped by the test instead of the live clock.
    task = hub._effect_task
    hub._effect_task = None
    if task is not None:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task
    await hass.services.async_call(
        DOMAIN, "recall_look", {"name": "Holiday", "transition": 2}, blocking=True
    )
    assert hub.fades.contains("880055BE")
    assert "880055BE" not in hub.effects.owners
    assert hub._deferred
    deadline = min(item.deadline for item in hub._deferred)
    hub.render_frame(deadline - 0.1)
    assert "880055BE" not in hub.effects.owners
    hub.render_frame(deadline)
    assert hub.effects.owners.get("880055BE") == hub.all_fixtures_owner(SUPPLY_HOST)
    assert hub._deferred == []

    await hass.services.async_call(
        "light",
        "turn_off",
        {"entity_id": one, "transition": 3},
        blocking=True,
    )
    assert hub.fades.contains("880055BE")
    assert hass.states.get(one).state == STATE_OFF
    started, _duration = hub.fades.started_at("880055BE")
    hub.render_fades(started + 3)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\x00"


def test_strings_cover_the_new_ui_text():
    import json

    strings = json.loads((ROOT / "custom_components/color_kinetics/strings.json").read_text())
    english = json.loads(
        (ROOT / "custom_components/color_kinetics/translations/en.json").read_text()
    )
    assert strings == english
    assert strings["entity"]["select"]["look"]["name"] == "Look"
    for service in ("save_look", "recall_look", "delete_look"):
        described = strings["services"][service]
        assert described["name"]
        assert described["description"]
        assert described["fields"]["name"]["name"]
        assert described["fields"]["device_id"]["description"]
    assert strings["services"]["recall_look"]["fields"]["transition"]["description"]
    for key in ("duplicate_look", "unknown_look", "look_name", "look_target"):
        assert strings["exceptions"][key]["message"]


async def test_sunset_blueprint_validates_and_is_not_installed(hass):
    from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
    from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
    from homeassistant.util.yaml.loader import load_yaml_dict

    data = load_yaml_dict(BLUEPRINT)
    blueprint = Blueprint(
        data,
        expected_domain="automation",
        path="color_kinetics/sunset_to_bedtime.yaml",
        schema=BLUEPRINT_SCHEMA,
    )
    assert blueprint.metadata["domain"] == "automation"
    assert "sunset_offset" in blueprint.inputs
    assert "look" in blueprint.inputs
    assert "light" in blueprint.inputs
    assert "bedtime" in blueprint.inputs
    assert "transition" in blueprint.inputs
    substituted = BlueprintInputs(
        blueprint,
        {
            "use_blueprint": {
                "path": "color_kinetics/sunset_to_bedtime.yaml",
                "input": {
                    "light": "light.porch",
                    "bedtime": "22:30:00",
                    "look": "Evening",
                    "supply": ["device-enabler"],
                    "off_lights": {"entity_id": ["light.all_fixtures", "light.oaks"]},
                },
            }
        },
    ).async_substitute()
    from homeassistant.components.homeassistant.triggers import time as time_trigger
    from homeassistant.components.sun.trigger import SunsetTrigger
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.script import async_validate_actions_config

    triggers = cv.TRIGGER_SCHEMA(substituted["triggers"])
    platforms = {trigger["platform"] for trigger in triggers}
    assert "sun.sunset" in platforms
    assert "time" in platforms
    for trigger in triggers:
        if trigger["platform"] == "sun.sunset":
            await SunsetTrigger.async_validate_complete_config(hass, trigger)
        else:
            time_trigger.TRIGGER_SCHEMA(trigger)
    await async_validate_actions_config(hass, substituted["actions"])
    sunset = substituted["actions"][0]["choose"][0]["sequence"][0]["then"][0]
    assert sunset["action"] == "color_kinetics.recall_look"
    assert sunset["data"]["device_id"] == ["device-enabler"]
    bedtime = substituted["actions"][0]["choose"][1]["sequence"]
    assert bedtime[0]["target"]["entity_id"] == ["light.all_fixtures", "light.oaks"]
    assert bedtime[1]["target"]["entity_id"] == "light.porch"
    assert "supply" in blueprint.inputs
    assert "off_lights" in blueprint.inputs
    blank = BlueprintInputs(
        blueprint,
        {
            "use_blueprint": {
                "path": "color_kinetics/sunset_to_bedtime.yaml",
                "input": {
                    "light": "light.all_fixtures",
                    "bedtime": "21:00:00",
                    "look": "",
                    "off_lights": {"entity_id": "light.all_fixtures"},
                },
            }
        },
    ).async_substitute()
    await async_validate_actions_config(hass, blank["actions"])
    assert blank["actions"][0]["choose"][0]["sequence"][0]["then"][0]["data"]["device_id"] == []
    assert hass.states.async_entity_ids("automation") == []


async def test_christmas_saved_from_off_does_not_fade_to_black(hass, monkeypatch):
    """An effect started from off is on, so recall fades toward that light, not black."""
    await _hold_frames(monkeypatch)
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    one = _entity_id(hass, "light", "880055BE")
    assert hass.states.get(one).state == STATE_OFF
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": EFFECT_CHRISTMAS, "brightness": 128},
        blocking=True,
    )
    state = hass.states.get(one)
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 128
    await hass.services.async_call(DOMAIN, "save_look", {"name": "Tree"}, blocking=True)
    saved = hub._looks["Tree"]["fixtures"]["880055BE"]
    assert saved["is_on"] is True
    assert saved["brightness"] == 128
    owner = hub.all_fixtures_owner(SUPPLY_HOST)
    assert hub._looks["Tree"]["lights"][owner]["effect"] == EFFECT_CHRISTMAS
    await hass.services.async_call("light", "turn_off", {"entity_id": group}, blocking=True)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\x00"
    await hass.services.async_call(
        DOMAIN,
        "recall_look",
        {"name": "Tree", "transition": 4},
        blocking=True,
    )
    assert hub.fades.contains("880055BE")
    started, duration = hub.fades.started_at("880055BE")
    hub.render_fades(started + duration)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) != b"\x00\x00\x00"
    assert hub._deferred
    hub.render_frame(min(item.deadline for item in hub._deferred))
    assert hub.effects.owners.get("880055BE") == owner
    hub.render_effects(0.0)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x80\x00\x00"


async def test_animated_turn_on_fades_into_the_first_frame(hass, monkeypatch):
    await _hold_frames(monkeypatch)
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    one = _entity_id(hass, "light", "880055BE")
    await hass.services.async_call(
        "light",
        "turn_on",
        {
            "entity_id": group,
            "effect": EFFECT_CHRISTMAS,
            "brightness": 255,
            "transition": 4,
        },
        blocking=True,
    )
    assert hass.states.get(one).state == STATE_ON
    assert hass.states.get(one).attributes["brightness"] == 255
    assert "880055BE" not in hub.effects.owners
    assert hub.fades.contains("880055BE")
    started, duration = hub.fades.started_at("880055BE")
    hub.render_fades(started + duration / 2)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x80\x00\x00"
    hub.render_fades(started + duration)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\xff\x00\x00"
    hub.render_frame(min(item.deadline for item in hub._deferred))
    assert hub.effects.owners.get("880055BE") == hub.all_fixtures_owner(SUPPLY_HOST)


async def test_fixture_device_id_selects_its_supply(hass):
    from custom_components.color_kinetics.services import expand_device_ids

    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    registry = dr.async_get(hass)
    fixture = registry.async_get_device_by_identifier((DOMAIN, "880055BE"), entry.entry_id)
    supply = registry.async_get_device_by_identifier((DOMAIN, SUPPLY_SERIAL), entry.entry_id)
    assert fixture.via_device_id == supply.id
    expanded = expand_device_ids(hass, [hub], {fixture.id})
    assert supply.id in expanded
    await hass.services.async_call(
        DOMAIN,
        "save_look",
        {"name": "Via fixture", "device_id": fixture.id},
        blocking=True,
    )
    assert "Via fixture" in hub.look_names()
    await hass.services.async_call(
        DOMAIN,
        "recall_look",
        {"name": "Via fixture", "device_id": fixture.id},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        "delete_look",
        {"name": "Via fixture", "device_id": []},
        blocking=True,
    )
    assert hub.look_names() == []
