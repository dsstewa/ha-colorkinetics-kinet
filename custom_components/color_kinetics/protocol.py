"""KiNET encode, decode, and the UDP client.

Outgoing packets are limited to an allow-list: supply poll, port list,
supply property read, fixture scan, fixture property read, and light data
(v1 DMXOUT or v2 PORTOUT). Configuration writes are rejected before they
reach the socket. Light data is unicast only.

Byte layouts follow the QuickPlay Pro 2 capture (section 1a of the research
spec) and OLA's KiNetNodeTest vectors. Multi-byte fields are little-endian.
The IPv4 address and MAC in a poll reply are raw network-order bytes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
import logging
import struct
from typing import Any

from .properties import supply_serial_hex

_LOGGER = logging.getLogger(__name__)

MAGIC = 0x4ADC0104
KINET_PORT = 6038
HEADER_SIZE = 12
DMX_SLOTS = 512
PORTOUT_MIN_DATA = 24

# Wire types. Names match the capture, not Haukcode where they disagree.
TYPE_POLL = 0x0001
TYPE_POLL_REPLY = 0x0002
TYPE_SET_IP = 0x0003
TYPE_SET_UNIVERSE = 0x0005
TYPE_SET_NAME = 0x0006
TYPE_PORT_LIST = 0x000A
TYPE_PORT_LIST_REPLY = 0x000B
TYPE_DMXOUT = 0x0101
TYPE_WRITE_SUPPLY_PROP = 0x0103
TYPE_READ_SUPPLY_PROP = 0x0105
TYPE_SUPPLY_PROP_REPLY = 0x0106
TYPE_PORTOUT = 0x0108
TYPE_SYNC = 0x0109
TYPE_FIXTURE_PROP = 0x0203
TYPE_FIXTURE_PROP_REPLY = 0x0204
TYPE_FIXTURE_SCAN = 0x0207
TYPE_FIXTURE_SCAN_REPLY = 0x0208

# The only packet types this integration will put on the wire.
ALLOWED_OUTGOING_TYPES = frozenset(
    {
        TYPE_POLL,
        TYPE_PORT_LIST,
        TYPE_READ_SUPPLY_PROP,
        TYPE_DMXOUT,
        TYPE_PORTOUT,
        TYPE_FIXTURE_PROP,
        TYPE_FIXTURE_SCAN,
    }
)

# Known write / configure / broadcast-sync types. These must never be sent.
FORBIDDEN_OUTGOING_TYPES = frozenset(
    {
        TYPE_SET_IP,
        TYPE_SET_UNIVERSE,
        TYPE_SET_NAME,
        TYPE_WRITE_SUPPLY_PROP,
        TYPE_SYNC,
    }
)

# QuickPlay Pro 2 sent this 4-byte poll argument. Replay it unchanged.
POLL_ARGUMENT = bytes.fromhex("0affffff")

POLL_TIMEOUT = 3.0
PROP_TIMEOUT = 1.5
SCAN_TIMEOUT = 20.0

_LIMITED_BROADCAST = ipaddress.IPv4Address("255.255.255.255")

PacketListener = Callable[["Header", bytes, str], None]


class KiNetError(Exception):
    """Base error for the KiNET client."""


class ForbiddenPacketError(KiNetError):
    """A packet type is outside the read-and-light-data allow-list."""

    def __init__(self, packet_type: int) -> None:
        """Store the rejected type."""
        self.packet_type = packet_type
        super().__init__(
            f"Refusing to send KiNET type 0x{packet_type:04x}. "
            "Only supply poll, port list, property reads, fixture scan, "
            "and light data are allowed."
        )


class BroadcastNotAllowedError(KiNetError):
    """The destination is broadcast, multicast, or otherwise not unicast."""

    def __init__(self, host: str) -> None:
        """Store the rejected host."""
        self.host = host
        super().__init__(f"Refusing non-unicast KiNET destination {host}")


class InvalidHostError(KiNetError):
    """The host is not an IPv4 address."""

    def __init__(self, host: str) -> None:
        """Store the rejected host."""
        self.host = host
        super().__init__(f"Not an IPv4 address: {host}")


class KiNetTimeoutError(KiNetError):
    """A supply did not answer in time."""


class ProtocolError(KiNetError):
    """A datagram is not a KiNET packet this code understands."""


@dataclass(frozen=True)
class Header:
    """The 12-byte KiNET header."""

    version: int
    packet_type: int
    sequence: int


@dataclass(frozen=True)
class SupplyInfo:
    """A parsed supply-poll reply."""

    ip: str
    mac: str
    protocol_version: int
    serial: bytes
    serial_hex: str
    manufacturer: str
    model: str
    firmware_part: str
    firmware_version: str
    label: str
    details: str
    trailer: bytes

    @property
    def firmware(self) -> str:
        """Firmware part and version, as QuickPlay shows them."""
        parts = [part for part in (self.firmware_part, self.firmware_version) if part]
        return " ".join(parts)


@dataclass(frozen=True)
class PortInfo:
    """One output port from a port-list reply."""

    port_id: int
    port_type: int
    extra: bytes


@dataclass(frozen=True)
class PortList:
    """A parsed port-list reply."""

    ports: tuple[PortInfo, ...]


@dataclass(frozen=True)
class ScanRecord:
    """One fixture-scan reply.

    ``scanning`` is false on the terminal reply. ``serial`` is set only on
    fixture records (serial length 4).
    """

    port: int
    scanning: bool
    serial: int | None


@dataclass(frozen=True)
class FixtureProperty:
    """A parsed fixture property reply."""

    serial: int
    prop: int
    value: int
    status: int


@dataclass(frozen=True)
class SupplyProperty:
    """A parsed supply property reply."""

    prop: int
    flag: int
    value: bytes


@dataclass(frozen=True)
class LightData:
    """A parsed DMXOUT or PORTOUT packet."""

    version: int
    packet_type: int
    sequence: int
    port: int
    universe: int
    flags: int
    data: bytes


def assert_unicast_host(host: str) -> str:
    """Return ``host`` when it is an IPv4 unicast address.

    Limited broadcast, multicast, and 0.0.0.0 are rejected. So is any
    address whose last octet is 0 or 255: on a normal LAN those are the
    network and directed-broadcast addresses, and ``SO_BROADCAST`` is not
    required for ``sendto`` to accept them. Loopback is allowed so tests
    can bind locally. A /31 or /32 where .0 or .255 is a real host is not
    distinguishable without the mask, so those addresses are rejected too.
    """
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError as err:
        raise InvalidHostError(host) from err
    if not isinstance(address, ipaddress.IPv4Address):
        raise InvalidHostError(host)
    last_octet = int(address) & 0xFF
    if (
        address == _LIMITED_BROADCAST
        or address.is_multicast
        or address.is_unspecified
        or last_octet in (0, 255)
    ):
        raise BroadcastNotAllowedError(host)
    return str(address)


def assert_outgoing_allowed(packet_type: int) -> None:
    """Raise if ``packet_type`` must not be transmitted."""
    if packet_type not in ALLOWED_OUTGOING_TYPES:
        raise ForbiddenPacketError(packet_type)


def format_fixture_serial(serial: int) -> str:
    """Format a fixture serial as eight uppercase hex digits."""
    return f"{serial & 0xFFFFFFFF:08X}"


def _encode_header(packet_type: int, sequence: int, version: int = 1) -> bytes:
    assert_outgoing_allowed(packet_type)
    if version not in (1, 2):
        raise ProtocolError("KiNET header version must be 1 or 2")
    return struct.pack("<IHHI", MAGIC, version, packet_type, sequence & 0xFFFFFFFF)


def parse_header(data: bytes) -> Header:
    """Parse a KiNET header. Raises ProtocolError on a short or foreign datagram."""
    if len(data) < HEADER_SIZE:
        raise ProtocolError("KiNET datagram is shorter than the header")
    magic, version, packet_type, sequence = struct.unpack_from("<IHHI", data)
    if magic != MAGIC:
        raise ProtocolError("KiNET magic mismatch")
    return Header(version=version, packet_type=packet_type, sequence=sequence)


def packet_type_of(data: bytes) -> int:
    """Return the type field, or raise ProtocolError."""
    return parse_header(data).packet_type


def build_poll(sequence: int) -> bytes:
    """Build a 16-byte supply poll (type 0x0001), version 1."""
    return _encode_header(TYPE_POLL, sequence) + POLL_ARGUMENT


def build_port_list(sequence: int) -> bytes:
    """Build a 13-byte port-list request (type 0x000a), version 1."""
    return _encode_header(TYPE_PORT_LIST, sequence) + b"\x00"


def build_supply_property_read(sequence: int, prop: int) -> bytes:
    """Build a 16-byte supply property read (type 0x0105)."""
    return _encode_header(TYPE_READ_SUPPLY_PROP, sequence) + struct.pack(
        "<I", prop & 0xFFFFFFFF
    )


def build_fixture_scan(sequence: int) -> bytes:
    """Build a 16-byte fixture scan (type 0x0207), version 1."""
    return _encode_header(TYPE_FIXTURE_SCAN, sequence) + b"\x00\x00\x00\x00"


def build_fixture_property_read(sequence: int, serial: int, prop: int) -> bytes:
    """Build a 20-byte fixture property read (type 0x0203).

    The property id is a little-endian u32, matching QuickPlay (``41 00 00 00``).
    """
    return _encode_header(TYPE_FIXTURE_PROP, sequence) + struct.pack(
        "<II", serial & 0xFFFFFFFF, prop & 0xFFFFFFFF
    )


def build_dmxout(sequence: int, data: bytes) -> bytes:
    """Build a KiNET v1 DMXOUT packet. Matches OLA, including no padding."""
    if not data or len(data) > DMX_SLOTS:
        raise ProtocolError("DMXOUT channel data must be 1 to 512 bytes")
    # port, flags, timer, universe (don't care), start code.
    tail = struct.pack("<BBHIB", 0, 0, 0, 0xFFFFFFFF, 0)
    return _encode_header(TYPE_DMXOUT, sequence, version=1) + tail + bytes(data)


def build_portout(
    sequence: int,
    port: int,
    data: bytes,
    *,
    flags: int = 0,
    version: int = 1,
) -> bytes:
    """Build a KiNET v2 PORTOUT packet.

    ``version`` is the header version field: 1 writes ``01 00`` (OLA),
    2 writes ``02 00`` (Haukcode). Data is zero-padded to at least 24 bytes,
    matching OLA. ``port`` is the output connector (1-16 on a supply;
    0-255 is accepted so the OLA vector with port 255 can be reproduced).
    """
    if not 0 <= port <= 255:
        raise ProtocolError("PORTOUT port must be 0-255")
    if len(data) > DMX_SLOTS:
        raise ProtocolError("PORTOUT channel data must be at most 512 bytes")
    length = max(len(data), PORTOUT_MIN_DATA)
    padded = bytes(data) + bytes(length - len(data))
    tail = struct.pack("<IBBHHH", 0xFFFFFFFF, port, 0, flags, length, 0)
    return _encode_header(TYPE_PORTOUT, sequence, version=version) + tail + padded


def _nul_split(rest: bytes) -> tuple[str, str, bytes]:
    parts = rest.split(b"\x00", 2)
    details = parts[0].decode("utf-8", errors="replace") if parts else ""
    label = parts[1].decode("utf-8", errors="replace") if len(parts) > 1 else ""
    trailer = parts[2] if len(parts) > 2 else b""
    return details, label, trailer


def _detail_fields(details: str) -> dict[str, str]:
    """Parse ``M:`` / ``D:`` / ``#:`` lines. A ``V34`` line keeps the leading V."""
    fields: dict[str, str] = {}
    for line in details.splitlines():
        if not line:
            continue
        if len(line) > 2 and line[1] == ":":
            fields[line[0]] = line[2:]
        elif line.startswith("V") and "V" not in fields:
            fields["V"] = line
    return fields


