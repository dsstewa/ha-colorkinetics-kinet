"""Config and options flow for Philips Color Kinetics."""

from __future__ import annotations

import logging
from typing import Any
import uuid

import probatio
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_FIXTURE_ORDER,
    CONF_GROUPS,
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
from .order import apply_submitted_order, merge_group_members
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

    # Version 2 rewrites the 0.1.0 16-hex supply serial to the 8-hex form.
    VERSION = 2
    MINOR_VERSION = 1

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
        """Choose light output, fixture order, or groups."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["output", "fixture_order", "groups"],
        )

    async def async_step_output(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit hosts, protocol, and manual fixtures."""
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
                serial = options[CONF_SUPPLIES][options[CONF_HOSTS][0]]["serial_hex"]
                self.hass.config_entries.async_update_entry(
                    entry,
                    unique_id=serial,
                    title=_title(options[CONF_SUPPLIES], options[CONF_HOSTS]),
                )
                return self.async_create_entry(title="", data=options)
        current = self._suggested()
        return self.async_show_form(
            step_id="output",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, current),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_fixture_order(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Drag fixtures into order. The saved value is a list of serials."""
        pairs = self._ordered_fixture_entities()
        if user_input is not None:
            chosen = [str(entity_id) for entity_id in user_input.get("fixtures") or []]
            by_entity = {entity_id: serial for entity_id, serial in pairs}
            submitted = [by_entity[entity_id] for entity_id in chosen if entity_id in by_entity]
            order = apply_submitted_order(
                self._saved_order(), submitted, self._discovered_serials()
            )
            return self._save_preserved(fixture_order=order)
        schema = probatio.Schema({})
        if pairs:
            schema = probatio.Schema(
                {
                    probatio.Required("fixtures"): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            multiple=True,
                            reorder=True,
                            include_entities=[entity_id for entity_id, _serial in pairs],
                        )
                    )
                }
            )
        suggested = {"fixtures": [entity_id for entity_id, _serial in pairs]} if pairs else {}
        return self.async_show_form(
            step_id="fixture_order",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            description_placeholders={"order": self._order_text(pairs)},
        )

    async def async_step_groups(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a group to edit, add one, or leave the list as it is."""
        groups = self._saved_groups()
        labels = await self._group_action_labels()
        options: list[dict[str, str]] = [
            {"value": "add", "label": labels["add"]},
            {"value": "done", "label": labels["done"]},
        ]
        options[1:1] = [
            {"value": group["id"], "label": group["name"]} for group in groups
        ]
        if user_input is not None:
            action = str(user_input["group_action"])
            if action == "done":
                return self._save_preserved()
            if action == "add":
                self.context["group_id"] = uuid.uuid4().hex
                return await self.async_step_group_edit()
            self.context["group_id"] = action
            return await self.async_step_group_edit()
        return self.async_show_form(
            step_id="groups",
            data_schema=probatio.Schema(
                {
                    probatio.Required("group_action"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options, sort=False)
                    )
                }
            ),
        )

    async def async_step_group_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name a group and drag its fixtures into order."""
        group_id = str(self.context.get("group_id") or "")
        current = next((group for group in self._saved_groups() if group["id"] == group_id), None)
        pairs = self._ordered_fixture_entities()
        by_entity = {entity_id: serial for entity_id, serial in pairs}
        by_serial = {serial: entity_id for entity_id, serial in pairs}
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("delete") and current is not None:
                groups = [group for group in self._saved_groups() if group["id"] != group_id]
                return self._save_preserved(groups=groups)
            name = str(user_input.get("name") or "").strip()
            groups = [group for group in self._saved_groups() if group["id"] != group_id]
            if not name:
                errors["name"] = "invalid_group"
            elif any(group["name"].casefold() == name.casefold() for group in groups):
                errors["name"] = "duplicate_group"
            else:
                chosen = [str(entity_id) for entity_id in user_input.get("members") or []]
                submitted = [by_entity[entity_id] for entity_id in chosen if entity_id in by_entity]
                previous = current["members"] if current is not None else []
                members = merge_group_members(previous, submitted, set(by_entity.values()))
                if not members:
                    errors["members"] = "group_members"
                else:
                    groups.append({"id": group_id, "name": name, "members": members})
                    return self._save_preserved(groups=groups)
        suggested_members = []
        if current is not None:
            suggested_members = [
                by_serial[serial] for serial in current["members"] if serial in by_serial
            ]
        schema_dict: dict[Any, Any] = {
            probatio.Required("name"): selector.TextSelector(),
        }
        if pairs:
            schema_dict[probatio.Optional("members")] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    multiple=True,
                    reorder=True,
                    include_entities=[entity_id for entity_id, _serial in pairs],
                )
            )
        schema_dict[probatio.Optional("delete")] = selector.BooleanSelector()
        suggested = {
            "name": current["name"] if current else "",
            "members": suggested_members,
            "delete": False,
        }
        return self.async_show_form(
            step_id="group_edit",
            data_schema=self.add_suggested_values_to_schema(probatio.Schema(schema_dict), suggested),
            errors=errors,
            description_placeholders={"name": current["name"] if current else "New group"},
        )

    async def _group_action_labels(self) -> dict[str, str]:
        """Labels for the group menu. The text lives in strings.json."""
        from homeassistant.helpers.translation import async_get_translations

        translated = await async_get_translations(
            self.hass, self.hass.config.language, "selector", [DOMAIN]
        )
        prefix = f"component.{DOMAIN}.selector.group_action.options."
        return {
            "add": translated.get(f"{prefix}add", "add"),
            "done": translated.get(f"{prefix}done", "done"),
        }

    def _save_preserved(self, **updates: Any) -> ConfigFlowResult:
        """Store order or groups without dropping the other options."""
        options = dict(self.config_entry.options)
        if "fixture_order" in updates:
            options[CONF_FIXTURE_ORDER] = updates["fixture_order"]
        if "groups" in updates:
            options[CONF_GROUPS] = updates["groups"]
        options.setdefault(CONF_FIXTURE_ORDER, list(self._saved_order()))
        options.setdefault(CONF_GROUPS, list(self._saved_groups()))
        return self.async_create_entry(title="", data=options)

    def _saved_order(self) -> list[str]:
        return [str(serial) for serial in self.config_entry.options.get(CONF_FIXTURE_ORDER) or []]

    def _saved_groups(self) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        for raw in self.config_entry.options.get(CONF_GROUPS) or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            groups.append(
                {
                    "id": str(raw["id"]),
                    "name": str(raw.get("name") or "Group"),
                    "members": [str(serial) for serial in raw.get("members") or []],
                }
            )
        return groups

    def _hub(self):
        try:
            return self.config_entry.runtime_data
        except (AttributeError, RuntimeError):
            return None

    def _discovered_serials(self) -> list[tuple[str, int]]:
        hub = self._hub()
        if hub is None:
            return []
        found: list[tuple[str, int]] = []
        seen: set[str] = set()
        for fixture in hub.fixtures():
            if not fixture.serial_hex or fixture.source == "manual" or fixture.serial_hex in seen:
                continue
            seen.add(fixture.serial_hex)
            address = fixture.dmx_start if fixture.dmx_start is not None else 1_000_000
            found.append((fixture.serial_hex, address))
        return found

    def _ordered_fixture_entities(self) -> list[tuple[str, str]]:
        registry = er.async_get(self.hass)
        hub = self._hub()
        serials = hub.fixture_order() if hub is not None else self._saved_order()
        pairs: list[tuple[str, str]] = []
        for serial in serials:
            entity_id = registry.async_get_entity_id("light", DOMAIN, serial)
            if entity_id:
                pairs.append((entity_id, serial))
        return pairs

    def _order_text(self, pairs: list[tuple[str, str]]) -> str:
        if not pairs:
            return "No discovered fixtures yet."
        return ", ".join(f"{index}. {serial}" for index, (_entity, serial) in enumerate(pairs, start=1))

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
            CONF_FIXTURE_ORDER: list(self._saved_order()),
            CONF_GROUPS: list(self._saved_groups()),
        }
