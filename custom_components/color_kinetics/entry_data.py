"""Read config-entry data and options without surprising fallbacks."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_HOSTS,
    CONF_MANUAL_FIXTURES,
    CONF_OUTPUT_PORT,
    CONF_PORTOUT_VERSION,
    CONF_PROTOCOL,
    CONF_SUPPLIES,
    DEFAULT_OUTPUT_PORT,
    DEFAULT_PORTOUT_VERSION,
    DEFAULT_PROTOCOL,
    PROTOCOL_DMXOUT,
    PROTOCOL_PORTOUT,
)


def hosts_of(entry: ConfigEntry) -> list[str]:
    """Return the supply addresses for this entry. Options win after setup."""
    hosts = entry.options.get(CONF_HOSTS) or entry.data.get(CONF_HOSTS) or []
    return [str(host) for host in hosts]


def supplies_of(entry: ConfigEntry) -> dict[str, dict[str, Any]]:
    """Return the last polled supply descriptions stored on the entry."""
    if CONF_SUPPLIES in entry.options:
        raw = entry.options[CONF_SUPPLIES]
    else:
        raw = entry.data.get(CONF_SUPPLIES, {})
    return {str(host): dict(info) for host, info in raw.items()}


def manual_text_of(entry: ConfigEntry) -> str:
    """Return the manual-fixture text from options."""
    return str(entry.options.get(CONF_MANUAL_FIXTURES) or "")


def output_settings_of(entry: ConfigEntry) -> tuple[str, int, int]:
    """Return protocol, PORTOUT header version, and output port.

    Invalid stored values fall back to PORTOUT, header version 1, port 1.
    """
    protocol = entry.options.get(CONF_PROTOCOL, DEFAULT_PROTOCOL)
    if protocol not in (PROTOCOL_PORTOUT, PROTOCOL_DMXOUT):
        protocol = DEFAULT_PROTOCOL
    try:
        version = int(entry.options.get(CONF_PORTOUT_VERSION, DEFAULT_PORTOUT_VERSION))
    except (TypeError, ValueError):
        version = DEFAULT_PORTOUT_VERSION
    if version not in (1, 2):
        version = DEFAULT_PORTOUT_VERSION
    try:
        port = int(entry.options.get(CONF_OUTPUT_PORT, DEFAULT_OUTPUT_PORT))
    except (TypeError, ValueError):
        port = DEFAULT_OUTPUT_PORT
    if not 1 <= port <= 16:
        port = DEFAULT_OUTPUT_PORT
    return str(protocol), version, port