def parse_poll_reply(data: bytes) -> SupplyInfo:
    """Parse a type 0x0002 supply poll reply."""
    header = parse_header(data)
    if header.packet_type != TYPE_POLL_REPLY:
        raise ProtocolError("expected a supply poll reply")
    if len(data) < 32:
        raise ProtocolError("supply poll reply is too short")
    ip = ".".join(str(byte) for byte in data[12:16])
    mac = ":".join(f"{byte:02x}" for byte in data[16:22])
    protocol_version = struct.unpack_from("<H", data, 22)[0]
    serial = bytes(data[24:32])
    details, label, trailer = _nul_split(data[32:])
    fields = _detail_fields(details)
    return SupplyInfo(
        ip=ip,
        mac=mac,
        protocol_version=protocol_version,
        serial=serial,
        serial_hex=supply_serial_hex(serial),
        manufacturer=fields.get("M", ""),
        model=fields.get("D", ""),
        firmware_part=fields.get("#", ""),
        firmware_version=fields.get("V", ""),
        label=label,
        details=details,
        trailer=trailer,
    )


def parse_port_list(data: bytes) -> PortList:
    """Parse a type 0x000b port-list reply."""
    header = parse_header(data)
    if header.packet_type != TYPE_PORT_LIST_REPLY:
        raise ProtocolError("expected a port-list reply")
    if len(data) < 16:
        raise ProtocolError("port-list reply is too short")
    count = struct.unpack_from("<I", data, 12)[0]
    ports: list[PortInfo] = []
    offset = 16
    for _ in range(min(count, 16)):
        if offset + 8 > len(data):
            break
        port_id = data[offset]
        port_type = struct.unpack_from("<I", data, offset + 1)[0]
        extra = bytes(data[offset + 5 : offset + 8])
        ports.append(PortInfo(port_id=port_id, port_type=port_type, extra=extra))
        offset += 8
    return PortList(ports=tuple(ports))


