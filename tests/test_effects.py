"""Fixture order, groups, and effect frames."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE

from custom_components.color_kinetics.const import CONF_FIXTURE_ORDER, CONF_GROUPS, DOMAIN
from custom_components.color_kinetics.effects import (
    EFFECT_ALTERNATE,
    EFFECT_SOLID,
    EFFECT_BREATHE,
    EFFECT_COLOR_CHASE,
    EFFECT_COLOR_WHEEL,
    EFFECT_RAINBOW,
    EFFECT_THEATER,
    EffectEngine,
    cycle_phase,
    cycle_seconds,
    fixture_channels,
)
from custom_components.color_kinetics.order import apply_submitted_order, effective_order
from tests.fake_kinet import SUPPLY_HOST, SUPPLY_SERIAL
from tests.test_light import _entity_id, _entry, _light_packets, _setup, _settle, _wait_light

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "fake_udp")


def test_order_appends_new_fixtures_and_keeps_missing_slots():
    saved = ["8800565C", "GONE0001", "880055BE"]
    discovered = [("880055BE", 0), ("88002CD9", 3), ("8800565C", 21)]
    assert effective_order(saved, discovered) == [
        "8800565C",
        "GONE0001",
        "880055BE",
        "88002CD9",
    ]
    reordered = apply_submitted_order(
        saved, ["880055BE", "8800565C"], discovered
    )
    assert reordered[0] == "880055BE"
    assert "GONE0001" in reordered
    assert reordered[-1] == "88002CD9"


def test_effect_frames_are_deterministic():
    assert cycle_seconds(5) == pytest.approx(1.2)
    assert cycle_seconds(1) == pytest.approx(6.0)
    assert cycle_phase(0.0, 5) == 0.0
    assert cycle_phase(0.6, 5) == pytest.approx(0.5)
    assert fixture_channels(EFFECT_COLOR_WHEEL, 0, 4, 0.0, (0, 0, 255), 255) == b"\xff\x00\x00"
    assert fixture_channels(EFFECT_COLOR_WHEEL, 3, 4, 0.0, (0, 0, 255), 255) == b"\xff\x00\x00"
    assert fixture_channels(EFFECT_RAINBOW, 0, 2, 0.0, (0, 0, 0), 255) == b"\xff\x00\x00"
    assert fixture_channels(EFFECT_RAINBOW, 1, 2, 0.0, (0, 0, 0), 255) == b"\x00\xff\xff"
    assert fixture_channels(EFFECT_COLOR_CHASE, 0, 4, 0.0, (0, 0, 255), 255) == b"\x00\x00\xff"
    assert fixture_channels(EFFECT_COLOR_CHASE, 1, 4, 0.0, (0, 0, 255), 255) == b"\x00\x00\x00"
    assert fixture_channels(EFFECT_COLOR_CHASE, 2, 4, 0.5, (0, 0, 255), 255) == b"\x00\x00\xff"
    assert fixture_channels(EFFECT_THEATER, 0, 6, 0.0, (255, 0, 0), 255) == b"\xff\x00\x00"
    assert fixture_channels(EFFECT_THEATER, 1, 6, 0.0, (255, 0, 0), 255) == b"\x00\x00\x00"
    assert fixture_channels(EFFECT_THEATER, 1, 6, 1 / 3, (255, 0, 0), 255) == b"\xff\x00\x00"
    assert fixture_channels(EFFECT_ALTERNATE, 0, 4, 0.0, (255, 0, 0), 255) == b"\xff\x00\x00"
    assert fixture_channels(EFFECT_ALTERNATE, 1, 4, 0.0, (255, 0, 0), 255) == b"\x00\x00\x00"
    assert fixture_channels(EFFECT_ALTERNATE, 1, 4, 0.5, (255, 0, 0), 255) == b"\xff\x00\x00"
    breathe_dim = fixture_channels(EFFECT_BREATHE, 0, 1, 0.0, (255, 0, 0), 255)
    breathe_full = fixture_channels(EFFECT_BREATHE, 0, 1, 0.5, (255, 0, 0), 255)
    assert breathe_dim == b"\x33\x00\x00"
    assert breathe_full == b"\xff\x00\x00"
    assert fixture_channels("Solid", 0, 1, 0.0, (0, 255, 0), 255) == b"\x00\xff\x00"
    assert effective_order(["A", "A"], [("C", 2), ("B", 1)]) == ["A", "B", "C"]
    assert apply_submitted_order(["A", "B", "A"], ["C", "C"], [("D", 3)]) == [
        "A",
        "B",
        "C",
        "D",
    ]
    from custom_components.color_kinetics.order import merge_group_members

    assert merge_group_members(
        ["880055BE", "DEADBEEF", "88002CD9"],
        ["880055BE", "88002CD9"],
        {"880055BE", "88002CD9"},
    ) == ["880055BE", "DEADBEEF", "88002CD9"]
    assert merge_group_members(
        ["880055BE", "DEADBEEF", "88002CD9"],
        ["88002CD9", "880055BE"],
        {"880055BE", "88002CD9"},
    ) == ["88002CD9", "880055BE", "DEADBEEF"]
    assert merge_group_members(
        ["880055BE", "DEADBEEF", "88002CD9"], ["88002CD9"], {"880055BE", "88002CD9"}
    ) == ["DEADBEEF", "88002CD9"]
    assert apply_submitted_order(
        ["880055BE", "DEADBEEF", "88002CD9"], ["88002CD9", "880055BE"], []
    ) == ["88002CD9", "880055BE", "DEADBEEF"]
    engine = EffectEngine()
    engine.start("all", EFFECT_SOLID, ["A"], (255, 0, 0), 255, 5)
    assert engine.active is False
    engine.start("all", EFFECT_COLOR_WHEEL, ["A", "A"], (255, 0, 0), 255, 5)
    engine.start("all", EFFECT_COLOR_WHEEL, [], (255, 0, 0), 255, 5)
    assert engine.active is False
    engine.start("all", EFFECT_COLOR_WHEEL, ["A"], (255, 0, 0), 255, 5)
    engine.owners["B"] = "missing"
    assert engine.release_fixture("B") == "missing"
    frames = engine.frames(0.0, lambda _key: None, lambda _key: True)
    assert frames == {}


def test_latest_effect_owns_a_fixture_and_a_manual_color_releases_it():
    engine = EffectEngine()
    engine.start("all", EFFECT_COLOR_WHEEL, ["A", "B", "C"], (255, 0, 0), 255, 5)
    engine.start("group", EFFECT_COLOR_CHASE, ["B", "C"], (0, 0, 255), 255, 5)
    assert engine.owners["A"] == "all"
    assert engine.owners["B"] == "group"
    assert engine.runs["all"].order == ["A"]
    assert engine.release_fixture("B") == "group"
    assert "B" not in engine.owners
    assert engine.runs["group"].order == ["C"]
    engine.release_fixture("C")
    assert "group" not in engine.runs
    engine.stop("all")
    assert engine.active is False


async def test_effects_follow_order_and_a_manual_color_wins(hass, fake_udp):
    entry = await _setup(hass, _entry())
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_FIXTURE_ORDER: ["88002CD9", "880055BE"],
        },
    )
    await _settle(hass, entry)
    hub = entry.runtime_data
    assert hub.fixture_order()[0] == "88002CD9"
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    await hass.services.async_call(
        "light",
        "turn_on",
        {
            "entity_id": group,
            "effect": EFFECT_COLOR_WHEEL,
            "brightness": 255,
        },
        blocking=True,
    )
    hub.render_effects(0.0)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\xff\x00\x00"
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) == b"\xff\x00\x00"
    task = hub._effect_task
    assert task is not None and not task.done()

    one = _entity_id(hass, "light", "880055BE")
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

    before = len(_light_packets(fake_udp))
    hub.render_effects(0.0)
    hub.pump.flush()
    packet = await _wait_light(
        fake_udp, lambda item: len(_light_packets(fake_udp)) > before
    )
    assert packet.data[0:3] == b"\x00\x00\xff"
    assert packet.data[3:6] == b"\xff\x00\x00"

    speed = _entity_id(hass, "number", f"{SUPPLY_SERIAL}:all_fixtures:effect_speed")
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": speed, "value": 1},
        blocking=True,
    )
    assert hub.effect_speed(hub.all_fixtures_owner(SUPPLY_HOST)) == 1
    assert hass.states.get(group).state == STATE_ON
    speed_id = _entity_id(hass, "number", f"{SUPPLY_SERIAL}:all_fixtures:effect_speed")
    assert speed_id is not None and speed_id.endswith("_effect_speed")
    await asyncio.sleep(0.05)
    hub.effects.stop(hub.all_fixtures_owner(SUPPLY_HOST))
    await asyncio.sleep(0.05)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert task.done()


async def test_group_effect_takes_fixtures_and_skips_unavailable(hass, fake_udp, monkeypatch):
    monkeypatch.setattr(
        "custom_components.color_kinetics.protocol.PROP_TIMEOUT", 0.05
    )
    fake_udp.skip_props[0x88005B93] = {0x40}
    entry = await _setup(
        hass,
        _entry(
            **{
                CONF_GROUPS: [
                    {
                        "id": "oaks",
                        "name": "Oaks",
                        "members": ["880055BE", "88005B93", "88002CD9"],
                    }
                ]
            }
        ),
    )
    group = _entity_id(hass, "light", "group:oaks")
    assert hass.states.get(_entity_id(hass, "light", "88005B93")).state == STATE_UNAVAILABLE
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": EFFECT_COLOR_CHASE, "rgb_color": [0, 255, 0], "brightness": 255},
        blocking=True,
    )
    entry.runtime_data.render_effects(0.0)
    data = bytes(entry.runtime_data._buffers[SUPPLY_HOST])
    assert data[0:3] == b"\x00\xff\x00"
    assert data[3:6] == b"\x00\x00\x00"
    assert data[6:9] == b"\x00\x00\x00"
    # The chase frame is dark here, but the fixture belongs to the effect, so it is on.
    assert hass.states.get(_entity_id(hass, "light", "88002CD9")).state == STATE_ON
    from homeassistant.components.light import DATA_COMPONENT

    member = hass.data[DATA_COMPONENT].get_entity(_entity_id(hass, "light", "88002CD9"))
    before_push = bytes(entry.runtime_data._buffers[SUPPLY_HOST][3:6])
    member.apply_fixture_update()
    assert bytes(entry.runtime_data._buffers[SUPPLY_HOST][3:6]) == before_push
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "effect": EFFECT_SOLID, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    packet = await _wait_light(fake_udp, lambda item: item.data[0:3] == b"\xff\x00\x00")
    assert packet.data[0:3] == b"\xff\x00\x00"
    assert packet.data[3:6] == b"\xff\x00\x00"
    # 88005B93 never got an address or a saved color, so those slots stay off.
    assert packet.data[6:9] == b"\x00\x00\x00"
    assert len(packet.data) >= 24
    assert entry.runtime_data.effects.active is False


async def test_a_stolen_group_reports_off_and_turn_off_does_not_zero(hass, fake_udp, monkeypatch):
    entry = await _setup(
        hass,
        _entry(
            **{
                CONF_GROUPS: [
                    {"id": "oaks", "name": "Oaks", "members": ["880055BE", "88002CD9"]},
                    {"id": "pines", "name": "Pines", "members": ["880055BE", "88002CD9"]},
                ]
            }
        ),
    )
    oaks = _entity_id(hass, "light", "group:oaks")
    pines = _entity_id(hass, "light", "group:pines")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": oaks, "effect": EFFECT_COLOR_WHEEL},
        blocking=True,
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": pines, "effect": EFFECT_COLOR_CHASE, "rgb_color": [0, 0, 255]},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(oaks).state == STATE_OFF
    assert hass.states.get(oaks).attributes.get("effect") is None
    from homeassistant.components.light import DATA_COMPONENT as LIGHT_DATA

    assert hass.data[LIGHT_DATA].get_entity(oaks)._attr_effect == EFFECT_SOLID
    hub = entry.runtime_data
    assert hub.effects.owners["880055BE"] == "group:pines"
    before = bytes(hub._buffers[SUPPLY_HOST][0:6])
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": oaks}, blocking=True
    )
    assert bytes(hub._buffers[SUPPLY_HOST][0:6]) == before
    assert hub.effects.owners["880055BE"] == "group:pines"
    from homeassistant.components.light import DATA_COMPONENT

    group_entity = hass.data[DATA_COMPONENT].get_entity(oaks)
    group_entity._group_id = "missing"
    assert group_entity._candidate_keys() == []
    group_entity.publish()
    assert group_entity.name == "Group"
    await hass.services.async_call(
        "light",
        "turn_on",
        {
            "entity_id": _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures"),
            "effect": "Bogus",
        },
        blocking=True,
    )
    hub = entry.runtime_data
    hub.effects.start(
        hub.all_fixtures_owner(SUPPLY_HOST), "Color wheel", ["NOPE"], (255, 0, 0), 255, 5
    )
    hub.render_effects(0.0)
    from custom_components.color_kinetics.hub import ColorKineticsHub

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "groups": [
                "nope",
                {"name": "Nameless"},
                {"id": "empty", "name": "Empty", "members": []},
            ],
        },
    )
    await hass.async_block_till_done()
    hub = entry.runtime_data
    assert hub.light_groups() == []
    monkeypatch.setattr(ColorKineticsHub, "hosts", property(lambda _self: []))
    assert (DOMAIN, "group:x") in hub.group_device_info(
        {"id": "x", "name": "X", "members": []}
    )["identifiers"]
