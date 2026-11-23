"""Diagnostic sensors for confirmed properties and the raw dump."""

from __future__ import annotations

import struct

import pytest
from homeassistant.const import STATE_OFF, STATE_UNKNOWN

from custom_components.color_kinetics.properties import (
    channel_triple,
    model_name,
    power_time_text,
    raw_property_key,
    supply_serial_hex,
)
from custom_components.color_kinetics.protocol import TYPE_FIXTURE_PROP, TYPE_READ_SUPPLY_PROP, packet_type_of
from tests.fake_kinet import SUPPLY_HOST, SUPPLY_SERIAL
from tests.test_light import _entity_id, _entry, _setup

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "fake_udp")


def test_property_formatters_match_the_confirmed_values():
    assert power_time_text(bytes.fromhex("2c010000")) == "5 minutes"
    assert power_time_text((60).to_bytes(4, "little")) == "1 minute"
    assert power_time_text((90).to_bytes(4, "little")) == "90 seconds"
    assert model_name(None) == "Color Kinetics fixture"
    assert model_name(0x99) == "Type 0x99"
    assert supply_serial_hex(b"\x83\xeb\x00\x7f\x00\x00\x00\x00") == "7F00EB83"
    assert supply_serial_hex(b"\x01\x02") == "0102"
    assert channel_triple({}, (0x43, 0x44, 0x45)) is None
    assert channel_triple({0x43: 1, 0x45: 0}, (0x43, 0x44, 0x45)) == "1 / unknown / 0"


async def test_confirmed_fields_and_raw_properties_come_from_the_capture(hass, fake_udp):
    entry = await _setup(hass, _entry())
    assert hass.states.get(_entity_id(hass, "sensor", f"{SUPPLY_SERIAL}:power_time")).state == (
        "5 minutes"
    )
    raw_supply = hass.states.get(_entity_id(hass, "sensor", f"{SUPPLY_SERIAL}:raw_properties"))
    assert raw_supply.state == "3 properties"
    assert raw_supply.attributes[raw_property_key(0x0051)] == "20000000"
    assert raw_supply.attributes[raw_property_key(0x044D)] == "ffff0180"
    assert raw_supply.attributes[raw_property_key(0x044E)] == "ffff0280"
    assert raw_property_key(0x0052) not in raw_supply.attributes

    assert hass.states.get(_entity_id(hass, "sensor", "880055BE:dmx_address")).state == "1"
    assert hass.states.get(_entity_id(hass, "sensor", "760054AC:dmx_address")).state == "10"
    assert hass.states.get(_entity_id(hass, "sensor", "760054A3:dmx_address")).state == "13"
    assert hass.states.get(_entity_id(hass, "sensor", "880055BE:startup_channels")).state == (
        "0 / 0 / 0"
    )
    assert hass.states.get(_entity_id(hass, "sensor", "760054AC:startup_channels")).state == (
        "0 / 0 / 0"
    )
    assert hass.states.get(_entity_id(hass, "sensor", "880055BE:scale_factors")).state == (
        "255 / 255 / 255"
    )
    assert _entity_id(hass, "sensor", "760054AC:scale_factors") is None

    blast = hass.states.get(_entity_id(hass, "sensor", "880055BE:raw_properties"))
    assert blast.state == "6 properties"
    assert blast.attributes[raw_property_key(0x6E)] == 1
    assert blast.attributes[raw_property_key(0xC0)] == 4
    assert blast.attributes[raw_property_key(0x6D)] == 1
    assert blast.attributes[raw_property_key(0x4F)] == 1
    assert blast.attributes[raw_property_key(0xCC)] == 0
    assert blast.attributes[raw_property_key(0xC2)] == 0
    assert raw_property_key(0x60) not in blast.attributes
    assert raw_property_key(0x43) not in blast.attributes

    burst = hass.states.get(_entity_id(hass, "sensor", "760054AC:raw_properties"))
    assert burst.state == "1 property"
    assert burst.attributes[raw_property_key(0x6E)] == 1
    assert raw_property_key(0xC0) not in burst.attributes

    burst_reads = []
    blast_reads = []
    supply_reads = []
    for data, _addr in fake_udp.sent:
        kind = packet_type_of(data)
        assert kind != 0x0103
        if kind == TYPE_READ_SUPPLY_PROP:
            supply_reads.append(struct.unpack_from("<I", data, 12)[0])
        if kind == TYPE_FIXTURE_PROP:
            serial, prop = struct.unpack_from("<II", data, 12)
            if serial == 0x760054AC:
                burst_reads.append(prop)
            if serial == 0x880055BE:
                blast_reads.append(prop)
    assert 0x0052 in supply_reads
    assert 0x60 not in burst_reads
    assert 0x60 in blast_reads
    assert hass.states.get(_entity_id(hass, "light", "880055BE")).state == STATE_OFF

    entry.runtime_data._supplies[SUPPLY_HOST]["properties"]["0052"] = "zz"
    assert entry.runtime_data.supply_property(SUPPLY_HOST, 0x0052) is None
    from homeassistant.components.sensor import DATA_COMPONENT

    dmx = hass.data[DATA_COMPONENT].get_entity(
        _entity_id(hass, "sensor", "880055BE:dmx_address")
    )
    entry.runtime_data._fixtures.pop("880055BE")
    dmx.refresh()
    assert dmx.available is False
    entry.runtime_data._drop_stale_registry_entries()
    assert _entity_id(hass, "sensor", f"{SUPPLY_SERIAL}:power_time") is not None
    assert _entity_id(hass, "light", f"{SUPPLY_SERIAL}:all_fixtures") is not None


async def test_a_missing_property_is_unknown_and_the_fixture_remains(hass, fake_udp, monkeypatch):
    monkeypatch.setattr("custom_components.color_kinetics.protocol.PROP_TIMEOUT", 0.05)
    for prop in (0x0051, 0x0052, 0x044D, 0x044E):
        fake_udp.supply_props.pop(prop)
    fake_udp.skip_props[0x880055BE] = {0x43}
    fake_udp.skip_props[0x760054AC] = {0x6E}
    await _setup(hass, _entry())
    assert hass.states.get(_entity_id(hass, "sensor", f"{SUPPLY_SERIAL}:power_time")).state == (
        STATE_UNKNOWN
    )
    assert hass.states.get(_entity_id(hass, "sensor", f"{SUPPLY_SERIAL}:raw_properties")).state == (
        "0 properties"
    )
    assert hass.states.get(_entity_id(hass, "sensor", "880055BE:startup_channels")).state == (
        "unknown / 0 / 0"
    )
    assert hass.states.get(_entity_id(hass, "light", "880055BE")).state == STATE_OFF
    assert hass.states.get(_entity_id(hass, "sensor", "760054AC:raw_properties")).state == (
        "0 properties"
    )