def parse_supply_property(data: bytes) -> SupplyProperty:
    """Parse a type 0x0106 supply property reply."""
    header = parse_header(data)
    if header.packet_type != TYPE_SUPPLY_PROP_REPLY:
        raise ProtocolError("expected a supply property reply")
    if len(data) < 20:
        raise ProtocolError("supply property reply is too short")
    prop, flag = struct.unpack_from("<HH", data, 12)
    return SupplyProperty(prop=prop, flag=flag, value=bytes(data[16:20]))


def parse_scan_reply(data: bytes) -> ScanRecord:
    """Parse a type 0x0208 fixture-scan reply (20 or 24 bytes)."""
    header = parse_header(data)
    if header.packet_type != TYPE_FIXTURE_SCAN_REPLY:
        raise ProtocolError("expected a fixture scan reply")
    if len(data) < 20:
        raise ProtocolError("fixture scan reply is too short")
    port = data[12]
    scanning = bool(data[13])
    serial: int | None = None
    if len(data) >= 24 and data[17] == 1 and data[18] == 4:
        serial = struct.unpack_from("<I", data, 20)[0]
    return ScanRecord(port=port, scanning=scanning, serial=serial)


def parse_fixture_property(data: bytes) -> FixtureProperty:
    """Parse a type 0x0204 fixture property reply.

    Layout after the header: serial u32, property u16, value u8, status u8.
    """
    header = parse_header(data)
    if header.packet_type != TYPE_FIXTURE_PROP_REPLY:
        raise ProtocolError("expected a fixture property reply")
    if len(data) < 20:
        raise ProtocolError("fixture property reply is too short")
    serial, prop, value, status = struct.unpack_from("<IHBB", data, 12)
    return FixtureProperty(serial=serial, prop=prop, value=value, status=status)


