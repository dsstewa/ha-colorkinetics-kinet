"""Byte-exact encoders and parsers, including garbage input."""

from __future__ import annotations

import struct

import pytest

from custom_components.color_kinetics.protocol import (
    ALLOWED_OUTGOING_TYPES,
    FORBIDDEN_OUTGOING_TYPES,
    TYPE_DMXOUT,
    TYPE_FIXTURE_PROP,
    TYPE_FIXTURE_PROP_REPLY,
    TYPE_FIXTURE_SCAN,
    TYPE_FIXTURE_SCAN_REPLY,
    TYPE_POLL,
    TYPE_POLL_REPLY,
    TYPE_PORT_LIST,
    TYPE_PORT_LIST_REPLY,
    TYPE_PORTOUT,
    TYPE_READ_SUPPLY_PROP,
    TYPE_SET_IP,
    TYPE_SUPPLY_PROP_REPLY,
    TYPE_SYNC,
    BroadcastNotAllowedError,
    ForbiddenPacketError,
    InvalidHostError,
    ProtocolError,
    assert_outgoing_allowed,
    assert_unicast_host,
    build_dmxout,
    build_fixture_property_read,
    build_fixture_scan,
    build_poll,
    build_port_list,
    build_portout,
    build_supply_property_read,
    dmx_start_zero_based,
    format_fixture_serial,
    parse_fixture_property,
    parse_header,
    parse_light_data,
    parse_poll_reply,
    parse_port_list,
    parse_scan_reply,
    parse_supply_property,
    supply_entry_dict,
)
from tests.fake_kinet import (
    FIXTURES,
    fixture_property_reply,
    load_capture,
    scan_progress,
    scan_record,
)

_OLA = [1, 5, 8, 10, 14, 45, 100, 255]
_PACKETS = load_capture()


def _raw(packet: dict) -> bytes:
    return bytes.fromhex(packet["hex"])


def test_ola_golden_vectors_match_bytes_and_reference_encoder():
    """The four OLA KiNetNodeTest vectors, as literal bytes."""
    assert build_dmxout(0, bytes(_OLA)) == bytes(
        [4, 1, 0xDC, 0x4A, 1, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 255, 255, 0]
        + _OLA
    )

    padded = bytes(_OLA) + bytes(16)
    assert build_portout(0, 7, bytes(_OLA)) == bytes(
        [
            4, 1, 0xDC, 0x4A, 1, 0, 8, 1, 0, 0, 0, 0,
            255, 255, 255, 255, 7, 0, 0, 0, 24, 0, 0, 0,
        ]
        + list(padded)
    )
    full = bytes(range(1, 25))
    assert build_portout(1, 7, full) == bytes(
        [
            4, 1, 0xDC, 0x4A, 1, 0, 8, 1, 1, 0, 0, 0,
            255, 255, 255, 255, 7, 0, 0, 0, 24, 0, 0, 0,
        ]
        + list(full)
    )
    edge = bytes([255] + [0] * 23 + [255])
    assert build_portout(2, 255, edge) == bytes(
        [
            4, 1, 0xDC, 0x4A, 1, 0, 8, 1, 2, 0, 0, 0,
            255, 255, 255, 255, 255, 0, 0, 0, 25, 0, 0, 0,
        ]
        + list(edge)
    )


def test_portout_version_two_is_the_ola_packet_with_a_different_version_field():
    """Haukcode's header version is not a second encoder. Only bytes 4-5 change."""
    ola = build_portout(0, 7, bytes(_OLA), version=1)
    hauk = build_portout(0, 7, bytes(_OLA), version=2)
    assert ola[:4] == hauk[:4] == bytes([4, 1, 0xDC, 0x4A])
    assert ola[4:6] == b"\x01\x00"
    assert hauk[4:6] == b"\x02\x00"
    assert ola[6:] == hauk[6:]
    assert hauk[6:8] == b"\x08\x01"


def test_portout_version_field_is_the_only_difference_between_ola_and_haukcode():
    version_1 = build_portout(0, 1, bytes(_OLA), version=1)
    version_2 = build_portout(0, 1, bytes(_OLA), version=2)
    assert version_1[4:6] == b"\x01\x00"
    assert version_2[4:6] == b"\x02\x00"
    assert version_1[:4] == version_2[:4]
    assert version_1[6:] == version_2[6:]


