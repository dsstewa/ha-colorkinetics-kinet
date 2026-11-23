"""Diagnostic sensors for confirmed KiNET fields, plus one raw dump."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .hub import ColorKineticsConfigEntry, ColorKineticsHub, Fixture
from .properties import (
    RAW_FIXTURE_PROPS,
    RAW_SUPPLY_PROPS,
    SCALE_PROPS,
    STARTUP_PROPS,
    SUFFIX_DMX,
    SUFFIX_POWER_TIME,
    SUFFIX_RAW,
    SUFFIX_SCALE,
    SUFFIX_STARTUP,
    SUPPLY_POWER_TIME,
    TYPE_BLAST,
    channel_triple,
    power_time_text,
    raw_property_key,
)

_KIND_POWER = "power"
_KIND_DMX = "dmx"
_KIND_STARTUP = "startup"
_KIND_SCALE = "scale"
_KIND_RAW_SUPPLY = "raw_supply"
_KIND_RAW_FIXTURE = "raw_fixture"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ColorKineticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add diagnostic sensors. They update when a scan finishes, and are not polled."""
    hub = entry.runtime_data
    entities: dict[str, DetailSensor] = {}

    def _sync() -> None:
        added: list[DetailSensor] = []
        for unique_id, kind, translation_key, host, fixture_key in _specs(hub):
            entity = entities.get(unique_id)
            if entity is None:
                entity = DetailSensor(hub, unique_id, kind, translation_key, host, fixture_key)
                entities[unique_id] = entity
                added.append(entity)
            elif entity.hass is not None:
                entity.refresh()
        if added:
            async_add_entities(added)

    entry.async_on_unload(hub.add_listener(_sync))
    _sync()


def _specs(
    hub: ColorKineticsHub,
) -> list[tuple[str, str, str, str, str | None]]:
    specs: list[tuple[str, str, str, str, str | None]] = []
    for host in hub.hosts:
        ident = hub.supply_identifier(host)
        specs.append((f"{ident}:{SUFFIX_POWER_TIME}", _KIND_POWER, SUFFIX_POWER_TIME, host, None))
        specs.append((f"{ident}:{SUFFIX_RAW}", _KIND_RAW_SUPPLY, SUFFIX_RAW, host, None))
    for fixture in hub.fixtures():
        if not fixture.serial_hex or fixture.source == "manual":
            continue
        key = fixture.serial_hex
        host = fixture.host
        specs.append((f"{key}:{SUFFIX_DMX}", _KIND_DMX, SUFFIX_DMX, host, key))
        specs.append((f"{key}:{SUFFIX_STARTUP}", _KIND_STARTUP, SUFFIX_STARTUP, host, key))
        specs.append((f"{key}:{SUFFIX_RAW}", _KIND_RAW_FIXTURE, SUFFIX_RAW, host, key))
        if fixture.type_code == TYPE_BLAST:
            specs.append((f"{key}:{SUFFIX_SCALE}", _KIND_SCALE, SUFFIX_SCALE, host, key))
    return specs


class DetailSensor(SensorEntity):
    """One diagnostic value, or the raw property bag."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hub: ColorKineticsHub,
        unique_id: str,
        kind: str,
        translation_key: str,
        host: str,
        fixture_key: str | None,
    ) -> None:
        """Bind the sensor to a supply or a fixture."""
        self._hub = hub
        self._kind = kind
        self._host = host
        self._fixture_key = fixture_key
        self._attr_unique_id = unique_id
        self._attr_translation_key = translation_key
        self._apply()

    def _fixture(self) -> Fixture | None:
        if self._fixture_key is None:
            return None
        return self._hub.get_fixture(self._fixture_key)

    def _apply(self) -> None:
        present = self._is_present()
        self._attr_available = present
        self._attr_device_info = self._device_info()
        if not present:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        self._attr_native_value = self._value()
        self._attr_extra_state_attributes = self._attributes()

    def _is_present(self) -> bool:
        if self._host not in self._hub.hosts:
            return False
        if self._fixture_key is None:
            return True
        fixture = self._fixture()
        return bool(fixture and fixture.present and fixture.host == self._host)

    def _device_info(self):
        if self._fixture_key is None:
            return self._hub.supply_device_info(self._host)
        fixture = self._fixture()
        if fixture is None:
            return self._hub.supply_device_info(self._host)
        return self._hub.fixture_device_info(fixture)

    def _value(self):
        if self._kind == _KIND_POWER:
            raw = self._hub.supply_property(self._host, SUPPLY_POWER_TIME)
            if raw is None:
                return None
            return power_time_text(raw)
        fixture = self._fixture()
        props = fixture.properties if fixture else {}
        if self._kind == _KIND_DMX:
            if fixture is None or fixture.dmx_start is None:
                return None
            return fixture.dmx_start + 1
        if self._kind == _KIND_STARTUP:
            return channel_triple(props, STARTUP_PROPS)
        if self._kind == _KIND_SCALE:
            return channel_triple(props, SCALE_PROPS)
        count = len(self._attributes())
        if count == 1:
            return "1 property"
        return f"{count} properties"

    def _attributes(self) -> dict[str, object]:
        if self._kind == _KIND_RAW_SUPPLY:
            stored = self._hub.supply_properties(self._host)
            return {
                raw_property_key(prop): stored[f"{prop:04X}"]
                for prop in RAW_SUPPLY_PROPS
                if f"{prop:04X}" in stored
            }
        if self._kind == _KIND_RAW_FIXTURE:
            fixture = self._fixture()
            props = fixture.properties if fixture else {}
            return {
                raw_property_key(prop): props[prop]
                for prop in RAW_FIXTURE_PROPS
                if prop in props
            }
        return {}

    def refresh(self) -> None:
        """Pull the latest scan results into the entity state."""
        self._apply()
        if self.hass is not None:
            self.async_write_ha_state()
