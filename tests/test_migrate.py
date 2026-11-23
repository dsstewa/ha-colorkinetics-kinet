"""Upgrading a 0.1.0 entry keeps the supply device and entity ids."""

from __future__ import annotations

import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.color_kinetics.const import DOMAIN
from custom_components.color_kinetics.hub import STORAGE_VERSION
from custom_components.color_kinetics.migrate import _migrate_supplies, async_migrate_entry
from custom_components.color_kinetics.properties import canonical_supply_serial
from tests.fake_kinet import SUPPLY_HOST, SUPPLY_SERIAL
from tests.test_light import _settle

OLD_SERIAL = "83EB007F00000000"
FIXTURE = "880055BE"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "fake_udp")


def test_only_the_16_hex_supply_serial_is_rewritten():
    assert canonical_supply_serial(OLD_SERIAL) == SUPPLY_SERIAL
    assert canonical_supply_serial(SUPPLY_SERIAL) == SUPPLY_SERIAL
    assert canonical_supply_serial(FIXTURE) == FIXTURE


async def test_future_entry_versions_are_left_alone(hass):
    newer = MockConfigEntry(domain=DOMAIN, version=3, minor_version=1, unique_id=OLD_SERIAL)
    newer.add_to_hass(hass)
    assert await async_migrate_entry(hass, newer) is False
    assert newer.unique_id == OLD_SERIAL

    current = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=1, unique_id=SUPPLY_SERIAL
    )
    current.add_to_hass(hass)
    assert await async_migrate_entry(hass, current) is True
    assert current.unique_id == SUPPLY_SERIAL


async def test_malformed_supply_records_are_not_rewritten(hass):
    untouched = {"supplies": None}
    assert _migrate_supplies(untouched, {}) is untouched
    replacements: dict[str, str] = {}
    migrated = _migrate_supplies(
        {"supplies": {"172.16.10.41": "not-a-dict", "10.0.0.2": {"serial_hex": 12}}},
        replacements,
    )
    assert migrated["supplies"]["172.16.10.41"] == "not-a-dict"
    assert migrated["supplies"]["10.0.0.2"]["serial_hex"] == 12
    assert replacements == {}

    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=1, data={}, options={})
    entry.add_to_hass(hass)
    await Store(hass, 1, f"{DOMAIN}_{entry.entry_id}").async_save(
        {"supplies": {"172.16.10.41": "not-a-dict", "10.0.0.2": {"serial_hex": None}}}
    )
    assert await async_migrate_entry(hass, entry) is True
    stored = await Store(hass, 1, f"{DOMAIN}_{entry.entry_id}").async_load()
    assert stored["supplies"]["172.16.10.41"] == "not-a-dict"
    assert stored["supplies"]["10.0.0.2"]["serial_hex"] is None

    bare = MockConfigEntry(domain=DOMAIN, version=1, minor_version=1, data={}, options={})
    bare.add_to_hass(hass)
    await Store(hass, 1, f"{DOMAIN}_{bare.entry_id}").async_save({"supplies": []})
    assert await async_migrate_entry(hass, bare) is True
    assert (await Store(hass, 1, f"{DOMAIN}_{bare.entry_id}").async_load())["supplies"] == []


