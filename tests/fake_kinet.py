"""Scripted Data Enabler for tests.

Replies are built from the QuickPlay capture and the same layouts the
parsers accept. Nothing here opens a socket.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import struct
from pathlib import Path

from custom_components.color_kinetics.protocol import (
    MAGIC,
    TYPE_FIXTURE_PROP,
    TYPE_FIXTURE_SCAN,
    TYPE_POLL,
    TYPE_PORT_LIST,
    parse_header,
)

CAPTURE_PATH = Path(__file__).parent / "fixtures" / "kinet_packets.json"

# serial, 0-based DMX start, type code. Matches the capture and the spec table.
FIXTURES: tuple[tuple[int, int, int], ...] = (
    (0x880055BE, 0, 0x18),
    (0x88002CD9, 3, 0x18),
    (0x88005B93, 6, 0x18),
    (0x760054AC, 9, 0x04),
    (0x760054A3, 12, 0x04),
    (0x880041E9, 15, 0x18),
    (0x880041F1, 18, 0x18),
    (0x8800565C, 21, 0x18),
)

SUPPLY_HOST = "172.16.10.41"
SUPPLY_SERIAL = "83EB007F00000000"


def load_capture() -> list[dict]:
    """Return the decoded QuickPlay packets."""
    return json.loads(CAPTURE_PATH.read_text(encoding="utf-8"))


def with_sequence(packet: bytes, sequence: int) -> bytes:
    """Replace the echoed sequence number in a captured reply."""
    return packet[:8] + struct.pack("<I", sequence & 0xFFFFFFFF) + packet[12:]


def first_reply(packets: list[dict], packet_type: int) -> bytes:
    """First captured reply of a type from the Data Enabler."""
    for packet in packets:
        if packet["typ"] == packet_type and packet["src"].startswith(SUPPLY_HOST):
            return bytes.fromhex(packet["hex"])
    raise AssertionError(f"no captured reply of type {packet_type:#x}")


def scan_progress(sequence: int, *, scanning: bool, port: int = 1) -> bytes:
    """20-byte 0x0208 progress or done reply, version 2."""
    head = struct.pack("<IHHI", MAGIC, 2, 0x0208, sequence & 0xFFFFFFFF)
    flag = 1 if scanning else 0
    return head + bytes((port, flag, 0, 0, 1, 0, 0, 0))


def scan_record(sequence: int, serial: int, *, port: int = 1) -> bytes:
    """24-byte 0x0208 fixture record, version 2."""
    head = struct.pack("<IHHI", MAGIC, 2, 0x0208, sequence & 0xFFFFFFFF)
    body = bytes((port, 1, 0, 0, 1, 1, 4, 0)) + struct.pack("<I", serial & 0xFFFFFFFF)
    return head + body


def fixture_property_reply(
    sequence: int,
    serial: int,
    prop: int,
    value: int,
    *,
    status: int = 0x09,
) -> bytes:
    """20-byte 0x0204 fixture property reply."""
    return struct.pack(
        "<IHHI", MAGIC, 1, 0x0204, sequence & 0xFFFFFFFF
    ) + struct.pack("<IHBB", serial & 0xFFFFFFFF, prop & 0xFFFF, value & 0xFF, status & 0xFF)


class FakeTransport:
    """Datagram transport that hands replies straight to the protocol."""

    def __init__(
        self,
        protocol: asyncio.DatagramProtocol,
        handler: Callable[[bytes, tuple], list] | None,
    ) -> None:
        self.protocol = protocol
        self.handler = handler
        self.sent: list[tuple[bytes, tuple]] = []
        self.closed = False

    def sendto(self, data: bytes, addr: tuple | None = None) -> None:
        payload = bytes(data)
        destination = addr if addr is not None else ("127.0.0.1", 6038)
        self.sent.append((payload, destination))
        if self.handler is None or self.protocol is None:
            return
        replies = self.handler(payload, destination) or []
        host = destination[0]
        for item in replies:
            if isinstance(item, tuple):
                reply, port = item
            else:
                reply, port = item, 6038
            self.protocol.datagram_received(reply, (host, port))

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.close()

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str, default=None):
        return default


class SupplyScript:
    """Answer a KiNET client the way the captured Data Enabler did."""

    def __init__(
        self,
        fixtures: tuple[tuple[int, int, int], ...] | list[tuple[int, int, int]] | None = None,
    ) -> None:
        packets = load_capture()
        self.poll_reply = first_reply(packets, 0x0002)
        self.port_reply = first_reply(packets, 0x000B)
        self.fixtures = list(FIXTURES if fixtures is None else fixtures)
        self.drop_types: set[int] = set()
        self.skip_props: dict[int, set[int]] = {}
        self.status_overrides: dict[tuple[int, int], int] = {}
        self.limit: int | None = None
        self.finish_scan = True
        self.sent: list[tuple[bytes, tuple]] = []
        self.reply_ports: list[int] = []
        self.transports: list[FakeTransport] = []

    def prop_value(self, serial: int, prop: int) -> int | None:
        for known, dmx, type_code in self.fixtures:
            if known != serial:
                continue
            if prop in self.skip_props.get(serial, set()):
                return None
            if prop == 0x40:
                return (dmx >> 8) & 0xFF
            if prop == 0x41:
                return dmx & 0xFF
            if prop == 0x05:
                return type_code
        return None

    def __call__(self, data: bytes, addr: tuple) -> list[tuple[bytes, int]]:
        self.sent.append((data, addr))
        replies = self._replies(data)
        for _reply, port in replies:
            self.reply_ports.append(port)
        return replies

    def _replies(self, data: bytes) -> list[tuple[bytes, int]]:
        header = parse_header(data)
        if header.packet_type in self.drop_types:
            return []
        sequence = header.sequence
        if header.packet_type == TYPE_POLL:
            return [(with_sequence(self.poll_reply, sequence), 6038)]
        if header.packet_type == TYPE_PORT_LIST:
            return [(with_sequence(self.port_reply, sequence), 6038)]
        if header.packet_type == TYPE_FIXTURE_SCAN:
            return self._scan_replies(sequence)
        if header.packet_type == TYPE_FIXTURE_PROP:
            serial, prop = struct.unpack_from("<II", data, 12)
            value = self.prop_value(serial, prop)
            if value is None:
                return []
            status = self.status_overrides.get((serial, prop), 0x09)
            return [
                (fixture_property_reply(sequence, serial, prop, value, status=status), 6039)
            ]
        return []

    def _scan_replies(self, sequence: int) -> list[tuple[bytes, int]]:
        chosen = self.fixtures if self.limit is None else self.fixtures[: self.limit]
        replies: list[tuple[bytes, int]] = [(scan_progress(sequence, scanning=True), 6039)]
        for serial, _dmx, _type_code in chosen:
            replies.append((scan_record(sequence, serial), 6039))
        if self.finish_scan:
            replies.append((scan_progress(sequence, scanning=False), 6039))
        return replies


def install_fake_endpoint(monkeypatch, handler) -> list[FakeTransport]:
    """Replace asyncio datagram endpoints with ``handler`` replies."""
    transports: list[FakeTransport] = []

    async def _endpoint(self, protocol_factory, **kwargs):
        if kwargs.get("allow_broadcast"):
            raise AssertionError("KiNET socket must not enable broadcast")
        protocol = protocol_factory()
        transport = FakeTransport(protocol, handler)
        transports.append(transport)
        if hasattr(handler, "transports"):
            handler.transports.append(transport)
        protocol.connection_made(transport)
        return transport, protocol

    monkeypatch.setattr(asyncio.BaseEventLoop, "create_datagram_endpoint", _endpoint)
    return transports
