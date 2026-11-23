"""Discovery, lights, rescan, restore, rate limit, and unload."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.color_kinetics.const import DOMAIN
from custom_components.color_kinetics.hub import ColorKineticsHub
from custom_components.color_kinetics.light import scale_rgb
from custom_components.color_kinetics.protocol import (
    ALLOWED_OUTGOING_TYPES,
    TYPE_DMXOUT,
    TYPE_PORTOUT,
    packet_type_of,
    parse_light_data,
)
from tests.fake_kinet import FIXTURES, SUPPLY_HOST, SUPPLY_SERIAL

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "fake_udp")


def _entry(**options):
    supplies = {
        SUPPLY_HOST: {
            "ip": SUPPLY_HOST,
            "mac": "00:0a:c5:47:1f:7f",
            "protocol_version": 2,
            "serial_hex": SUPPLY_SERIAL,
            "manufacturer": "Philips Color Kinetics",
            "model": "Data Enabler Pro",
            "firmware": "SFT-000155-00 V34",
            "label": "Data Enabler Pro",
        }
    }
    base = {
        "hosts": [SUPPLY_HOST],
        "supplies": supplies,
        "protocol": "portout",
        "portout_version": 1,
        "output_port": 1,
        "manual_fixtures": "",
    }
    base.update(options)
    return MockConfigEntry(
        domain=DOMAIN,
        data={"hosts": [SUPPLY_HOST], "supplies": supplies},
        options=base,
        unique_id=SUPPLY_SERIAL,
        title=f"Data Enabler Pro ({SUPPLY_HOST})",
    )


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, entry)
    return entry


async def _settle(hass, entry):
    await hass.async_block_till_done()
    task = getattr(entry.runtime_data, "initial_task", None)
    if task is not None and not task.done():
        await task
    await hass.async_block_till_done()


def _entity_id(hass, domain, unique_id):
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


def _light_packets(script):
    packets = []
    for data, addr in script.sent:
        if packet_type_of(data) in (TYPE_DMXOUT, TYPE_PORTOUT):
            packets.append((parse_light_data(data), addr))
    return packets


async def _wait_light(script, predicate):
    deadline = asyncio.get_running_loop().time() + 1.0

    def ready():
        packets = _light_packets(script)
        return bool(packets) and predicate(packets[-1][0])

    while asyncio.get_running_loop().time() < deadline:
        if ready():
            return _light_packets(script)[-1][0]
        await asyncio.sleep(0.01)
    raise AssertionError("light packet did not match")


def test_scale_rgb_rounds_half_up():
    assert scale_rgb((255, 0, 0), 255) == b"\xff\x00\x00"
    assert scale_rgb((255, 255, 255), 128) == b"\x80\x80\x80"
    assert scale_rgb((255, 0, 0), 1) == b"\x01\x00\x00"
    assert scale_rgb((255, 128, 0), 0) == b"\x00\x00\x00"


async def test_discovery_creates_a_light_per_fixture_of_both_types(hass, fake_udp):
    entry = await _setup(hass, _entry())
    registry = er.async_get(hass)
    states = []
    for serial, dmx, type_code in FIXTURES:
        entity_id = _entity_id(hass, "light", f"{serial:08X}")
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state.state == STATE_OFF
        assert state.attributes["dmx_start"] == dmx + 1
        assert state.attributes["type_code"] == f"0x{type_code:02X}"
        assert state.attributes["supply"] == SUPPLY_HOST
        states.append(state)
        device = dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, f"{serial:08X}"), entry.entry_id
        )
        assert device is not None
        assert device.model == {
            0x18: "Blast PowerCore gen4 3-Ch",
            0x04: "iW/iColor Burst Powercore",
        }[type_code]
        assert device.sw_version is None
        assert device.via_device_id is not None
    assert len(states) == 8
    button = _entity_id(hass, "button", f"{SUPPLY_SERIAL}:rescan")
    assert button is not None
    assert hass.states.get(button).state == "unknown"
    supply = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SUPPLY_SERIAL), entry.entry_id
    )
    assert supply.model == "Data Enabler Pro"
    assert supply.sw_version == "SFT-000155-00 V34"
    assert supply.serial_number == SUPPLY_SERIAL
    for data, addr in fake_udp.sent:
        assert packet_type_of(data) in ALLOWED_OUTGOING_TYPES
        assert addr == (SUPPLY_HOST, 6038)
    assert registry.async_get(button) is not None


async def test_rgb_brightness_and_off_map_onto_dmx_channels(hass, fake_udp):
    entry = await _setup(hass, _entry())
    first = _entity_id(hass, "light", "880055BE")
    other = _entity_id(hass, "light", "760054A3")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": first, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    packet = await _wait_light(
        fake_udp, lambda item: item.data[0:3] == b"\xff\x00\x00"
    )
    assert packet.packet_type == TYPE_PORTOUT
    assert packet.version == 1
    assert packet.port == 1
    assert packet.universe == 0xFFFFFFFF
    assert packet.data[0:3] == b"\xff\x00\x00"
    assert len(packet.data) >= 24

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": other, "rgb_color": [0, 0, 255], "brightness": 128},
        blocking=True,
    )
    packet = await _wait_light(fake_udp, lambda item: item.data[12:15] == b"\x00\x00\x80")
    assert packet.data[0:3] == b"\xff\x00\x00"
    assert packet.data[12:15] == b"\x00\x00\x80"
    assert hass.states.get(other).attributes["brightness"] == 128
    assert hass.states.get(other).attributes["rgb_color"] == (0, 0, 255)
    assert hass.states.get(other).state == STATE_ON

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": first, "hs_color": [120, 100]},
        blocking=True,
    )
    await _wait_light(fake_udp, lambda item: item.data[0] == 0 and item.data[1] > 0)

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": first, "xy_color": [0.3, 0.3]},
        blocking=True,
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": other, "brightness": 0},
        blocking=True,
    )
    assert hass.states.get(other).state == STATE_OFF
    packet = await _wait_light(fake_udp, lambda item: item.data[12:15] == b"\x00\x00\x00")
    assert packet.data[12:15] == b"\x00\x00\x00"

    hub = entry.runtime_data
    hub.write_channels(SUPPLY_HOST, 600, b"\x01\x02\x03")
    assert hub._transmit("10.9.9.9") is False


async def test_rate_cap_and_keepalive_use_the_frame_pump(hass, fake_udp):
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    assert hub.pump._min_interval == pytest.approx(1 / 40)
    assert hub.pump._keepalive == pytest.approx(1.0)
    await hub.pump.stop()
    clock = {"t": 50.0}
    hub.pump._now = lambda: clock["t"]
    hub.pump._last.clear()
    hub.pump._dirty.clear()
    hub.pump._armed.clear()
    before = len(_light_packets(fake_udp))
    entity_id = _entity_id(hass, "light", "880055BE")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id, "rgb_color": [255, 0, 0]},
        blocking=True,
    )
    assert hub.pump.seconds_until_due() == 0
    hub.pump.flush()
    assert len(_light_packets(fake_udp)) == before + 1
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id, "rgb_color": [0, 0, 255]},
        blocking=True,
    )
    assert hub.pump.seconds_until_due() == pytest.approx(1 / 40)
    hub.pump.flush()
    assert len(_light_packets(fake_udp)) == before + 1
    clock["t"] += 1 / 40
    hub.pump.flush()
    assert _light_packets(fake_udp)[-1][0].data[0:3] == b"\x00\x00\xff"
    clock["t"] += 1.0
    hub.pump.flush()
    assert len(_light_packets(fake_udp)) == before + 3
    assert _light_packets(fake_udp)[-1][0].data[0:3] == b"\x00\x00\xff"


async def test_names_and_areas_survive_rescan_and_missing_fixtures_stay(hass, fake_udp):
    entry = await _setup(
        hass,
        _entry(manual_fixtures="880055BE, 4, Oak left\n8800565C, 30, Far"),
    )
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device_by_identifier((DOMAIN, "880055BE"), entry.entry_id)
    area = ar.async_get(hass).async_create("Oak")
    dev_reg.async_update_device(device.id, area_id=area.id, name_by_user="Oak trunk")

    from dataclasses import replace

    discovered = entry.runtime_data.get_fixture("880055BE")
    entry.runtime_data._fixtures["ghost"] = replace(
        discovered, key="ghost", serial_hex=None, source="discovery"
    )
    fake_udp.limit = 1
    button = _entity_id(hass, "button", f"{SUPPLY_SERIAL}:rescan")
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    await hass.async_block_till_done()

    device = dev_reg.async_get_device_by_identifier((DOMAIN, "880055BE"), entry.entry_id)
    assert device.area_id == area.id
    assert device.name_by_user == "Oak trunk"
    assert hass.states.get(_entity_id(hass, "light", "880055BE")).state == STATE_OFF
    assert hass.states.get(_entity_id(hass, "light", "88002CD9")).state == STATE_OFF
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id(hass, "light", "88002CD9")).state == STATE_UNAVAILABLE
    assert hass.states.get(_entity_id(hass, "light", "8800565C")).attributes["dmx_start"] == 30

    fake_udp.limit = None
    fake_udp.fixtures.append((0x88009999, 24, 0x18))
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    await hass.async_block_till_done()
    assert _entity_id(hass, "light", "88009999") is not None
    assert hass.states.get(_entity_id(hass, "light", "88002CD9")).state == STATE_OFF
    assert hass.states.get(_entity_id(hass, "light", "8800565C")).attributes["dmx_start"] == 22


async def test_missing_properties_and_partial_scan(hass, fake_udp, monkeypatch):
    fake_udp.skip_props[0x760054A3] = {0x41}
    fake_udp.skip_props[0x760054AC] = {0x05}
    entry = await _setup(hass, _entry())
    missing_address = hass.states.get(_entity_id(hass, "light", "760054A3"))
    assert missing_address.state == STATE_UNAVAILABLE
    typed = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "760054AC"), entry.entry_id
    )
    assert typed.model == "Color Kinetics fixture"
    assert "type_code" not in hass.states.get(_entity_id(hass, "light", "760054AC")).attributes

    monkeypatch.setattr(
        "custom_components.color_kinetics.protocol.SCAN_TIMEOUT", 0.05
    )
    entry.runtime_data.client.scan_timeout = 0.05
    fake_udp.finish_scan = False
    fake_udp.limit = 3
    button = _entity_id(hass, "button", f"{SUPPLY_SERIAL}:rescan")
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id(hass, "light", "880055BE")).state == STATE_OFF
    # One incomplete scan is not enough to drop a fixture.
    assert hass.states.get(_entity_id(hass, "light", "880041E9")).state == STATE_OFF
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id(hass, "light", "880041E9")).state == STATE_UNAVAILABLE


async def test_one_missed_scan_keeps_the_color_and_the_second_marks_unavailable(
    hass, fake_udp
):
    entry = await _setup(hass, _entry())
    first = _entity_id(hass, "light", "880055BE")
    second = _entity_id(hass, "light", "88002CD9")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": first, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": second, "rgb_color": [0, 255, 0], "brightness": 255},
        blocking=True,
    )
    await _wait_light(fake_udp, lambda item: item.data[3:6] == b"\x00\xff\x00")
    fake_udp.fixtures = [fixture for fixture in fake_udp.fixtures if fixture[0] != 0x88002CD9]
    button = _entity_id(hass, "button", f"{SUPPLY_SERIAL}:rescan")
    before = len(_light_packets(fake_udp))
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(second).state == STATE_ON
    packet = await _wait_light(
        fake_udp,
        lambda item: len(_light_packets(fake_udp)) > before
        and item.data[0:3] == b"\xff\x00\x00"
        and item.data[3:6] == b"\x00\xff\x00",
    )
    assert packet.data[3:6] == b"\x00\xff\x00"
    assert len(packet.data) >= 24
    before = len(_light_packets(fake_udp))
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(second).state == STATE_UNAVAILABLE
    packet = await _wait_light(
        fake_udp,
        lambda item: len(_light_packets(fake_udp)) > before
        and item.data[0:3] == b"\xff\x00\x00"
        and item.data[3:6] == b"\x00\xff\x00",
    )
    assert packet.data[3:6] == b"\x00\xff\x00"
    assert entry.state is ConfigEntryState.LOADED


async def test_manual_address_is_used_when_the_high_byte_is_missing(hass, fake_udp):
    fake_udp.skip_props[0x880055BE] = {0x40}
    entry = await _setup(hass, _entry(manual_fixtures="880055BE, 10"))
    fixture = entry.runtime_data.get_fixture("880055BE")
    assert fixture is not None
    assert fixture.dmx_start == 9
    assert entry.runtime_data.is_available(fixture)
    assert hass.states.get(_entity_id(hass, "light", "880055BE")).state == STATE_OFF


async def test_a_failed_setup_closes_the_socket(hass, fake_udp, monkeypatch):
    entry = _entry()
    entry.add_to_hass(hass)

    async def boom(_entry, _platforms):
        raise RuntimeError("platforms failed")

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", boom)
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert fake_udp.transports
    assert all(transport.closed for transport in fake_udp.transports)


async def test_missing_high_byte_or_bad_status_leaves_the_address_unknown(
    hass, fake_udp
):
    fake_udp.skip_props[0x880055BE] = {0x40}
    fake_udp.status_overrides[(0x88002CD9, 0x40)] = 0x00
    entry = await _setup(hass, _entry())
    missing = entry.runtime_data.get_fixture("880055BE")
    bad = entry.runtime_data.get_fixture("88002CD9")
    assert missing is not None and missing.dmx_start is None
    assert bad is not None and bad.dmx_start is None
    assert not entry.runtime_data.is_available(missing)
    assert not entry.runtime_data.is_available(bad)
    assert hass.states.get(_entity_id(hass, "light", "880055BE")).state == STATE_UNAVAILABLE
    assert hass.states.get(_entity_id(hass, "light", "88002CD9")).state == STATE_UNAVAILABLE


async def test_manual_fixture_fallback_and_invalid_list(hass, fake_udp):
    entry = await _setup(hass, _entry(manual_fixtures="Porch wash, 40"))
    entity_id = _entity_id(hass, "light", "manual:172.16.10.41:porch-wash")
    assert entity_id is not None
    assert hass.states.get(entity_id).attributes["dmx_start"] == 40
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id, "rgb_color": [0, 255, 0], "brightness": 255},
        blocking=True,
    )
    packet = await _wait_light(fake_udp, lambda item: item.data[39:42] == b"\x00\xff\x00")
    assert packet.data[39:42] == b"\x00\xff\x00"

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "manual_fixtures": "this is not valid"}
    )
    await _settle(hass, entry)
    assert hass.states.get(_entity_id(hass, "light", "880055BE")) is not None


async def test_protocol_options_switch_v1_v2_and_port(hass, fake_udp):
    entry = await _setup(hass, _entry())
    entity_id = _entity_id(hass, "light", "880055BE")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id, "rgb_color": [255, 0, 0], "brightness": 200},
        blocking=True,
    )
    await _wait_light(fake_udp, lambda item: item.packet_type == TYPE_PORTOUT)

    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, "protocol": "dmxout"},
    )
    await _settle(hass, entry)
    packet = await _wait_light(fake_udp, lambda item: item.packet_type == TYPE_DMXOUT)
    assert packet.version == 1
    assert packet.data[0] > 0

    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, "protocol": "portout", "portout_version": 2, "output_port": 7},
    )
    await _settle(hass, entry)
    packet = await _wait_light(
        fake_udp, lambda item: item.packet_type == TYPE_PORTOUT and item.version == 2
    )
    assert packet.port == 7
    assert packet.version == 2


async def test_restore_unload_and_reload(hass, fake_udp):
    entry = await _setup(hass, _entry())
    entity_id = _entity_id(hass, "light", "8800565C")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id, "rgb_color": [10, 20, 30], "brightness": 40},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, entry)
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes["rgb_color"] == (10, 20, 30)
    assert state.attributes["brightness"] == 40
    packet = await _wait_light(
        fake_udp, lambda item: item.data[21:24] == scale_rgb((10, 20, 30), 40)
    )
    assert packet.data[21:24] == scale_rgb((10, 20, 30), 40)


async def test_rescan_button_errors_and_unreachable_supply(hass, fake_udp, monkeypatch):
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    button = _entity_id(hass, "button", f"{SUPPLY_SERIAL}:rescan")
    lock = hub._locks.setdefault(SUPPLY_HOST, asyncio.Lock())
    await lock.acquire()
    try:
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "button", "press", {"entity_id": button}, blocking=True
            )
    finally:
        lock.release()

    fake_udp.drop_types.add(0x0207)
    monkeypatch.setattr(
        "custom_components.color_kinetics.protocol.SCAN_TIMEOUT", 0.05
    )
    hub.client.scan_timeout = 0.05
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": button}, blocking=True
        )
    assert hass.states.get(_entity_id(hass, "light", "880055BE")).state == STATE_OFF

    with pytest.raises(Exception):
        await hub.async_rescan("10.9.9.9")


async def test_startup_poll_timeout_raises_an_issue_and_keeps_cache(hass, fake_udp, monkeypatch):
    monkeypatch.setattr("custom_components.color_kinetics.protocol.POLL_TIMEOUT", 0.05)
    fake_udp.drop_types.add(0x0001)
    entry = await _setup(hass, _entry())
    from homeassistant.helpers import issue_registry as ir

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, "supply_unreachable_172_16_10_41"
    )
    assert issue is not None
    assert _entity_id(hass, "button", f"{SUPPLY_SERIAL}:rescan") is not None
    assert _entity_id(hass, "light", "880055BE") is None

    hub = entry.runtime_data
    hub._load_cache(
        {"fixtures": {"bad": {"nope": True}, "manual-skip": {
            "key": "manual-skip",
            "host": SUPPLY_HOST,
            "source": "manual",
            "dmx_start": 0,
            "present": True,
        }}}
    )
    assert "bad" not in hub._fixtures
    assert "manual-skip" not in hub._fixtures

    async def explode(_host):
        raise RuntimeError("boom")

    hub.client.poll_supply = explode  # type: ignore[method-assign]
    await hub.async_initial_refresh()
    assert isinstance(hub.last_error, RuntimeError)

    hub._scanned_hosts.discard(SUPPLY_HOST)
    await hub._locks.setdefault(SUPPLY_HOST, asyncio.Lock()).acquire()
    try:
        hub.last_error = None
        hub.client.poll_supply = entry.runtime_data.client.poll_supply
    finally:
        hub._locks[SUPPLY_HOST].release()


async def test_unload_failure_leaves_the_socket_open(hass, fake_udp, monkeypatch):
    entry = await _setup(hass, _entry())
    from custom_components.color_kinetics import async_unload_entry

    async def _refuse(*_args, **_kwargs):
        return False

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", _refuse)
    try:
        assert await async_unload_entry(hass, entry) is False
        assert entry.runtime_data.client.transport is not None
    finally:
        await entry.runtime_data.async_stop()


async def test_light_edges_and_a_failed_first_scan(hass, fake_udp, monkeypatch):
    from homeassistant.components.light import DATA_COMPONENT
    from homeassistant.core import State

    monkeypatch.setattr(
        "custom_components.color_kinetics.protocol.SCAN_TIMEOUT", 0.05
    )
    fake_udp.drop_types.add(0x0207)
    failed = await _setup(hass, _entry())
    from homeassistant.helpers import issue_registry as ir

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "scan_failed_172_16_10_41")
        is not None
    )
    await failed.runtime_data.async_stop()
    assert await hass.config_entries.async_unload(failed.entry_id)
    await hass.async_block_till_done()

    fake_udp.drop_types.discard(0x0207)
    entry = await _setup(hass, _entry())
    entity_id = _entity_id(hass, "light", "880055BE")
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)
    hub_for_dup = entry.runtime_data
    assert entity._device_info(None)["identifiers"] == {(DOMAIN, entity._key)}
    entity._attr_is_on = False
    assert entity._channels() == b"\x00\x00\x00"
    await entity.async_turn_on(brightness=0)
    assert entity.is_on is False
    await entity.async_turn_on(hs_color=(120, 100))
    from homeassistant.util import color as color_util

    expected_hs = scale_rgb(color_util.color_hs_to_RGB(120, 100), 255)
    packet = await _wait_light(fake_udp, lambda item: item.data[0:3] == expected_hs)
    assert packet.data[0:3] == expected_hs
    await entity.async_turn_on(xy_color=(0.3, 0.3))
    expected_xy = scale_rgb(color_util.color_xy_to_RGB(0.3, 0.3), 255)
    packet = await _wait_light(fake_udp, lambda item: item.data[0:3] == expected_xy)
    assert packet.data[0:3] == expected_xy
    assert entity.is_on is True
    fake_udp.fixtures.append(fake_udp.fixtures[0])
    await hub_for_dup.async_rescan(SUPPLY_HOST)

    async def _no_extra():
        return None

    async def _last_state():
        return State(entity_id, "on", {"brightness": 12, "rgb_color": (1, 2, 3)})

    entity.async_get_last_extra_data = _no_extra  # type: ignore[method-assign]
    entity.async_get_last_state = _last_state  # type: ignore[method-assign]
    await entity.async_added_to_hass()
    assert entity._attr_brightness == 12
    assert entity._attr_rgb_color == (1, 2, 3)

    from dataclasses import replace

    hub = entry.runtime_data
    from custom_components.color_kinetics.hub import Fixture

    assert (
        Fixture(
            key="x",
            host=SUPPLY_HOST,
            serial_hex=None,
            label=None,
            dmx_start=None,
            type_code=None,
            present=True,
            source="manual",
            properties={},
        ).display_name
        == "Color Kinetics fixture"
    )
    current = hub.get_fixture("880055BE")
    hub._fixtures["880055BE"] = replace(current, host="10.9.9.9")
    entity._push()
    hub._fixtures.pop("880055BE")
    assert entity.extra_state_attributes == {}
    hub._fixtures.clear()
    hub._span.clear()
    assert hub._transmit(SUPPLY_HOST) is False
    hub.write_channels("10.1.1.1", 0, b"\x01\x02\x03")


async def test_scan_in_progress_during_refresh_is_ignored(hass, fake_udp):
    entry = await _setup(hass, _entry())
    hub: ColorKineticsHub = entry.runtime_data
    hub._scanned_hosts.discard(SUPPLY_HOST)
    await hub._locks.setdefault(SUPPLY_HOST, asyncio.Lock()).acquire()
    try:
        await hub.async_initial_refresh()
    finally:
        hub._locks[SUPPLY_HOST].release()
    assert _entity_id(hass, "light", "880055BE") is not None


def _offsets(data: bytes, payload: bytes, *, skip: int | None = None) -> None:
    """Check every discovered fixture. ``skip`` is left alone: it must not be required to be zero."""
    for _serial, start, _type_code in FIXTURES:
        if start == skip:
            continue
        assert data[start : start + 3] == payload


async def test_all_fixtures_paints_every_fixture_in_one_frame(hass, fake_udp):
    await _setup(hass, _entry())
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    button = _entity_id(hass, "button", f"{SUPPLY_SERIAL}:rescan")
    registry = er.async_get(hass)
    group_entry = registry.async_get(group)
    button_entry = registry.async_get(button)
    assert group_entry.device_id == button_entry.device_id
    assert group_entry.entity_category is None
    assert "All fixtures" in hass.states.get(group).name

    before = len(_light_packets(fake_udp))
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    packet = await _wait_light(
        fake_udp,
        lambda item: len(_light_packets(fake_udp)) == before + 1
        and item.data[0:3] == b"\xff\x00\x00",
    )
    _offsets(packet.data, b"\xff\x00\x00")
    assert hass.states.get(group).state == STATE_ON
    assert hass.states.get(group).attributes["rgb_color"] == (255, 0, 0)
    assert hass.states.get(_entity_id(hass, "light", "88005B93")).attributes["rgb_color"] == (
        255,
        0,
        0,
    )

    before = len(_light_packets(fake_udp))
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "brightness": 128},
        blocking=True,
    )
    packet = await _wait_light(
        fake_udp,
        lambda item: len(_light_packets(fake_udp)) == before + 1
        and item.data[0:3] == b"\x80\x00\x00",
    )
    _offsets(packet.data, b"\x80\x00\x00")
    await asyncio.sleep(0.05)
    assert len(_light_packets(fake_udp)) == before + 1

    from homeassistant.components.light import DATA_COMPONENT

    entity = hass.data[DATA_COMPONENT].get_entity(group)
    before = len(_light_packets(fake_udp))
    await entity.async_turn_on(hs_color=(0, 100), brightness=255)
    packet = await _wait_light(
        fake_udp,
        lambda item: len(_light_packets(fake_udp)) == before + 1
        and item.data[0:3] == b"\xff\x00\x00",
    )
    _offsets(packet.data, b"\xff\x00\x00")
    from homeassistant.util.color import color_xy_to_RGB

    expected = scale_rgb(color_xy_to_RGB(0.3, 0.3), 255)
    before = len(_light_packets(fake_udp))
    await entity.async_turn_on(xy_color=(0.3, 0.3), brightness=255)
    packet = await _wait_light(
        fake_udp,
        lambda item: len(_light_packets(fake_udp)) == before + 1
        and item.data[0:3] == expected,
    )
    _offsets(packet.data, expected)

    kept = tuple(hass.states.get(group).attributes["rgb_color"])
    one = _entity_id(hass, "light", "880055BE")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": one, "rgb_color": [0, 0, 255], "brightness": 255},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(group).state == STATE_ON
    assert tuple(hass.states.get(group).attributes["rgb_color"]) == kept
    assert kept != (0, 0, 255)

    before = len(_light_packets(fake_udp))
    await entity.async_turn_on(brightness=0)
    packet = await _wait_light(
        fake_udp,
        lambda item: len(_light_packets(fake_udp)) == before + 1
        and item.data[0:3] == b"\x00\x00\x00",
    )
    _offsets(packet.data, b"\x00\x00\x00")
    assert hass.states.get(group).state == STATE_OFF
    assert hass.states.get(one).state == STATE_OFF
    from homeassistant.const import STATE_UNAVAILABLE

    hub = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    hub._fixtures.clear()
    await entity.async_turn_on(rgb_color=(1, 2, 3))
    assert hass.states.get(group).state == STATE_UNAVAILABLE


async def test_all_fixtures_skips_an_unavailable_fixture_and_a_manual_one(hass, fake_udp, monkeypatch):
    monkeypatch.setattr(
        "custom_components.color_kinetics.protocol.PROP_TIMEOUT", 0.05
    )
    fake_udp.skip_props[0x88005B93] = {0x40}
    await _setup(hass, _entry(manual_fixtures="Porch wash, 40"))
    group = _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures")
    before = len(_light_packets(fake_udp))
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": group, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    packet = await _wait_light(
        fake_udp,
        lambda item: len(_light_packets(fake_udp)) == before + 1
        and item.data[0:3] == b"\xff\x00\x00",
    )
    _offsets(packet.data, b"\xff\x00\x00", skip=6)
    assert packet.data[39:42] == b"\x00\x00\x00"
    assert hass.states.get(_entity_id(hass, "light", "88005B93")).state == STATE_UNAVAILABLE
    await asyncio.sleep(0.05)
    assert len(_light_packets(fake_udp)) == before + 1


async def test_offline_88005b93_stays_until_a_second_scan_misses_it(hass, fake_udp):
    await _setup(hass, _entry())
    entity_id = _entity_id(hass, "light", "88005B93")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    await _wait_light(fake_udp, lambda item: item.data[6:9] == b"\xff\x00\x00")
    fake_udp.fixtures = [fixture for fixture in fake_udp.fixtures if fixture[0] != 0x88005B93]
    button = _entity_id(hass, "button", f"{SUPPLY_SERIAL}:rescan")
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    packet = await _wait_light(fake_udp, lambda item: item.data[6:9] == b"\xff\x00\x00")
    assert packet.data[6:9] == b"\xff\x00\x00"
    assert hass.states.get(_entity_id(hass, "sensor", "88005B93:dmx_address")).state == (
        STATE_UNAVAILABLE
    )


async def test_reload_keeps_the_color_of_an_unavailable_fixture(hass, fake_udp):
    """A reload must not paint 00 00 00 over a fixture that missed two scans."""
    entry = await _setup(hass, _entry())
    entity_id = _entity_id(hass, "light", "88002CD9")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id, "rgb_color": [0, 255, 0], "brightness": 255},
        blocking=True,
    )
    await _wait_light(fake_udp, lambda item: item.data[3:6] == b"\x00\xff\x00")
    fake_udp.fixtures = [fixture for fixture in fake_udp.fixtures if fixture[0] != 0x88002CD9]
    button = _entity_id(hass, "button", f"{SUPPLY_SERIAL}:rescan")
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    before = len(_light_packets(fake_udp))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await _settle(hass, entry)
    packet = await _wait_light(
        fake_udp,
        lambda item: len(_light_packets(fake_udp)) > before
        and item.data[3:6] == b"\x00\xff\x00",
    )
    assert packet.data[3:6] == b"\x00\xff\x00"


async def test_frames_are_at_least_24_bytes_and_keep_a_saved_color(hass, fake_udp):
    """Never-set slots are off. A saved color in the middle of the frame stays."""
    from dataclasses import replace

    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    hub._saved_channels.clear()
    hub._span[SUPPLY_HOST] = 3
    hub._buffers[SUPPLY_HOST] = bytearray(512)
    hub._buffers[SUPPLY_HOST][0:3] = b"\xff\x00\x00"
    assert hub._transmit(SUPPLY_HOST) is True
    packet = _light_packets(fake_udp)[-1][0]
    assert len(packet.data) >= 24
    assert packet.data[0:3] == b"\xff\x00\x00"
    assert packet.data[6:9] == b"\x00\x00\x00"

    middle = hub.get_fixture("88005B93")
    hub._fixtures["88005B93"] = replace(middle, present=False)
    neighbor = hub.get_fixture("88002CD9")
    hub._fixtures["88002CD9"] = replace(neighbor, present=False)
    hub._saved_channels.pop("88002CD9", None)
    hub._saved_channels["88005B93"] = b"\x00\xff\x00"
    hub._buffers[SUPPLY_HOST][6:9] = b"\x00\x00\x00"
    hub.write_channels(SUPPLY_HOST, 9, b"\xff\x00\x00", schedule=False)
    assert hub._transmit(SUPPLY_HOST) is True
    packet = _light_packets(fake_udp)[-1][0]
    assert len(packet.data) >= 24
    assert packet.data[6:9] == b"\x00\xff\x00"
    assert packet.data[9:12] == b"\xff\x00\x00"

    hub._fixtures.clear()
    hub._saved_channels.clear()
    hub._span.clear()
    hub._buffers[SUPPLY_HOST] = bytearray(512)
    assert hub._transmit(SUPPLY_HOST) is False
    hub._load_cache({"channels": {"880055BE": 1, "88002CD9": "zz"}, "fixtures": {}})
    hub._saved_channels["missing"] = b"\x01\x02\x03"
    hub._restore_saved_channels()


async def test_removing_the_entry_deletes_the_channel_store(hass, fake_udp, hass_storage):
    entry = await _setup(hass, _entry())
    await entry.runtime_data._save()
    key = f"color_kinetics_{entry.entry_id}"
    assert "channels" in hass_storage[key]["data"]
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_address_reread_timeout_keeps_the_previous_start(hass, fake_udp):
    entry = await _setup(hass, _entry())
    hub = entry.runtime_data
    fixture = hub.get_fixture("880055BE")
    assert fixture is not None and fixture.dmx_start == 0
    previous_low = fixture.properties.get(0x40)
    fake_udp.skip_props[0x880055BE] = {0x40, 0x05}
    button = _entity_id(hass, "button", f"{SUPPLY_SERIAL}:rescan")
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done()
    updated = hub.get_fixture("880055BE")
    assert updated is not None
    assert updated.dmx_start == 0
    assert updated.properties.get(0x40) == previous_low
    assert updated.type_code == fixture.type_code
    assert hub.is_available(updated)