def parse_light_data(data: bytes) -> LightData:
    """Parse a DMXOUT or PORTOUT packet."""
    header = parse_header(data)
    if header.packet_type == TYPE_DMXOUT:
        if len(data) < 21:
            raise ProtocolError("DMXOUT packet is too short")
        port, _flags_byte, _timer, universe, _start = struct.unpack_from(
            "<BBHIB", data, 12
        )
        return LightData(
            version=header.version,
            packet_type=header.packet_type,
            sequence=header.sequence,
            port=port,
            universe=universe,
            flags=0,
            data=bytes(data[21:]),
        )
    if header.packet_type == TYPE_PORTOUT:
        if len(data) < 24:
            raise ProtocolError("PORTOUT packet is too short")
        universe, port, _pad, flags, length, _start = struct.unpack_from(
            "<IBBHHH", data, 12
        )
        return LightData(
            version=header.version,
            packet_type=header.packet_type,
            sequence=header.sequence,
            port=port,
            universe=universe,
            flags=flags,
            data=bytes(data[24 : 24 + length]),
        )
    raise ProtocolError("not a light-data packet")


# Every captured 0x0204 used status 0x09. Anything else is not a usable value.
PROPERTY_STATUS_OK = 0x09


def dmx_start_zero_based(high: int | None, low: int | None) -> int | None:
    """Combine fixture properties 0x40 and 0x41 into a 0-based DMX slot.

    Both bytes are required. A missing high byte is not treated as 0: that
    would aim a fixture at the wrong address when the read times out.
    """
    if high is None or low is None:
        return None
    return ((high & 0xFF) << 8) | (low & 0xFF)


