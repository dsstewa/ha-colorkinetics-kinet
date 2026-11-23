"""Runtime for one Color Kinetics config entry.

One UDP client, one frame buffer per Data Enabler, and a fixture list keyed
by serial. Discovery runs at startup only when that supply has never been
scanned, and again when the user presses Rescan. It is not polled.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import (
    CHANNELS_PER_FIXTURE,
    DOMAIN,
    KEEPALIVE_SECONDS,
    MANUFACTURER,
    MAX_FPS,
)
from .entry_data import (
    hosts_of,
    manual_text_of,
    output_settings_of,
    supplies_of,
)
from .manual import ManualFixture, ManualFixtureError, parse_manual_fixtures
from .protocol import (
    DMX_SLOTS,
    KiNetClient,
    KiNetError,
    KiNetTimeoutError,
    SupplyInfo,
    build_dmxout,
    build_portout,
    dmx_start_zero_based,
    format_fixture_serial,
    supply_entry_dict,
)
from .pump import FramePump

_LOGGER = logging.getLogger(__name__)

_FIXTURE_PROPS = (0x40, 0x41, 0x05)
_STORAGE_VERSION = 1

Listener = Any


class ScanInProgressError(KiNetError):
    """A fixture scan is already running on this supply."""

    def __init__(self, host: str) -> None:
        """Store the supply address."""
        self.host = host
        super().__init__(f"A fixture scan is already running on {host}.")


@dataclass(frozen=True)
class Fixture:
    """One fixture, discovered or entered by hand."""

    key: str
    host: str
    serial_hex: str | None
    label: str | None
    dmx_start: int | None
    type_code: int | None
    present: bool
    source: str
    properties: dict[int, int]
    miss_count: int = 0

    @property
    def display_name(self) -> str:
        """Default device name. A user rename is stored on the device."""
        if self.label:
            return self.label
        if self.serial_hex:
            return f"Color Kinetics {self.serial_hex}"
        return "Color Kinetics fixture"

    @property
    def model(self) -> str:
        """Expose the fixture type code. The two captured types differ."""
        if self.type_code is None:
            return "Color Kinetics fixture"
        return f"Type 0x{self.type_code:02X}"


def _fixture_from_dict(raw: dict[str, Any]) -> Fixture:
    properties = {int(key): int(value) for key, value in raw.get("properties", {}).items()}
    return Fixture(
        key=str(raw["key"]),
        host=str(raw["host"]),
        serial_hex=raw.get("serial_hex"),
        label=raw.get("label"),
        dmx_start=raw.get("dmx_start"),
        type_code=raw.get("type_code"),
        present=bool(raw.get("present", True)),
        source=str(raw.get("source") or "discovery"),
        properties=properties,
        miss_count=int(raw.get("miss_count") or 0),
    )


def _fixture_to_dict(fixture: Fixture) -> dict[str, Any]:
    source = "discovery" if fixture.source == "both" else fixture.source
    return {
        "key": fixture.key,
        "host": fixture.host,
        "serial_hex": fixture.serial_hex,
        "label": fixture.label,
        "dmx_start": fixture.dmx_start,
        "type_code": fixture.type_code,
        "present": fixture.present,
        "source": source,
        "miss_count": fixture.miss_count,
        "properties": {str(key): value for key, value in fixture.properties.items()},
    }


class ColorKineticsHub:
    """Holds the socket, the fixture list, and the light-data buffers."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Create a hub. ``async_start`` opens the socket."""
        self.hass = hass
        self.entry = entry
        self.client = KiNetClient()
        self.pump = FramePump(
            min_interval=1 / MAX_FPS,
            keepalive=KEEPALIVE_SECONDS,
            send=self._transmit,
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, _STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}"
        )
        self._fixtures: dict[str, Fixture] = {}
        self._supplies: dict[str, dict[str, Any]] = {}
        self._ports: dict[str, list[dict[str, int]]] = {}
        self._scanned_hosts: set[str] = set()
        self._buffers: dict[str, bytearray] = {}
        self._span: dict[str, int] = {}
        self._listeners: list[Listener] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._supply_device_ids: dict[str, str] = {}
        self._stopped = False
        self.initial_task: asyncio.Task[None] | None = None
        self.last_error: Exception | None = None

    @property
    def hosts(self) -> list[str]:
        """Configured Data Enabler addresses."""
        return hosts_of(self.entry)

    def fixtures(self) -> list[Fixture]:
        """Fixtures in DMX order."""
        return sorted(
            self._fixtures.values(),
            key=lambda fixture: (
                fixture.host,
                fixture.dmx_start if fixture.dmx_start is not None else 10_000,
                fixture.key,
            ),
        )

    def get_fixture(self, key: str) -> Fixture | None:
        """Return one fixture by its stable key."""
        return self._fixtures.get(key)

    def is_available(self, fixture: Fixture) -> bool:
        """A fixture is controllable when it has an address and was seen or entered."""
        return (
            fixture.host in self.hosts
            and fixture.present
            and fixture.dmx_start is not None
        )

    def supply_identifier(self, host: str) -> str:
        """Stable device-registry id for a supply. Serial wins over the IP."""
        info = self._supplies.get(host) or {}
        serial = info.get("serial_hex")
        if serial:
            return str(serial)
        return f"host:{host}"

    def supply_device_id(self, host: str) -> str | None:
        """Device-registry id of the supply, once it has been registered."""
        return self._supply_device_ids.get(host)

    def supply_name(self, host: str) -> str:
        """Default supply device name, including the IP so two units differ."""
        info = self._supplies.get(host) or {}
        label = info.get("label") or info.get("model") or "Data Enabler"
        return f"{label} ({host})"

    def button_unique_id(self, host: str) -> str:
        """Unique id of the rescan button for one supply."""
        return f"{self.supply_identifier(host)}:rescan"

    def add_listener(self, listener: Listener) -> Any:
        """Subscribe to fixture-list changes. Returns an unsubscribe callback."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def async_start(self) -> None:
        """Open the socket, load the cache, and start the frame pump."""
        self._supplies = supplies_of(self.entry)
        await self.client.start()
        stored = await self._store.async_load()
        self._load_cache(stored or {})
        self._merge_manual()
        self._register_supply_devices()
        self._drop_stale_registry_entries()
        self.pump.start()

    async def async_stop(self) -> None:
        """Stop sending, close the socket, and drop repair issues. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        self._clear_issues()
        await self.pump.stop()
        await self._save()
        await self.client.stop()

    async def async_initial_refresh(self) -> None:
        """Poll each supply. Scan only a supply that has never completed a scan."""
        try:
            for host in list(self.hosts):
                try:
                    await self._poll(host)
                except (KiNetError, OSError) as err:
                    _LOGGER.warning("Data Enabler %s did not answer: %s", host, err)
                    self._create_issue("supply_unreachable", host)
                    continue
                self._delete_issue("supply_unreachable", host)
                if host in self._scanned_hosts:
                    continue
                try:
                    await self.async_rescan(host)
                except ScanInProgressError:
                    _LOGGER.debug("Scan already running on %s", host)
                except (KiNetError, OSError) as err:
                    _LOGGER.warning("Fixture scan failed for %s: %s", host, err)
                    self._create_issue("scan_failed", host)
        except Exception as err:  # noqa: BLE001 - keep the background task quiet
            self.last_error = err
            _LOGGER.exception("Color Kinetics refresh failed")

    async def async_rescan(self, host: str) -> None:
        """Poll, list ports, scan fixtures, and read DMX address and type.

        Raises ScanInProgressError if a scan is already running. Other
        KiNetError failures leave the previous fixture list in place.
        """
        if host not in self.hosts:
            raise KiNetError(f"{host} is not a configured Data Enabler")
        lock = self._locks.setdefault(host, asyncio.Lock())
        if lock.locked():
            raise ScanInProgressError(host)
        async with lock:
            await self._poll(host)
            self._delete_issue("supply_unreachable", host)
            ports = await self.client.read_ports(host)
            self._ports[host] = [
                {"id": port.port_id, "type": port.port_type} for port in ports.ports
            ]
            records = await self.client.scan_fixtures(host)
            serials: list[int] = []
            seen: set[int] = set()
            for record in records:
                if record.serial is None or record.serial in seen:
                    continue
                seen.add(record.serial)
                serials.append(record.serial)
            properties: dict[int, dict[int, int]] = {}
            for serial in serials:
                properties[serial] = await self._read_fixture_properties(host, serial)
            self._apply_scan(host, serials, properties)
            self._scanned_hosts.add(host)
            self._delete_issue("scan_failed", host)
            await self._save()

    async def _read_fixture_properties(self, host: str, serial: int) -> dict[int, int]:
        found: dict[int, int] = {}
        for prop in _FIXTURE_PROPS:
            try:
                reply = await self.client.read_fixture_property(host, serial, prop)
            except KiNetTimeoutError:
                _LOGGER.debug(
                    "Fixture %s did not answer property %#04x",
                    format_fixture_serial(serial),
                    prop,
                )
                continue
            if reply.status != 0x09:
                _LOGGER.debug(
                    "Fixture %s property %#04x status %#04x is not usable",
                    format_fixture_serial(serial),
                    prop,
                    reply.status,
                )
                continue
            found[prop] = reply.value
        return found

    async def _poll(self, host: str) -> SupplyInfo:
        info = await self.client.poll_supply(host)
        self._supplies[host] = supply_entry_dict(info)
        self._register_supply_devices()
        _LOGGER.info(
            "Data Enabler %s %s firmware %s",
            host,
            info.model or info.label,
            info.firmware,
        )
        return info

    def _apply_scan(
        self,
        host: str,
        serials: list[int],
        properties: dict[int, dict[int, int]],
    ) -> None:
        """Apply one scan. A fixture is dropped only after two scans miss it.

        The frame buffer is not cleared. A fixture that is still present
        rewrites its own slots; one that is merely missing keeps the bytes
        it last sent, so a lost reply cannot black out the run.
        """
        seen: set[str] = set()
        for serial in serials:
            key = format_fixture_serial(serial)
            seen.add(key)
            props = properties.get(serial, {})
            previous = self._fixtures.get(key)
            self._fixtures[key] = Fixture(
                key=key,
                host=host,
                serial_hex=key,
                label=previous.label if previous else None,
                dmx_start=dmx_start_zero_based(props.get(0x40), props.get(0x41)),
                type_code=props.get(0x05),
                present=True,
                source="discovery",
                properties=dict(props),
                miss_count=0,
            )
        for key, fixture in list(self._fixtures.items()):
            if fixture.host != host or fixture.source == "manual" or key in seen:
                continue
            if fixture.serial_hex is None:
                continue
            misses = fixture.miss_count + 1
            self._fixtures[key] = replace(
                fixture,
                miss_count=misses,
                present=misses < 2,
            )
        self._merge_manual()
        self._register_fixture_devices()
        self._notify()
        if self._fixture_span(host):
            self.pump.mark_dirty(host)
        _LOGGER.info("Fixture scan of %s found %s fixture(s)", host, len(seen))

    def _merge_manual(self) -> None:
        """Overlay the options-flow fixture list onto the discovery cache."""
        self._fixtures = {
            key: fixture
            for key, fixture in self._fixtures.items()
            if fixture.source != "manual"
        }
        for key, fixture in list(self._fixtures.items()):
            if fixture.source == "both":
                self._fixtures[key] = replace(fixture, source="discovery")
        try:
            manuals = parse_manual_fixtures(manual_text_of(self.entry), self.hosts)
        except ManualFixtureError:
            _LOGGER.warning("Ignoring invalid manual fixture list")
            return
        for manual in manuals:
            self._apply_manual(manual)

    def _apply_manual(self, manual: ManualFixture) -> None:
        current = self._fixtures.get(manual.key)
        if current is not None and current.serial_hex:
            if current.present and current.dmx_start is not None:
                if current.dmx_start != manual.dmx_start:
                    _LOGGER.warning(
                        "Manual DMX %s for %s ignored; discovery reported %s",
                        manual.dmx_start + 1,
                        manual.key,
                        current.dmx_start + 1,
                    )
                dmx_start = current.dmx_start
            else:
                dmx_start = manual.dmx_start
            self._fixtures[manual.key] = replace(
                current,
                label=manual.label or current.label,
                dmx_start=dmx_start,
                present=True,
                source="both",
                host=current.host if current.present else manual.host,
            )
            return
        self._fixtures[manual.key] = Fixture(
            key=manual.key,
            host=manual.host,
            serial_hex=manual.serial_hex,
            label=manual.label,
            dmx_start=manual.dmx_start,
            type_code=None,
            present=True,
            source="manual",
            properties={},
        )

    def write_channels(self, host: str, start: int, data: bytes) -> None:
        """Write channel bytes into the supply buffer and schedule a send."""
        if host not in self.hosts:
            return
        end = start + len(data)
        if start < 0 or end > DMX_SLOTS:
            _LOGGER.warning(
                "DMX channels %s-%s on %s are outside 1-512", start + 1, end, host
            )
            return
        buffer = self._buffers.setdefault(host, bytearray(DMX_SLOTS))
        buffer[start:end] = data
        self._span[host] = max(self._span.get(host, 0), end)
        self.pump.mark_dirty(host)

    def _fixture_span(self, host: str) -> int:
        end = 0
        for fixture in self._fixtures.values():
            if fixture.host != host or fixture.dmx_start is None:
                continue
            end = max(end, fixture.dmx_start + CHANNELS_PER_FIXTURE)
        return end

    def _transmit(self, host: str) -> bool:
        if host not in self.hosts:
            self.pump.forget(host)
            return False
        span = max(self._span.get(host, 0), self._fixture_span(host))
        if span <= 0:
            return False
        buffer = self._buffers.setdefault(host, bytearray(DMX_SLOTS))
        data = bytes(buffer[:span])
        protocol, version, port = output_settings_of(self.entry)
        sequence = self.client.allocate_sequence()
        if protocol == "dmxout":
            packet = build_dmxout(sequence, data)
        else:
            packet = build_portout(sequence, port, data, version=version)
        self.client.send(host, packet)
        return True

    def _load_cache(self, stored: dict[str, Any]) -> None:
        for host, info in stored.get("supplies", {}).items():
            self._supplies.setdefault(str(host), dict(info))
        self._scanned_hosts = {str(host) for host in stored.get("scanned_hosts", [])}
        self._ports = {
            str(host): list(ports) for host, ports in stored.get("ports", {}).items()
        }
        for key, raw in stored.get("fixtures", {}).items():
            try:
                fixture = _fixture_from_dict(raw)
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Skipping stored fixture %s", key)
                continue
            if fixture.source == "manual":
                continue
            self._fixtures[fixture.key] = fixture

    def _cache(self) -> dict[str, Any]:
        fixtures = {
            fixture.key: _fixture_to_dict(fixture)
            for fixture in self._fixtures.values()
            if fixture.source != "manual"
        }
        return {
            "supplies": self._supplies,
            "scanned_hosts": sorted(self._scanned_hosts),
            "ports": self._ports,
            "fixtures": fixtures,
        }

    async def _save(self) -> None:
        await self._store.async_save(self._cache())

    def _register_supply_devices(self) -> None:
        registry = dr.async_get(self.hass)
        hosts = set(self.hosts)
        hosts.update(fixture.host for fixture in self._fixtures.values())
        for host in hosts:
            info = self._supplies.get(host) or {}
            identifier = self.supply_identifier(host)
            connections: set[tuple[str, str]] | None = None
            if mac := info.get("mac"):
                connections = {(dr.CONNECTION_NETWORK_MAC, str(mac))}
            kwargs: dict[str, Any] = {
                "config_entry_id": self.entry.entry_id,
                "identifiers": {(DOMAIN, identifier)},
                "name": self.supply_name(host),
                "manufacturer": info.get("manufacturer") or MANUFACTURER,
                "model": info.get("model") or "Data Enabler",
                "sw_version": info.get("firmware") or None,
                "serial_number": info.get("serial_hex"),
            }
            if connections:
                kwargs["connections"] = connections
            device = registry.async_get_or_create(**kwargs)
            self._supply_device_ids[host] = device.id

    def _register_fixture_devices(self) -> None:
        registry = dr.async_get(self.hass)
        for fixture in self._fixtures.values():
            kwargs: dict[str, Any] = {
                "config_entry_id": self.entry.entry_id,
                "identifiers": {(DOMAIN, fixture.key)},
                "name": fixture.display_name,
                "manufacturer": MANUFACTURER,
                "model": fixture.model,
                "serial_number": fixture.serial_hex,
            }
            via = self._supply_device_ids.get(fixture.host)
            if via:
                kwargs["via_device_id"] = via
            registry.async_get_or_create(**kwargs)

    def _drop_stale_registry_entries(self) -> None:
        keep_entities = {fixture.key for fixture in self._fixtures.values()}
        keep_entities.update(self.button_unique_id(host) for host in self.hosts)
        entity_registry = er.async_get(self.hass)
        for entity in er.async_entries_for_config_entry(
            entity_registry, self.entry.entry_id
        ):
            if entity.unique_id not in keep_entities:
                entity_registry.async_remove(entity.entity_id)
        keep_devices = {fixture.key for fixture in self._fixtures.values()}
        keep_devices.update(self.supply_identifier(host) for host in self.hosts)
        keep_devices.update(
            self.supply_identifier(fixture.host) for fixture in self._fixtures.values()
        )
        device_registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(
            device_registry, self.entry.entry_id
        ):
            identifiers = {
                identifier
                for domain, identifier in device.identifiers
                if domain == DOMAIN
            }
            if identifiers and identifiers.isdisjoint(keep_devices):
                device_registry.async_remove_device(device.id)

    def _clear_issues(self) -> None:
        """Remove repair issues for every supply this entry has talked to."""
        hosts = set(self.hosts)
        hosts.update(self._supplies)
        for host in hosts:
            self._delete_issue("supply_unreachable", host)
            self._delete_issue("scan_failed", host)

    def _issue_id(self, kind: str, host: str) -> str:
        return f"{kind}_{host.replace('.', '_')}"

    def _create_issue(self, kind: str, host: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id(kind, host),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=kind,
            translation_placeholders={"host": host},
        )

    def _delete_issue(self, kind: str, host: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(kind, host))


type ColorKineticsConfigEntry = ConfigEntry[ColorKineticsHub]
