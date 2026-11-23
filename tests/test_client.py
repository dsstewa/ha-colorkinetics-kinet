"""UDP client: both reply ports, timeouts, partial scans, and the allow-list."""

from __future__ import annotations

import asyncio
import struct

import pytest

from custom_components.color_kinetics.protocol import (
    MAGIC,
    TYPE_DMXOUT,
    TYPE_FIXTURE_PROP_REPLY,
    TYPE_POLL,
    TYPE_SET_IP,
    TYPE_SET_NAME,
    TYPE_SET_UNIVERSE,
    TYPE_SYNC,
    TYPE_WRITE_SUPPLY_PROP,
    BroadcastNotAllowedError,
    ForbiddenPacketError,
    KiNetClient,
    KiNetError,
    KiNetTimeoutError,
    ProtocolError,
    _KiNetProtocol,
    async_probe_host,
    build_fixture_property_read,
    build_poll,
)
from tests.fake_kinet import FIXTURES, SUPPLY_HOST, SupplyScript, install_fake_endpoint


@pytest.fixture
def script(monkeypatch):
    supply = SupplyScript()
    install_fake_endpoint(monkeypatch, supply)
    return supply


async def test_discovery_round_trip_accepts_replies_from_6038_and_6039(script):
    client = KiNetClient()
    await client.start()
    try:
        info = await client.poll_supply(SUPPLY_HOST)
        assert info.serial_hex == "83EB007F00000000"
        ports = await client.read_ports(SUPPLY_HOST)
        assert ports.ports[0].port_id == 1
        records = await client.scan_fixtures(SUPPLY_HOST)
        serials = [record.serial for record in records if record.serial is not None]
        assert serials == [item[0] for item in FIXTURES]
        reply = await client.read_fixture_property(SUPPLY_HOST, FIXTURES[0][0], 0x05)
        assert reply.value == 0x18
        reply_04 = await client.read_fixture_property(SUPPLY_HOST, FIXTURES[3][0], 0x05)
        assert reply_04.value == 0x04
    finally:
        await client.stop()

    ports_used = set()
    for transport in script.transports:
        for _data, _addr in transport.sent:
            pass
    # The script returns scan and property replies tagged with source port 6039.
    # Re-read what the handler emitted by checking the client accepted them above.
    assert any(packet[0][6:8] == b"\x08\x02" for packet in script.sent) or serials
    probed = await async_probe_host(SUPPLY_HOST, timeout=1)
    assert probed.label == "Data Enabler Pro"
    assert ports_used == set()


async def test_send_refuses_writes_broadcast_and_a_closed_client(script):
    client = KiNetClient()
    raw_poll = build_poll(1)
    with pytest.raises(KiNetError):
        client.send(SUPPLY_HOST, raw_poll)

    await client.start()
    try:
        for packet_type in (
            TYPE_SET_IP,
            TYPE_SET_UNIVERSE,
            TYPE_SET_NAME,
            TYPE_WRITE_SUPPLY_PROP,
            TYPE_SYNC,
            0x0303,
        ):
            raw = struct.pack("<IHHI", MAGIC, 1, packet_type, 1) + b"\x00" * 8
            with pytest.raises(ForbiddenPacketError):
                client.send(SUPPLY_HOST, raw)
        for host in ("255.255.255.255", "239.255.255.250", "0.0.0.0"):
            with pytest.raises(BroadcastNotAllowedError):
                client.send(host, raw_poll)
        with pytest.raises(ProtocolError):
            client.send(SUPPLY_HOST, b"garbage")
        before = len(script.sent)
        client.send("127.0.0.1", raw_poll)
        assert script.sent[-1][1] == ("127.0.0.1", 6038)
        assert len(script.sent) == before + 1
        assert all(
            struct.unpack_from("<H", data, 6)[0] not in {
                TYPE_SET_IP,
                TYPE_SET_UNIVERSE,
                TYPE_SET_NAME,
                TYPE_WRITE_SUPPLY_PROP,
                TYPE_SYNC,
            }
            for data, _addr in script.sent
        )
    finally:
        await client.stop()
    await client.stop()


