"""Config and options flow for Philips Color Kinetics."""

from __future__ import annotations

import logging
from typing import Any

import probatio
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

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
    DOMAIN,
    PROTOCOL_DMXOUT,
    PROTOCOL_PORTOUT,
)
from .entry_data import hosts_of, manual_text_of, supplies_of
from .manual import HostListError, ManualFixtureError, parse_host_list, parse_manual_fixtures
from .protocol import KiNetError, SupplyInfo, async_probe_host, supply_entry_dict

_LOGGER = logging.getLogger(__name__)

_HOST_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(multiline=True)
)
_MANUAL_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(multiline=True)
)
_PROTOCOL_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[PROTOCOL_PORTOUT, PROTOCOL_DMXOUT],
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key="protocol",
    )
)
_VERSION_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=["1", "2"],
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key="portout_version",
    )
)
_PORT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=1,
        max=16,
        step=1,
        mode=selector.NumberSelectorMode.BOX,
    )
)

USER_SCHEMA = probatio.Schema(
    {probatio.Required(CONF_HOSTS): _HOST_SELECTOR}
)
OPTIONS_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOSTS): _HOST_SELECTOR,
        probatio.Required(CONF_PROTOCOL): _PROTOCOL_SELECTOR,
        probatio.Required(CONF_PORTOUT_VERSION): _VERSION_SELECTOR,
        probatio.Required(CONF_OUTPUT_PORT): _PORT_SELECTOR,
        probatio.Required(CONF_MANUAL_FIXTURES): _MANUAL_SELECTOR,
    }
)


def _configured_hosts(hass: Any, *, skip_entry_id: str | None = None) -> set[str]:
    hosts: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == skip_entry_id:
            continue
        hosts.update(hosts_of(entry))
    return hosts


def _configured_serials(hass: Any, *, skip_entry_id: str | None = None) -> set[str]:
    serials: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == skip_entry_id:
            continue
        for info in supplies_of(entry).values():
            if serial := info.get("serial_hex"):
                serials.add(str(serial))
    return serials


def _title(supplies: dict[str, dict[str, Any]], hosts: list[str]) -> str:
    if len(hosts) == 1:
        info = supplies.get(hosts[0], {})
        label = info.get("label") or info.get("model") or "Data Enabler"
        return f"{label} ({hosts[0]})"
    return f"Color Kinetics ({len(hosts)})"


class ColorKineticsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one or more Data Enablers."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return ColorKineticsOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for Data Enabler IP addresses and poll each one."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"host": ""}
        if user_input is not None:
            try:
                hosts = parse_host_list(user_input[CONF_HOSTS])
                duplicates = sorted(set(hosts) & _configured_hosts(self.hass))
                if duplicates:
                    raise HostListError("already_configured")
                supplies, taken = await self._probe_hosts(hosts, skip_entry_id=None)
            except HostListError as err:
                errors[CONF_HOSTS] = err.code
            except KiNetError as err:
                _LOGGER.debug("Supply poll failed: %s", err)
                errors["base"] = "cannot_connect"
                placeholders["host"] = getattr(err, "host", "") or ""
            else:
                if taken:
                    errors[CONF_HOSTS] = "already_configured"
                else:
                    serial = supplies[hosts[0]]["serial_hex"]
                    await self.async_set_unique_id(serial)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=_title(supplies, hosts),
                        data={CONF_HOSTS: hosts, CONF_SUPPLIES: supplies},
                        options={
                            CONF_HOSTS: hosts,
                            CONF_SUPPLIES: supplies,
                            CONF_PROTOCOL: DEFAULT_PROTOCOL,
                            CONF_PORTOUT_VERSION: DEFAULT_PORTOUT_VERSION,
                            CONF_OUTPUT_PORT: DEFAULT_OUTPUT_PORT,
                            CONF_MANUAL_FIXTURES: "",
                        },
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _probe_hosts(
        self, hosts: list[str], *, skip_entry_id: str | None
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        """Poll each host. Return supply dicts and serials owned elsewhere."""
        known = _configured_serials(self.hass, skip_entry_id=skip_entry_id)
        supplies: dict[str, dict[str, Any]] = {}
        taken: set[str] = set()
        seen: set[str] = set()
        for host in hosts:
            try:
                info = await async_probe_host(host)
            except KiNetError as err:
                err.host = host  # type: ignore[attr-defined]
                raise
            if info.serial_hex in known or info.serial_hex in seen:
                taken.add(info.serial_hex)
            seen.add(info.serial_hex)
            supplies[host] = supply_entry_dict(info)
        return supplies, taken


class ColorKineticsOptionsFlow(OptionsFlow):
    """Change addresses, the KiNET output flavour, and manual fixtures."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit options for every supply in this entry."""
        errors: dict[str, str] = {}
        placeholders = {"host": "", "detail": ""}
        entry = self.config_entry
        if user_input is not None:
            try:
                options = await self._build_options(user_input)
            except HostListError as err:
                errors[CONF_HOSTS] = err.code
            except ManualFixtureError as err:
                errors[CONF_MANUAL_FIXTURES] = "invalid_manual"
                placeholders["detail"] = err.detail
            except KiNetError as err:
                errors["base"] = "cannot_connect"
                placeholders["host"] = getattr(err, "host", "") or ""
            else:
                return self.async_create_entry(title="", data=options)
        current = self._suggested()
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, current),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _suggested(self) -> dict[str, Any]:
        entry = self.config_entry
        protocol, version, port = (
            entry.options.get(CONF_PROTOCOL, DEFAULT_PROTOCOL),
            entry.options.get(CONF_PORTOUT_VERSION, DEFAULT_PORTOUT_VERSION),
            entry.options.get(CONF_OUTPUT_PORT, DEFAULT_OUTPUT_PORT),
        )
        return {
            CONF_HOSTS: "\n".join(hosts_of(entry)),
            CONF_PROTOCOL: protocol,
            CONF_PORTOUT_VERSION: str(version),
            CONF_OUTPUT_PORT: port,
            CONF_MANUAL_FIXTURES: manual_text_of(entry),
        }

    async def _build_options(self, user_input: dict[str, Any]) -> dict[str, Any]:
        hosts = parse_host_list(str(user_input[CONF_HOSTS]))
        entry = self.config_entry
        duplicates = sorted(set(hosts) & _configured_hosts(self.hass, skip_entry_id=entry.entry_id))
        if duplicates:
            raise HostListError("already_configured")
        manual = parse_manual_fixtures(str(user_input.get(CONF_MANUAL_FIXTURES) or ""), hosts)
        existing = supplies_of(entry)
        supplies: dict[str, dict[str, Any]] = {}
        known = _configured_serials(self.hass, skip_entry_id=entry.entry_id)
        own_serials = {
            str(info.get("serial_hex"))
            for info in existing.values()
            if info.get("serial_hex")
        }
        for host in hosts:
            if host in existing:
                supplies[host] = existing[host]
                continue
            try:
                info: SupplyInfo = await async_probe_host(host)
            except KiNetError as err:
                err.host = host  # type: ignore[attr-defined]
                raise
            if info.serial_hex in known and info.serial_hex not in own_serials:
                raise HostListError("already_configured")
            supplies[host] = supply_entry_dict(info)
        # Touch the parsed fixtures so an empty list is valid and stored as text.
        _ = manual
        return {
            CONF_HOSTS: hosts,
            CONF_SUPPLIES: supplies,
            CONF_PROTOCOL: user_input[CONF_PROTOCOL],
            CONF_PORTOUT_VERSION: int(user_input[CONF_PORTOUT_VERSION]),
            CONF_OUTPUT_PORT: int(user_input[CONF_OUTPUT_PORT]),
            CONF_MANUAL_FIXTURES: str(user_input.get(CONF_MANUAL_FIXTURES) or ""),
        }
