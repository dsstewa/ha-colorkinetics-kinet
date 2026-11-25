"""Control panel entities, scene storage, follow-hub sync, and restore."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from custom_components.color_kinetics.const import CONF_FOLLOW_ENTRY, DOMAIN
from custom_components.color_kinetics.effects import fixture_channels
from custom_components.color_kinetics.entry_data import options_signature
from custom_components.color_kinetics.hub import STORAGE_VERSION
from custom_components.color_kinetics.scenes import percent_to_brightness
from tests.fake_kinet import SUPPLY_HOST, SUPPLY_SERIAL
from tests.test_light import _entity_id, _entry, _settle, _setup

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "fake_udp")

SECOND_HOST = "172.16.10.42"
SECOND_SERIAL = "11223344"


def _second_entry(**options):
    supplies = {
        SECOND_HOST: {
            "ip": SECOND_HOST,
            "mac": "00:0a:c5:47:1f:80",
            "protocol_version": 2,
            "serial_hex": SECOND_SERIAL,
            "manufacturer": "Philips Color Kinetics",
            "model": "Data Enabler Pro",
            "firmware": "SFT-000155-00 V34",
            "label": "Data Enabler Pro",
        }
    }
    base = {
        "hosts": [SECOND_HOST],
        "supplies": supplies,
        "protocol": "portout",
        "portout_version": 1,
        "output_port": 1,
        "manual_fixtures": "",
        "default_fade": 0,
        CONF_FOLLOW_ENTRY: "",
    }
    base.update(options)
    return MockConfigEntry(
        domain=DOMAIN,
        data={"hosts": [SECOND_HOST], "supplies": supplies},
        options=base,
        unique_id=SECOND_SERIAL,
        title=f"Data Enabler Pro ({SECOND_HOST})",
    )


def _split_second_supply(monkeypatch, fake_udp):
    """Answer 172.16.10.42 as a different supply with two of its own fixtures.

    ``__call__`` is looked up on the class, so the instance assignment does
    not reach the socket. Patch the class for the length of the test.
    """
    original = type(fake_udp).__call__
    second_fixtures = [(0xAAAA0001, 0, 0x18), (0xAAAA0002, 3, 0x18)]

    def _call(self, data, addr):
        saved_fixtures = list(self.fixtures)
        saved_poll = self.poll_reply
        if isinstance(addr, tuple) and addr and addr[0] == SECOND_HOST:
            self.fixtures = list(second_fixtures)
            reply = bytearray(saved_poll)
            reply[24:28] = bytes.fromhex("44332211")
            self.poll_reply = bytes(reply)
        try:
            return original(self, data, addr)
        finally:
            self.fixtures = saved_fixtures
            self.poll_reply = saved_poll

    monkeypatch.setattr(type(fake_udp), "__call__", _call)


async def _ids(hass):
    return {
        "effect": _entity_id(hass, "select", f"{SUPPLY_SERIAL}:panel_effect"),
        "scene": _entity_id(hass, "select", f"{SUPPLY_SERIAL}:panel_scene"),
        "target": _entity_id(hass, "select", f"{SUPPLY_SERIAL}:panel_target"),
        "brightness": _entity_id(hass, "number", f"{SUPPLY_SERIAL}:brightness"),
        "stop": _entity_id(hass, "button", f"{SUPPLY_SERIAL}:stop_effect"),
        "off": _entity_id(hass, "button", f"{SUPPLY_SERIAL}:all_off"),
        "speed": _entity_id(hass, "number", f"{SUPPLY_SERIAL}:all_fixtures:effect_speed"),
        "group": _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures"),
    }


async def test_panel_selects_buttons_and_scenes(hass, fake_udp):
    entry = await _setup(
        hass,
        _entry(
            **{
                "groups": [
                    {
                        "id": "tree",
                        "name": "Tree Run",
                        "members": ["880055BE", "88002CD9"],
                    }
                ]
            }
        ),
    )
    ids = await _ids(hass)
    effect = hass.states.get(ids["effect"])
    assert effect.state == "None"
    assert effect.attributes["options"][0] == "None"
    assert "Halloween" in effect.attributes["options"]
    assert "Rainbow" in effect.attributes["options"]
    assert "Solid" in effect.attributes["options"]
    scene = hass.states.get(ids["scene"])
    for name in ("Halloween Purple", "Christmas Red Green", "All Blue", "Camp", "All Orange"):
        assert name in scene.attributes["options"]
    assert hass.states.get(ids["target"]).state == "All fixtures"
    assert "Tree Run" in hass.states.get(ids["target"]).attributes["options"]
    assert float(hass.states.get(ids["brightness"]).state) == 100
    assert ids["speed"]
    assert hass.states.get(ids["group"]).attributes["effect_list"]

    hub = entry.runtime_data
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["scene"], "option": "Halloween Purple"},
        blocking=True,
    )
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == bytes((238, 0, 255))
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) == bytes((238, 0, 255))
    assert not hub.effects.active
    assert hass.states.get(ids["scene"]).state == "Halloween Purple"
    assert hass.states.get(ids["effect"]).state == "None"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["scene"], "option": "Christmas Red Green"},
        blocking=True,
    )
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\xff\x00\x00"
    assert bytes(hub._buffers[SUPPLY_HOST][3:6]) == b"\x00\xff\x00"
    assert bytes(hub._buffers[SUPPLY_HOST][6:9]) == b"\xff\x00\x00"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["scene"], "option": "All Blue"},
        blocking=True,
    )
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\xff"
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["scene"], "option": "Camp"},
        blocking=True,
    )
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == bytes((255, 180, 80))
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["scene"], "option": "All Orange"},
        blocking=True,
    )
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == bytes((255, 80, 0))

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["target"], "option": "Tree Run"},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["effect"], "option": "Rainbow"},
        blocking=True,
    )
    assert hass.states.get(ids["effect"]).state == "Rainbow"
    assert hass.states.get(ids["scene"]).state == "unknown"
    owner = hub.group_owner("tree")
    assert hub.effects.runs[owner].effect == "Rainbow"
    assert hub.effects.runs[owner].order == ["880055BE", "88002CD9"]


async def test_stop_leaves_colors_and_all_off_uses_the_light_path(hass, fake_udp):
    entry = await _setup(hass, _entry())
    ids = await _ids(hass)
    hub = entry.runtime_data
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["effect"], "option": "Breathe"},
        blocking=True,
    )
    assert hub.effects.active
    task = hub._effect_task
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    hub._effect_task = None
    held = bytes(hub._buffers[SUPPLY_HOST][0:3])
    await hass.services.async_call(
        "button", "press", {"entity_id": ids["stop"]}, blocking=True
    )
    assert not hub.effects.active
    assert hass.states.get(ids["effect"]).state == "None"
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == held

    await hass.services.async_call(
        "button", "press", {"entity_id": ids["off"]}, blocking=True
    )
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == b"\x00\x00\x00"
    assert hass.states.get(ids["scene"]).state == "unknown"
    assert hass.states.get(_entity_id(hass, "light", "880055BE")).state == "off"


async def test_brightness_scales_the_target(hass, fake_udp):
    entry = await _setup(hass, _entry())
    ids = await _ids(hass)
    hub = entry.runtime_data
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["scene"], "option": "All Blue"},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": ids["brightness"], "value": 50},
        blocking=True,
    )
    level = percent_to_brightness(50)
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == bytes(
        ((0 * level + 127) // 255, (0 * level + 127) // 255, (255 * level + 127) // 255)
    )
    assert float(hass.states.get(ids["brightness"]).state) == 50


async def test_selection_is_restored_from_storage(hass, fake_udp):
    entry = await _setup(hass, _entry())
    ids = await _ids(hass)
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["effect"], "option": "Colorloop"},
        blocking=True,
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, entry)
    ids = await _ids(hass)
    hub = entry.runtime_data
    assert hass.states.get(ids["effect"]).state == "Colorloop"
    assert hub.effects.runs[hub.all_fixtures_owner(SUPPLY_HOST)].effect == "Colorloop"


async def test_restore_entity_seeds_a_panel_the_storage_does_not_have(hass, fake_udp):
    mock_restore_cache(
        hass,
        [State("select.data_enabler_pro_172_16_10_41_effect", "Rainbow")],
    )
    entry = await _setup(hass, _entry())
    ids = await _ids(hass)
    assert ids["effect"].endswith("effect")
    hub = entry.runtime_data
    assert hass.states.get(ids["effect"]).state == "Rainbow"
    assert hub.panel_state(SUPPLY_HOST)["mode"] == "effect"
    assert hub.effects.active


async def test_storage_migration_keeps_looks_and_adds_scenes(hass, fake_udp, hass_storage):
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, version=2, minor_version=1)
    key = f"{DOMAIN}_{entry.entry_id}"
    await Store(hass, 2, key).async_save(
        {
            "looks": {"Evening": {"fixtures": {}, "lights": {}}},
            "selected_look": "Evening",
            "channels": {"880055BE": "010203"},
            "fixtures": {},
            "supplies": {},
            "scene_hosts": [],
        }
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, entry)
    hub = entry.runtime_data
    assert hub.look_names() == ["Evening"]
    assert "Halloween Purple" in hub.scene_names(SUPPLY_HOST)
    stored = hass_storage[key]
    assert stored["version"] == STORAGE_VERSION
    assert stored["data"]["looks"]["Evening"]["fixtures"] == {}
    assert "scenes" in stored["data"]

    for name in list(hub.scene_names(SUPPLY_HOST)):
        await hub.async_delete_scene(SUPPLY_HOST, name)
    assert hub.scene_names(SUPPLY_HOST) == []
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, entry)
    assert entry.runtime_data.scene_names(SUPPLY_HOST) == []


async def test_options_flow_edits_a_scene_and_rejects_a_follow_cycle(hass, fake_udp):
    entry = await _setup(hass, _entry())
    other = _second_entry()
    other.add_to_hass(hass)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    scenes = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "scenes"}
    )
    edit = await hass.config_entries.options.async_configure(
        scenes["flow_id"], {"scene_action": "Halloween Purple"}
    )
    saved = await hass.config_entries.options.async_configure(
        edit["flow_id"],
        {
            "name": "Halloween Purple",
            "pattern": "solid",
            "color": "1, 2, 3",
            "color_2": "",
            "brightness": 100,
            "fixtures": "",
            "delete": False,
        },
    )
    assert saved["type"] is FlowResultType.FORM
    assert saved["step_id"] == "scenes"
    done = await hass.config_entries.options.async_configure(
        saved["flow_id"], {"scene_action": "done"}
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    ids = await _ids(hass)
    hub = entry.runtime_data
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["scene"], "option": "Halloween Purple"},
        blocking=True,
    )
    assert bytes(hub._buffers[SUPPLY_HOST][0:3]) == bytes((1, 2, 3))

    duplicate = await hub.async_upsert_scene(
        SUPPLY_HOST,
        "camp",
        {"pattern": "solid", "rgb": [1, 1, 1], "brightness": 10, "fixtures": {}},
    )
    assert duplicate == "duplicate_scene"
    assert await hub.async_upsert_scene(SUPPLY_HOST, "  ", {"pattern": "solid"}) == "invalid_scene"
    await hub.async_select_scene(SUPPLY_HOST, "missing")
    await hub.async_delete_scene(SUPPLY_HOST, "missing")

    menu = await hass.config_entries.options.async_init(entry.entry_id)
    output = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "output"}
    )
    linked = await hass.config_entries.options.async_configure(
        output["flow_id"],
        {
            "hosts": SUPPLY_HOST,
            "protocol": "portout",
            "portout_version": "1",
            "output_port": 1,
            "manual_fixtures": "",
            "default_fade": 0,
            CONF_FOLLOW_ENTRY: other.entry_id,
        },
    )
    assert linked["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_FOLLOW_ENTRY] == other.entry_id

    menu = await hass.config_entries.options.async_init(other.entry_id)
    output = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "output"}
    )
    rejected = await hass.config_entries.options.async_configure(
        output["flow_id"],
        {
            "hosts": SECOND_HOST,
            "protocol": "portout",
            "portout_version": "1",
            "output_port": 1,
            "manual_fixtures": "",
            "default_fade": 0,
            CONF_FOLLOW_ENTRY: entry.entry_id,
        },
    )
    assert rejected["errors"][CONF_FOLLOW_ENTRY] == "follow_cycle"


async def test_follow_hub_syncs_phase_and_clears_when_the_leader_is_removed(
    hass, fake_udp, monkeypatch
):
    _split_second_supply(monkeypatch, fake_udp)
    leader = await _setup(hass, _entry())
    follower = _second_entry()
    follower.add_to_hass(hass)
    assert await hass.config_entries.async_setup(follower.entry_id)
    await _settle(hass, follower)

    options = dict(follower.options)
    options[CONF_FOLLOW_ENTRY] = leader.entry_id
    hass.config_entries.async_update_entry(follower, options=options)
    await hass.async_block_till_done()
    assert follower.runtime_data is follower.runtime_data
    same = options_signature({"protocol": "portout", CONF_FOLLOW_ENTRY: "a"})
    assert same == options_signature({"protocol": "portout", CONF_FOLLOW_ENTRY: "b"})

    ids = await _ids(hass)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": ids["speed"], "value": 8},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": ids["brightness"], "value": 80},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["effect"], "option": "Chase color"},
        blocking=True,
    )
    await hass.async_block_till_done()

    leader_hub = leader.runtime_data
    follower_hub = follower.runtime_data
    assert follower_hub.panel_state(SECOND_HOST)["effect"] == "Chase color"
    assert follower_hub.panel_state(SECOND_HOST)["brightness"] == 80
    follower_speed = _entity_id(
        hass, "number", f"{SECOND_SERIAL}:all_fixtures:effect_speed"
    )
    assert float(hass.states.get(follower_speed).state) == 8
    follower_level = _entity_id(hass, "number", f"{SECOND_SERIAL}:brightness")
    assert float(hass.states.get(follower_level).state) == 80

    for hub in (leader_hub, follower_hub):
        task = hub._effect_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        hub._effect_task = None

    moment = 10_000.0
    leader_hub.show_clock = lambda: moment  # type: ignore[method-assign]
    follower_hub.show_clock = lambda: moment  # type: ignore[method-assign]
    leader_hub._show_clocks[SUPPLY_HOST] = (moment, 8.0)
    follower_hub._show_clocks[SECOND_HOST] = (moment, 8.0)
    leader_hub.render_effects(0.0)
    follower_hub.render_effects(0.0)

    leader_keys = leader_hub.target_keys(SUPPLY_HOST)
    follower_keys = follower_hub.target_keys(SECOND_HOST)
    total = len(leader_keys) + len(follower_keys)
    assert len(leader_keys) == 8
    assert follower_keys == ["AAAA0001", "AAAA0002"]
    phase = 0.0
    run = leader_hub.effects.runs[leader_hub.all_fixtures_owner(SUPPLY_HOST)]
    followed = follower_hub.effects.runs[follower_hub.all_fixtures_owner(SECOND_HOST)]
    assert bytes(leader_hub._buffers[SUPPLY_HOST][0:3]) == fixture_channels(
        run.effect,
        0,
        total,
        phase,
        run.rgb,
        run.brightness,
        run.intensity,
        run.palette,
        run.direction,
        run.custom,
    )
    assert bytes(follower_hub._buffers[SECOND_HOST][0:3]) == fixture_channels(
        followed.effect,
        len(leader_keys),
        total,
        phase,
        followed.rgb,
        followed.brightness,
        followed.intensity,
        followed.palette,
        followed.direction,
        followed.custom,
    )
    local = fixture_channels(
        followed.effect,
        0,
        len(follower_keys),
        phase,
        followed.rgb,
        followed.brightness,
        followed.intensity,
        followed.palette,
        followed.direction,
        followed.custom,
    )
    assert bytes(follower_hub._buffers[SECOND_HOST][0:3]) != local

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": ids["scene"], "option": "All Blue"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert bytes(follower_hub._buffers[SECOND_HOST][0:3]) == b"\x00\x00\xff"

    assert await hass.config_entries.async_remove(leader.entry_id)
    await hass.async_block_till_done()
    assert follower.options.get(CONF_FOLLOW_ENTRY) in ("", None)