async def test_v010_upgrade_keeps_the_supply_device_and_entity_ids(hass):
    supplies = {
        SUPPLY_HOST: {
            "ip": SUPPLY_HOST,
            "mac": "00:0a:c5:47:1f:7f",
            "serial_hex": OLD_SERIAL,
            "manufacturer": "Philips Color Kinetics",
            "model": "Data Enabler Pro",
            "firmware": "SFT-000155-00 V34",
            "label": "Data Enabler Pro",
        }
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        unique_id=OLD_SERIAL,
        title=f"Data Enabler Pro ({SUPPLY_HOST})",
        data={"hosts": [SUPPLY_HOST], "supplies": supplies},
        options={
            "hosts": [SUPPLY_HOST],
            "supplies": supplies,
            "protocol": "portout",
            "portout_version": 1,
            "output_port": 1,
            "manual_fixtures": "",
        },
    )
    entry.add_to_hass(hass)
    await Store(hass, 1, f"{DOMAIN}_{entry.entry_id}").async_save(
        {
            "supplies": {SUPPLY_HOST: {"serial_hex": OLD_SERIAL}},
            "scanned_hosts": [],
            "ports": {},
            "fixtures": {
                FIXTURE: {
                    "key": FIXTURE,
                    "host": SUPPLY_HOST,
                    "serial_hex": FIXTURE,
                    "label": None,
                    "dmx_start": 0,
                    "type_code": 0x18,
                    "present": True,
                    "source": "discovery",
                    "properties": {},
                }
            },
        }
    )

    area = ar.async_get(hass).async_create("Oak")
    dev_reg = dr.async_get(hass)
    supply = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, OLD_SERIAL)},
        name=f"Data Enabler Pro ({SUPPLY_HOST})",
        manufacturer="Philips Color Kinetics",
        model="Data Enabler Pro",
        serial_number=OLD_SERIAL,
    )
    dev_reg.async_update_device(supply.id, area_id=area.id, name_by_user="Barn supply")
    fixture = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, FIXTURE)},
        name="Color Kinetics 880055BE",
        manufacturer="Philips Color Kinetics",
        via_device_id=supply.id,
    )
    dev_reg.async_update_device(fixture.id, area_id=area.id, name_by_user="Oak left")

    ent_reg = er.async_get(hass)
    button = ent_reg.async_get_or_create(
        "button",
        DOMAIN,
        f"{OLD_SERIAL}:rescan",
        suggested_object_id="barn_rescan",
        config_entry=entry,
        device_id=supply.id,
        original_name="Rescan fixtures",
    )
    light = ent_reg.async_get_or_create(
        "light",
        DOMAIN,
        FIXTURE,
        suggested_object_id="oak_left",
        config_entry=entry,
        device_id=fixture.id,
        original_name="Oak left",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, entry)

    assert entry.version == 2
    assert entry.minor_version == 1
    assert entry.unique_id == SUPPLY_SERIAL
    assert entry.data["supplies"][SUPPLY_HOST]["serial_hex"] == SUPPLY_SERIAL
    assert entry.options["supplies"][SUPPLY_HOST]["serial_hex"] == SUPPLY_SERIAL

    supply_devices = [
        device
        for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        if {(DOMAIN, OLD_SERIAL), (DOMAIN, SUPPLY_SERIAL)} & set(device.identifiers)
    ]
    assert len(supply_devices) == 1
    assert supply_devices[0].id == supply.id
    assert (DOMAIN, SUPPLY_SERIAL) in supply_devices[0].identifiers
    assert (DOMAIN, OLD_SERIAL) not in supply_devices[0].identifiers
    assert supply_devices[0].name_by_user == "Barn supply"
    assert supply_devices[0].area_id == area.id
    assert supply_devices[0].serial_number == SUPPLY_SERIAL

    fixture_devices = [
        device
        for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        if (DOMAIN, FIXTURE) in device.identifiers
    ]
    assert len(fixture_devices) == 1
    assert fixture_devices[0].id == fixture.id
    assert fixture_devices[0].name_by_user == "Oak left"
    assert fixture_devices[0].area_id == area.id
    assert fixture_devices[0].via_device_id == supply.id

    buttons = [
        entity
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if entity.domain == "button"
    ]
    assert len(buttons) == 1
    assert buttons[0].entity_id == button.entity_id
    assert buttons[0].unique_id == f"{SUPPLY_SERIAL}:rescan"
    assert buttons[0].device_id == supply.id

    restored = ent_reg.async_get(light.entity_id)
    assert restored is not None
    assert restored.entity_id == light.entity_id
    assert restored.unique_id == FIXTURE
    assert restored.device_id == fixture.id
    assert not any(
        entity.unique_id == OLD_SERIAL or entity.unique_id.startswith(f"{OLD_SERIAL}:")
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    )

    stored = await Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}").async_load()
    assert stored["supplies"][SUPPLY_HOST]["serial_hex"] == SUPPLY_SERIAL
    assert stored["fixtures"][FIXTURE]["serial_hex"] == FIXTURE