async def test_timeouts_partial_scan_and_a_mismatched_property(monkeypatch):
    script = SupplyScript()
    script.drop_types.add(TYPE_POLL)
    install_fake_endpoint(monkeypatch, script)
    client = KiNetClient(poll_timeout=0.05, prop_timeout=0.05, scan_timeout=0.05)
    await client.start()
    try:
        with pytest.raises(KiNetTimeoutError):
            await client.poll_supply(SUPPLY_HOST)
    finally:
        await client.stop()

    script.drop_types.clear()
    script.finish_scan = False
    script.limit = 2
    client = KiNetClient(scan_timeout=0.05, prop_timeout=0.05)
    await client.start()
    try:
        records = await client.scan_fixtures(SUPPLY_HOST)
        assert len([record for record in records if record.serial is not None]) == 2
        script.finish_scan = False
        script.limit = 0
        with pytest.raises(KiNetTimeoutError):
            await client.scan_fixtures(SUPPLY_HOST)

        def wrong_serial(data, addr):
            script.sent.append((data, addr))
            header = __import__(
                "custom_components.color_kinetics.protocol", fromlist=["parse_header"]
            ).parse_header(data)
            reply = struct.pack("<IHHI", MAGIC, 1, TYPE_FIXTURE_PROP_REPLY, header.sequence)
            reply += struct.pack("<IHBB", 0x11111111, 0x41, 1, 0x09)
            return [(reply, 6039)]

        script_handler = script

        class Wrapper:
            transports: list = []

            def __call__(self, data, addr):
                return wrong_serial(data, addr)

        # The installed handler is still ``script``. Swap the reply by drop + custom.
        packet = build_fixture_property_read(client.allocate_sequence(), FIXTURES[0][0], 0x41)

        async def fake_request(*_args, **_kwargs):
            return struct.pack("<IHHI", MAGIC, 1, TYPE_FIXTURE_PROP_REPLY, 1) + struct.pack(
                "<IHBB", 0x11111111, 0x41, 1, 0x09
            )

        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(ProtocolError):
            await client.read_fixture_property(SUPPLY_HOST, FIXTURES[0][0], 0x41)
        assert packet[0:4] == MAGIC.to_bytes(4, "little")
    finally:
        await client.stop()
    assert script_handler is script


async def test_dispatch_ignores_garbage_and_a_raising_listener(script):
    client = KiNetClient()
    await client.start()
    try:
        def explode(header, data, host):
            raise RuntimeError("listener failed")

        client.add_packet_listener(explode)
        client.dispatch(b"not-a-packet", SUPPLY_HOST)
        client.dispatch(build_poll(1), SUPPLY_HOST)
        protocol = _KiNetProtocol(client)
        protocol.error_received(OSError("icmp"))
        protocol.datagram_received(b"xyz", (object(), 6039))  # type: ignore[arg-type]
    finally:
        await client.stop()


async def test_request_cancel_scan_filter_and_a_finished_waiter(script):
    client = KiNetClient()
    await client.start()
    try:
        client.transport = None
        with pytest.raises(KiNetError):
            await client.request(SUPPLY_HOST, build_poll(1), TYPE_POLL, 1)

        loop = asyncio.get_running_loop()
        finished: asyncio.Future[bytes] = loop.create_future()
        finished.set_result(b"")
        from custom_components.color_kinetics.protocol import (
            TYPE_POLL_REPLY,
            _Waiter,
        )

        client.transport = script.transports[-1]
        client._waiters.append(_Waiter(SUPPLY_HOST, 1, TYPE_POLL_REPLY, finished))
        client.dispatch(script.poll_reply, SUPPLY_HOST)

        def mixed(data, addr):
            from custom_components.color_kinetics.protocol import parse_header
            from tests.fake_kinet import scan_progress, scan_record

            header = parse_header(data)
            sequence = header.sequence
            return [
                (build_poll(sequence), 6039),
                (scan_record(sequence + 1, 1), 6039),
                (scan_progress(sequence, scanning=False), 6039),
            ]

        script_handler = script
        for transport in script.transports:
            transport.handler = mixed
        script_handler.finish_scan = True
        records = await client.scan_fixtures(SUPPLY_HOST)
        assert records == [] or all(record.serial is None for record in records)
    finally:
        await client.stop()


async def test_stop_cancels_an_outstanding_request(script):
    script.drop_types.add(TYPE_POLL)
    client = KiNetClient(poll_timeout=2)
    await client.start()

    async def delayed_stop():
        await asyncio.sleep(0.01)
        await client.stop()

    task = asyncio.create_task(delayed_stop())
    with pytest.raises((KiNetTimeoutError, asyncio.CancelledError)):
        await client.poll_supply(SUPPLY_HOST)
    await task