def test_encoder_rejects_bad_lengths_ports_and_versions():
    with pytest.raises(ProtocolError):
        build_dmxout(0, b"")
    with pytest.raises(ProtocolError):
        build_dmxout(0, bytes(513))
    with pytest.raises(ProtocolError):
        build_portout(0, -1, b"\x01")
    with pytest.raises(ProtocolError):
        build_portout(0, 256, b"\x01")
    with pytest.raises(ProtocolError):
        build_portout(0, 1, bytes(513))
    with pytest.raises(ProtocolError):
        build_portout(0, 1, b"\x01", version=3)
    assert build_portout(0, 1, b"")[24:] == bytes(24)


def test_captured_requests_rebuild_byte_for_byte():
    """Every captured read request matches the encoder. 0x0109 is not built."""
    seen_sync = False
    for packet in _PACKETS:
        if not str(packet["src"]).startswith("172.16.11.134"):
            continue
        raw = _raw(packet)
        header = parse_header(raw)
        if header.packet_type == TYPE_POLL:
            assert build_poll(header.sequence) == raw
        elif header.packet_type == TYPE_PORT_LIST:
            assert build_port_list(header.sequence) == raw
        elif header.packet_type == TYPE_READ_SUPPLY_PROP:
            prop = struct.unpack_from("<I", raw, 12)[0]
            assert build_supply_property_read(header.sequence, prop) == raw
        elif header.packet_type == TYPE_FIXTURE_SCAN:
            assert build_fixture_scan(header.sequence) == raw
        elif header.packet_type == TYPE_FIXTURE_PROP:
            serial, prop = struct.unpack_from("<II", raw, 12)
            assert build_fixture_property_read(header.sequence, serial, prop) == raw
        elif header.packet_type == TYPE_SYNC:
            seen_sync = True
            with pytest.raises(ForbiddenPacketError):
                assert_outgoing_allowed(TYPE_SYNC)
        else:
            raise AssertionError(f"unexpected request type {header.packet_type:#x}")
    assert seen_sync