def supply_entry_dict(info: SupplyInfo) -> dict[str, Any]:
    """JSON-ready supply description stored on the config entry."""
    return {
        "ip": info.ip,
        "mac": info.mac,
        "protocol_version": info.protocol_version,
        "serial_hex": info.serial_hex,
        "manufacturer": info.manufacturer,
        "model": info.model,
        "firmware": info.firmware,
        "label": info.label,
    }


class _KiNetProtocol(asyncio.DatagramProtocol):
    """Unconnected UDP protocol. Replies from ports 6038 and 6039 are both kept."""

    def __init__(self, client: KiNetClient) -> None:
        """Bind the protocol to its client."""
        self._client = client

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Remember the transport."""
        self._client.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        """Hand the datagram to the client. The source port is not filtered."""
        host = addr[0]
        if not isinstance(host, str):
            return
        self._client.dispatch(bytes(data), host)

    def error_received(self, exc: Exception) -> None:
        """Log an ICMP error without tearing the socket down."""
        _LOGGER.debug("KiNET socket error: %s", exc)


@dataclass
class _Waiter:
    host: str
    sequence: int
    reply_type: int
    future: asyncio.Future[bytes]


class KiNetClient:
    """Async KiNET endpoint. One unconnected datagram socket, unicast only."""

    def __init__(
        self,
        *,
        poll_timeout: float | None = None,
        prop_timeout: float | None = None,
        scan_timeout: float | None = None,
    ) -> None:
        """Create a client. Call ``start`` before sending."""
        self.poll_timeout = POLL_TIMEOUT if poll_timeout is None else poll_timeout
        self.prop_timeout = PROP_TIMEOUT if prop_timeout is None else prop_timeout
        self.scan_timeout = SCAN_TIMEOUT if scan_timeout is None else scan_timeout
        self.transport: asyncio.BaseTransport | None = None
        self._sequence = 1
        self._waiters: list[_Waiter] = []
        self._listeners: list[PacketListener] = []

    async def start(self) -> None:
        """Bind an ephemeral UDP port. Broadcast is left disabled."""
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _KiNetProtocol(self),
            local_addr=("0.0.0.0", 0),
            allow_broadcast=False,
        )
        self.transport = transport

    async def stop(self) -> None:
        """Close the socket and cancel outstanding replies."""
        transport = self.transport
        self.transport = None
        if transport is not None:
            transport.close()
        for waiter in self._waiters:
            if not waiter.future.done():
                waiter.future.cancel()
        self._waiters.clear()
        self._listeners.clear()

    def allocate_sequence(self) -> int:
        """Return the next sequence number. Replies echo it."""
        sequence = self._sequence
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        return sequence

    def send(self, host: str, packet: bytes) -> None:
        """Send one allow-listed unicast datagram to UDP 6038.

        This is the only transmit path. The type and the destination are
        checked here even if the packet was built elsewhere.
        """
        destination = assert_unicast_host(host)
        packet_type = packet_type_of(packet)
        assert_outgoing_allowed(packet_type)
        transport = self.transport
        if transport is None:
            raise KiNetError("KiNET client is not started")
        _LOGGER.debug(
            "KiNET tx type %#06x seq %s to %s:%s (%s bytes)",
            packet_type,
            parse_header(packet).sequence,
            destination,
            KINET_PORT,
            len(packet),
        )
        try:
            transport.sendto(packet, (destination, KINET_PORT))  # type: ignore[attr-defined]
        except OSError as err:
            raise KiNetError(f"KiNET send to {destination} failed: {err}") from err

    def add_packet_listener(self, listener: PacketListener) -> Callable[[], None]:
        """Register a callback invoked for every decoded reply."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def dispatch(self, data: bytes, host: str) -> None:
        """Match a reply to a waiter or a scan listener."""
        try:
            header = parse_header(data)
        except ProtocolError:
            _LOGGER.debug("Ignoring non-KiNET datagram from %s", host)
            return
        for listener in list(self._listeners):
            try:
                listener(header, data, host)
            except Exception:  # noqa: BLE001 - a listener must not kill the socket
                _LOGGER.exception("KiNET reply listener failed")
        for waiter in self._waiters:
            if waiter.future.done():
                continue
            if (
                waiter.host == host
                and waiter.sequence == header.sequence
                and waiter.reply_type == header.packet_type
            ):
                waiter.future.set_result(data)
                break

    async def request(
        self,
        host: str,
        packet: bytes,
        reply_type: int,
        timeout: float,
    ) -> bytes:
        """Send ``packet`` and wait for the echo of its sequence number."""
        header = parse_header(packet)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        waiter = _Waiter(host, header.sequence, reply_type, future)
        self._waiters.append(waiter)
        try:
            self.send(host, packet)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as err:
            raise KiNetTimeoutError(
                f"No KiNET reply type 0x{reply_type:04x} from {host}"
            ) from err
        finally:
            self._waiters = [item for item in self._waiters if item is not waiter]
            if not future.done():
                future.cancel()

    async def poll_supply(self, host: str) -> SupplyInfo:
        """Poll one supply and parse the 0x0002 reply."""
        sequence = self.allocate_sequence()
        payload = await self.request(
            host, build_poll(sequence), TYPE_POLL_REPLY, self.poll_timeout
        )
        return parse_poll_reply(payload)

    async def read_ports(self, host: str) -> PortList:
        """Ask a supply for its output ports."""
        sequence = self.allocate_sequence()
        payload = await self.request(
            host, build_port_list(sequence), TYPE_PORT_LIST_REPLY, self.poll_timeout
        )
        return parse_port_list(payload)

    async def read_supply_property(self, host: str, prop: int) -> SupplyProperty:
        """Read one supply property. Raises if the reply names a different one."""
        sequence = self.allocate_sequence()
        packet = build_supply_property_read(sequence, prop)
        payload = await self.request(
            host, packet, TYPE_SUPPLY_PROP_REPLY, self.prop_timeout
        )
        parsed = parse_supply_property(payload)
        if parsed.prop != (prop & 0xFFFF):
            raise ProtocolError("supply property reply did not match the request")
        return parsed

    async def read_fixture_property(
        self, host: str, serial: int, prop: int
    ) -> FixtureProperty:
        """Read one fixture property. Raises if the reply names a different one."""
        sequence = self.allocate_sequence()
        packet = build_fixture_property_read(sequence, serial, prop)
        payload = await self.request(
            host, packet, TYPE_FIXTURE_PROP_REPLY, self.prop_timeout
        )
        parsed = parse_fixture_property(payload)
        if parsed.serial != (serial & 0xFFFFFFFF) or parsed.prop != (prop & 0xFFFF):
            raise ProtocolError("fixture property reply did not match the request")
        return parsed

    async def scan_fixtures(self, host: str) -> list[ScanRecord]:
        """Run a fixture scan and collect 0x0208 records until done or timeout.

        Records arrive from UDP source port 6039. The source port is ignored;
        the host and the echoed sequence number identify the reply.
        """
        sequence = self.allocate_sequence()
        packet = build_fixture_scan(sequence)
        records: list[ScanRecord] = []
        done = asyncio.Event()

        def on_packet(header: Header, data: bytes, source_host: str) -> None:
            if source_host != host or header.sequence != sequence:
                return
            if header.packet_type != TYPE_FIXTURE_SCAN_REPLY:
                return
            record = parse_scan_reply(data)
            if record.serial is not None:
                records.append(record)
            if not record.scanning:
                done.set()

        remove = self.add_packet_listener(on_packet)
        try:
            self.send(host, packet)
            await asyncio.wait_for(done.wait(), self.scan_timeout)
        except TimeoutError:
            if not records:
                raise KiNetTimeoutError(f"Fixture scan of {host} timed out") from None
            _LOGGER.warning(
                "Fixture scan of %s ended after %s record(s) without a done reply",
                host,
                len(records),
            )
        finally:
            remove()
        return records


async def async_probe_host(host: str, *, timeout: float | None = None) -> SupplyInfo:
    """Open a short-lived socket, poll one supply, and close it."""
    client = KiNetClient(poll_timeout=timeout)
    await client.start()
    try:
        return await client.poll_supply(host)
    finally:
        await client.stop()
