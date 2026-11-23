"""Parse the options-flow host list and manual-fixture fallback."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .protocol import (
    BroadcastNotAllowedError,
    InvalidHostError,
    assert_unicast_host,
    format_fixture_serial,
)

_HEX_SERIAL = re.compile(r"(?:0x)?([0-9A-Fa-f]{8})\Z")


class HostListError(Exception):
    """The host text cannot be used. ``code`` is a config-flow error key."""

    def __init__(self, code: str) -> None:
        """Store the error key."""
        self.code = code
        super().__init__(code)


class ManualFixtureError(Exception):
    """One manual-fixture line is invalid. ``detail`` is safe to show."""

    def __init__(self, detail: str) -> None:
        """Store the message."""
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class ManualFixture:
    """One user-entered fixture."""

    host: str
    serial_hex: str | None
    label: str | None
    dmx_start: int
    key: str


def parse_host_list(text: str) -> list[str]:
    """Parse IPv4 unicast addresses separated by newlines or commas."""
    hosts: list[str] = []
    raw = text.replace(",", "\n")
    for line in raw.splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        try:
            host = assert_unicast_host(item)
        except (InvalidHostError, BroadcastNotAllowedError) as err:
            raise HostListError("invalid_host") from err
        if host not in hosts:
            hosts.append(host)
    if not hosts:
        raise HostListError("no_hosts")
    return hosts


def _key_for(host: str, serial_hex: str | None, label: str | None) -> str:
    if serial_hex:
        return serial_hex
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    if not slug:
        raise ManualFixtureError("A fixture needs a serial or a label.")
    return f"manual:{host}:{slug}"


def _parse_identity(token: str) -> tuple[str | None, str | None]:
    match = _HEX_SERIAL.fullmatch(token.strip())
    if match:
        return format_fixture_serial(int(match.group(1), 16)), None
    label = token.strip()
    if not label:
        raise ManualFixtureError("A fixture needs a serial or a label.")
    return None, label


def parse_manual_fixtures(text: str, hosts: list[str]) -> list[ManualFixture]:
    """Parse manual fixtures.

    With one supply, a line is ``serial_or_label, dmx_start[, label]``.
    The supply IP may always be written first:
    ``host, serial_or_label, dmx_start[, label]``.
    DMX start is 1-based, the number QuickPlay shows. An 8-digit hex token
    is a serial; anything else is a label used as the stable entity key.
    """
    if not text or not text.strip():
        return []
    if not hosts:
        raise ManualFixtureError("Add a Data Enabler before manual fixtures.")
    fixtures: list[ManualFixture] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",")]
        try:
            host, ident, dmx_text, label = _split_fields(parts, hosts)
            serial_hex, identity_label = _parse_identity(ident)
            chosen_label = label or identity_label
            dmx_one_based = int(dmx_text, 10)
        except ManualFixtureError as err:
            raise ManualFixtureError(f"Line {lineno}: {err.detail}") from err
        except ValueError as err:
            raise ManualFixtureError(
                f"Line {lineno}: DMX start must be a whole number."
            ) from err
        if host not in hosts:
            raise ManualFixtureError(
                f"Line {lineno}: {host} is not one of the Data Enabler addresses."
            )
        if not 1 <= dmx_one_based <= 510:
            raise ManualFixtureError(
                f"Line {lineno}: DMX start must be from 1 to 510 "
                "so three RGB channels fit in a universe."
            )
        try:
            key = _key_for(host, serial_hex, chosen_label)
        except ManualFixtureError as err:
            raise ManualFixtureError(f"Line {lineno}: {err.detail}") from err
        if key in seen:
            raise ManualFixtureError(f"Line {lineno}: duplicate fixture {key}.")
        seen.add(key)
        fixtures.append(
            ManualFixture(
                host=host,
                serial_hex=serial_hex,
                label=chosen_label,
                dmx_start=dmx_one_based - 1,
                key=key,
            )
        )
    return fixtures


def _split_fields(
    parts: list[str], hosts: list[str]
) -> tuple[str, str, str, str | None]:
    """Return host, identity, dmx text, optional label."""
    if len(parts) >= 3 and _looks_like_ip(parts[0]):
        if len(parts) not in (3, 4):
            raise ManualFixtureError(
                "Use host, serial_or_label, dmx_start, and an optional label."
            )
        label = parts[3] if len(parts) == 4 and parts[3] else None
        return parts[0], parts[1], parts[2], label
    if len(hosts) == 1 and len(parts) in (2, 3):
        label = parts[2] if len(parts) == 3 and parts[2] else None
        return hosts[0], parts[0], parts[1], label
    if len(hosts) > 1:
        raise ManualFixtureError(
            "With more than one Data Enabler, start the line with its IP address."
        )
    raise ManualFixtureError(
        "Use serial_or_label, dmx_start, and an optional label."
    )


def _looks_like_ip(token: str) -> bool:
    try:
        assert_unicast_host(token)
    except (InvalidHostError, BroadcastNotAllowedError):
        return False
    return True