def test_every_captured_reply_parses_and_fixture_table_matches():
    props: dict[int, dict[int, int]] = {}
    supply_values: dict[int, bytes] = {}
    serials: set[int] = set()
    polls = 0
    ports = 0
    supply_props = 0
    scans = 0
    fixture_props = 0
    for packet in _PACKETS:
        raw = _raw(packet)
        if len(raw) >= 12:
            header = parse_header(raw)
        else:
            header = None
        if not str(packet["src"]).startswith("172.16.10.41"):
            continue
        assert header is not None
        if header.packet_type == TYPE_POLL_REPLY:
            polls += 1
            info = parse_poll_reply(raw)
            assert info.ip == "172.16.10.41"
            assert info.mac == "00:0a:c5:47:1f:7f"
            assert info.protocol_version == 2
            assert info.serial[:4] == bytes.fromhex("83eb007f")
            assert info.serial_hex == "7F00EB83"
            assert info.manufacturer == "Philips Color Kinetics"
            assert info.model == "Data Enabler Pro"
            assert info.firmware_part == "SFT-000155-00"
            assert info.firmware_version == "V34"
            assert info.firmware == "SFT-000155-00 V34"
            assert info.label == "Data Enabler Pro"
            assert info.details.startswith("M:Philips Color Kinetics")
            assert supply_entry_dict(info)["firmware"] == "SFT-000155-00 V34"
        elif header.packet_type == TYPE_PORT_LIST_REPLY:
            ports += 1
            parsed = parse_port_list(raw)
            assert len(parsed.ports) == 1
            assert parsed.ports[0].port_id == 1
            assert parsed.ports[0].port_type == 1
            assert parsed.ports[0].extra == b"\x00\x00\x00"
        elif header.packet_type == TYPE_SUPPLY_PROP_REPLY:
            supply_props += 1
            parsed = parse_supply_property(raw)
            assert parsed.flag == 0x4000
            assert len(parsed.value) == 4
            assert struct.unpack_from("<HH", raw, 12)[0] == parsed.prop
            supply_values[parsed.prop] = parsed.value
        elif header.packet_type == TYPE_FIXTURE_SCAN_REPLY:
            scans += 1
            parsed = parse_scan_reply(raw)
            assert parsed.port == 1
            if parsed.serial is not None:
                serials.add(parsed.serial)
                assert scan_record(header.sequence, parsed.serial) == raw
            else:
                assert scan_progress(header.sequence, scanning=parsed.scanning) == raw
        elif header.packet_type == TYPE_FIXTURE_PROP_REPLY:
            fixture_props += 1
            parsed = parse_fixture_property(raw)
            serial, prop, value, status = struct.unpack_from("<IHBB", raw, 12)
            assert (parsed.serial, parsed.prop, parsed.value, parsed.status) == (
                serial,
                prop,
                value,
                status,
            )
            assert status == 0x09
            assert fixture_property_reply(
                header.sequence, serial, prop, value, status=status
            ) == raw
            props.setdefault(serial, {})[prop] = value
        else:
            raise AssertionError(f"unexpected reply {header.packet_type:#x}")

    assert polls == 2
    assert ports == 1
    assert supply_props == 8
    assert scans == 10
    assert fixture_props == 119
    assert serials == {serial for serial, _dmx, _type in FIXTURES}
    for serial, dmx, type_code in FIXTURES:
        assert props[serial][0x40] == (dmx >> 8) & 0xFF
        assert props[serial][0x41] == dmx & 0xFF
        assert props[serial][0x05] == type_code
        assert props[serial][0x43] == props[serial][0x44] == props[serial][0x45] == 0
        assert dmx_start_zero_based(props[serial][0x40], props[serial][0x41]) == dmx
        if type_code == 0x18:
            assert props[serial][0x60] == props[serial][0x61] == props[serial][0x62] == 255
        else:
            assert 0x60 not in props[serial]
    assert format_fixture_serial(0x880055BE) == "880055BE"
    type_04 = [serial for serial, _dmx, type_code in FIXTURES if type_code == 0x04]
    type_18 = [serial for serial, _dmx, type_code in FIXTURES if type_code == 0x18]
    assert len(type_04) == 2
    assert len(type_18) == 6
    assert 0x60 not in props[type_04[0]]
    assert props[type_18[0]][0x60] == 255
    assert supply_values[0x0052] == bytes.fromhex("2c010000")
    assert int.from_bytes(supply_values[0x0052], "little") == 300


