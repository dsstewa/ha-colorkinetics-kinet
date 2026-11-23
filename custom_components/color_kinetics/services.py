"""Save, recall, and delete named looks."""

from __future__ import annotations

from typing import Any

import probatio
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .hub import ColorKineticsHub

SERVICE_SAVE_LOOK = "save_look"
SERVICE_RECALL_LOOK = "recall_look"
SERVICE_DELETE_LOOK = "delete_look"

_NAME = probatio.All(cv.string, str.strip)


def _look_name(value: str) -> str:
    """Reject a blank name before the handler runs."""
    text = str(value).strip()
    if not text:
        raise probatio.Invalid("Enter a name for the look.")
    return text


_DEVICE = probatio.Any(cv.string, [cv.string])

SAVE_LOOK_SCHEMA = probatio.Schema(
    {
        probatio.Required("name"): probatio.All(_NAME, _look_name),
        probatio.Optional("device_id"): _DEVICE,
    }
)
RECALL_LOOK_SCHEMA = probatio.Schema(
    {
        probatio.Required("name"): probatio.All(_NAME, _look_name),
        probatio.Optional("transition"): probatio.All(
            probatio.Coerce(float), probatio.Range(min=0, max=3600)
        ),
        probatio.Optional("device_id"): _DEVICE,
    }
)
DELETE_LOOK_SCHEMA = probatio.Schema(
    {
        probatio.Required("name"): probatio.All(_NAME, _look_name),
        probatio.Optional("device_id"): _DEVICE,
    }
)


def _device_ids(raw: Any) -> set[str]:
    """Device ids from a service call. A blank value means no device was chosen."""
    if raw is None or raw == "":
        return set()
    if isinstance(raw, str):
        return {raw}
    return {str(item) for item in raw if item}


def expand_device_ids(
    hass: HomeAssistant, hubs: list[ColorKineticsHub], device_ids: set[str]
) -> set[str]:
    """Map a fixture device to its Data Enabler, and keep supply devices.

    The service picker lists Data Enablers. A caller can still pass a fixture
    device id; that fixture's supply is what selects the entry.
    """
    if not device_ids:
        return set()
    registry = dr.async_get(hass)
    expanded = set(device_ids)
    for device_id in device_ids:
        device = registry.async_get(device_id)
        if device is None:
            continue
        if device.via_device_id:
            expanded.add(device.via_device_id)
        for domain, ident in device.identifiers:
            if domain != DOMAIN:
                continue
            for hub in hubs:
                fixture = hub.get_fixture(str(ident))
                if fixture is None:
                    continue
                supply = hub.supply_device_id(fixture.host)
                if supply:
                    expanded.add(supply)
    return expanded


def _supply_device_id_values(hub: ColorKineticsHub) -> set[str]:
    """Device-registry ids of a hub's Data Enablers."""
    private = getattr(hub, "_supply_device_ids", None)
    if isinstance(private, dict):
        return {str(value) for value in private.values()}
    public = getattr(hub, "supply_device_ids", None)
    if isinstance(public, dict):
        return {str(value) for value in public.values()}
    return set()


def select_hubs(
    hubs: list[ColorKineticsHub], device_id: Any
) -> list[ColorKineticsHub]:
    """Pick the hub for a service call.

    One loaded entry needs no device. Several entries need the Data Enabler
    device id, or a fixture device that expand_device_ids has mapped to it.
    """
    wanted = _device_ids(device_id)
    if not wanted:
        if len(hubs) != 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="look_target",
            )
        return list(hubs)
    chosen = [
        hub
        for hub in hubs
        if wanted.intersection(_supply_device_id_values(hub))
    ]
    if not chosen:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="look_target",
        )
    return chosen


def _loaded_hubs(hass: HomeAssistant) -> list[ColorKineticsHub]:
    hubs: list[ColorKineticsHub] = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        hub = getattr(entry, "runtime_data", None)
        if isinstance(hub, ColorKineticsHub):
            hubs.append(hub)
    return hubs


def _hubs_for_call(call: ServiceCall) -> list[ColorKineticsHub]:
    hubs = _loaded_hubs(call.hass)
    wanted = expand_device_ids(call.hass, hubs, _device_ids(call.data.get("device_id")))
    return select_hubs(hubs, wanted or None)


async def _async_save_look(call: ServiceCall) -> None:
    for hub in _hubs_for_call(call):
        await hub.async_save_look(call.data["name"])


async def _async_recall_look(call: ServiceCall) -> None:
    transition = call.data.get("transition")
    for hub in _hubs_for_call(call):
        await hub.async_recall_look(call.data["name"], transition)


async def _async_delete_look(call: ServiceCall) -> None:
    for hub in _hubs_for_call(call):
        await hub.async_delete_look(call.data["name"])


def async_register_services(hass: HomeAssistant) -> None:
    """Register look services. Registering again replaces the handler."""
    hass.services.async_register(
        DOMAIN, SERVICE_SAVE_LOOK, _async_save_look, schema=SAVE_LOOK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RECALL_LOOK, _async_recall_look, schema=RECALL_LOOK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_LOOK, _async_delete_look, schema=DELETE_LOOK_SCHEMA
    )


def async_remove_services(hass: HomeAssistant) -> None:
    """Drop the look services when the last entry unloads."""
    for service in (SERVICE_SAVE_LOOK, SERVICE_RECALL_LOOK, SERVICE_DELETE_LOOK):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