def test_parsers_reject_short_wrong_type_and_garbage():
    garbage = [b"", b"\x00", b"\x00" * 11, b"not kinet!!!!", b"\xff" * 30]
    for blob in garbage:
        with pytest.raises(ProtocolError):
            parse_header(blob)
    bad_magic = struct.pack("<IHHI", 0x12345678, 1, TYPE_POLL_REPLY, 1) + bytes(40)
    with pytest.raises(ProtocolError):
        parse_header(bad_magic)

    poll = next(_raw(packet) for packet in _PACKETS if packet["typ"] == TYPE_POLL_REPLY)
    short_poll = poll[:20]
    with pytest.raises(ProtocolError):
        parse_poll_reply(short_poll)
    with pytest.raises(ProtocolError):
        parse_poll_reply(build_poll(1))
    no_label = bytearray(poll)
    # Drop everything after the details NUL so the label is empty but still valid.
    details_end = bytes(no_label).index(b"\x00", 32)
    assert parse_poll_reply(bytes(no_label[: details_end + 1])).label == ""

    colon_version = bytearray(poll)
    text = bytes(colon_version[32:]).replace(b"V34\n", b"V:34\n")
    rewritten = bytes(colon_version[:32]) + text
    assert parse_poll_reply(rewritten).firmware_version == "34"

    port_packet = next(
        _raw(packet) for packet in _PACKETS if packet["typ"] == TYPE_PORT_LIST_REPLY
    )
    with pytest.raises(ProtocolError):
        parse_port_list(port_packet[:10])
    with pytest.raises(ProtocolError):
        parse_port_list(build_port_list(1))
    short_header = struct.pack("<IHHI", 0x4ADC0104, 1, TYPE_PORT_LIST_REPLY, 1) + b"\x00\x00"
    with pytest.raises(ProtocolError):
        parse_port_list(short_header)
    blank = poll[:32] + b"\n" + poll[32:]
    assert parse_poll_reply(blank).manufacturer == "Philips Color Kinetics"
    truncated = bytearray(port_packet)
    truncated[12:16] = struct.pack("<I", 4)
    parsed = parse_port_list(bytes(truncated))
    assert len(parsed.ports) == 1
    huge = bytearray(port_packet)
    huge[12:16] = struct.pack("<I", 100)
    assert len(parse_port_list(bytes(huge)).ports) == 1

    supply = next(
        _raw(packet) for packet in _PACKETS if packet["typ"] == TYPE_SUPPLY_PROP_REPLY
    )
    with pytest.raises(ProtocolError):
        parse_supply_property(supply[:16])
    with pytest.raises(ProtocolError):
        parse_supply_property(build_supply_property_read(1, 1))

    scan = next(
        _raw(packet) for packet in _PACKETS if packet["typ"] == TYPE_FIXTURE_SCAN_REPLY
    )
    with pytest.raises(ProtocolError):
        parse_scan_reply(scan[:16])
    with pytest.raises(ProtocolError):
        parse_scan_reply(build_fixture_scan(1))
    assert parse_scan_reply(scan_progress(3, scanning=False)).serial is None

    prop = next(
        _raw(packet) for packet in _PACKETS if packet["typ"] == TYPE_FIXTURE_PROP_REPLY
    )
    with pytest.raises(ProtocolError):
        parse_fixture_property(prop[:16])
    with pytest.raises(ProtocolError):
        parse_fixture_property(build_fixture_property_read(1, 1, 1))

    dmx = build_dmxout(0, bytes(_OLA))
    port = build_portout(0, 7, bytes(_OLA), flags=4)
    assert parse_light_data(dmx).data == bytes(_OLA)
    assert parse_light_data(dmx).packet_type == TYPE_DMXOUT
    parsed_port = parse_light_data(port)
    assert parsed_port.port == 7
    assert parsed_port.flags == 4
    assert parsed_port.universe == 0xFFFFFFFF
    assert parsed_port.data[:8] == bytes(_OLA)
    with pytest.raises(ProtocolError):
        parse_light_data(dmx[:20])
    with pytest.raises(ProtocolError):
        parse_light_data(port[:23])
    with pytest.raises(ProtocolError):
        parse_light_data(build_poll(1))


def test_dmx_address_requires_both_bytes():
    assert dmx_start_zero_based(None, None) is None
    assert dmx_start_zero_based(1, None) is None
    assert dmx_start_zero_based(None, 21) is None
    assert dmx_start_zero_based(1, 2) == 258


def test_unicast_guard_rejects_broadcast_multicast_and_garbage():
    assert assert_unicast_host("172.16.10.41") == "172.16.10.41"
    assert assert_unicast_host(" 127.0.0.1 ") == "127.0.0.1"
    for host in (
        "255.255.255.255",
        "224.0.0.1",
        "0.0.0.0",
        "172.16.10.255",
        "172.16.10.0",
        "192.168.1.255",
        "10.255.255.255",
        "172.16.255.255",
        "169.254.255.255",
    ):
        with pytest.raises(BroadcastNotAllowedError):
            assert_unicast_host(host)
    for host in ("not-an-ip", "172.16.10.41.2", "::1", ""):
        with pytest.raises(InvalidHostError):
            assert_unicast_host(host)


def test_allow_list_and_forbidden_set_are_disjoint():
    assert ALLOWED_OUTGOING_TYPES.isdisjoint(FORBIDDEN_OUTGOING_TYPES)
    for packet_type in (
        TYPE_SET_IP,
        0x0005,
        0x0006,
        0x0103,
        TYPE_SYNC,
        0x0303,
        0xFFFF,
    ):
        with pytest.raises(ForbiddenPacketError) as caught:
            assert_outgoing_allowed(packet_type)
        assert f"{packet_type:#x}" in str(caught.value) or f"{packet_type:04x}" in str(
            caught.value
        )
    for packet_type in ALLOWED_OUTGOING_TYPES:
        assert_outgoing_allowed(packet_type)
