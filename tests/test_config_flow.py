"""Config flow and options flow."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.color_kinetics.const import (
    CONF_HOSTS,
    CONF_MANUAL_FIXTURES,
    CONF_OUTPUT_PORT,
    CONF_PORTOUT_VERSION,
    CONF_PROTOCOL,
    DOMAIN,
)
from custom_components.color_kinetics import config_flow as flow_module
from custom_components.color_kinetics.protocol import KiNetTimeoutError, SupplyInfo

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "fake_udp")


def _supply(host: str, serial: str = "83EB007F00000000") -> SupplyInfo:
    return SupplyInfo(
        ip=host,
        mac="00:0a:c5:47:1f:7f",
        protocol_version=2,
        serial=bytes.fromhex(serial),
        serial_hex=serial,
        manufacturer="Philips Color Kinetics",
        model="Data Enabler Pro",
        firmware_part="SFT-000155-00",
        firmware_version="V34",
        label="Data Enabler Pro",
        details="M:Philips Color Kinetics\nD:Data Enabler Pro\n",
        trailer=b"",
    )


@pytest.fixture
def probes(monkeypatch):
    calls: list[str] = []

    async def _probe(host: str, timeout: float | None = None) -> SupplyInfo:
        calls.append(host)
        if host.endswith(".99"):
            raise KiNetTimeoutError(host)
        serial = "83EB007F00000000" if host == "172.16.10.41" else "1111111100000000"
        return _supply(host, serial)

    monkeypatch.setattr(flow_module, "async_probe_host", _probe)
    return calls


async def test_user_flow_creates_an_entry_for_one_or_more_supplies(hass, probes):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOSTS: "172.16.10.41\n172.16.10.42"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Color Kinetics (2)"
    assert result["data"]["hosts"] == ["172.16.10.41", "172.16.10.42"]
    assert result["options"]["protocol"] == "portout"
    assert result["options"]["portout_version"] == 1
    assert result["options"]["output_port"] == 1
    assert result["result"].unique_id == "83EB007F00000000"
    await hass.async_block_till_done()
    assert probes == ["172.16.10.41", "172.16.10.42"]


async def test_user_flow_errors(hass, probes):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    empty = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOSTS: "  \n# nothing"}
    )
    assert empty["errors"]["hosts"] == "no_hosts"

    invalid = await hass.config_entries.flow.async_configure(
        empty["flow_id"], {CONF_HOSTS: "255.255.255.255"}
    )
    assert invalid["errors"]["hosts"] == "invalid_host"

    offline = await hass.config_entries.flow.async_configure(
        invalid["flow_id"], {CONF_HOSTS: "172.16.10.99"}
    )
    assert offline["errors"]["base"] == "cannot_connect"
    assert offline["description_placeholders"]["host"] == "172.16.10.99"

    created = await hass.config_entries.flow.async_configure(
        offline["flow_id"], {CONF_HOSTS: "172.16.10.41"}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    again = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    duplicate = await hass.config_entries.flow.async_configure(
        again["flow_id"], {CONF_HOSTS: "172.16.10.41"}
    )
    assert duplicate["errors"]["hosts"] == "already_configured"


async def test_user_flow_aborts_when_the_supply_serial_is_already_known(hass, probes):
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="83EB007F00000000",
        data={"hosts": ["10.1.1.1"], "supplies": {"10.1.1.1": {"serial_hex": "DIFFERENT"}}},
        options={"hosts": ["10.1.1.1"], "supplies": {"10.1.1.1": {"serial_hex": "DIFFERENT"}}},
    )
    existing.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    aborted = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOSTS: "172.16.10.41"}
    )
    assert aborted["type"] is FlowResultType.ABORT
    assert aborted["reason"] == "already_configured"


async def test_same_serial_on_a_new_address_is_already_configured(hass, probes):
    """A second entry that polls the same supply serial is rejected."""

    async def _same(host: str, timeout: float | None = None) -> SupplyInfo:
        if host == "10.9.9.9":
            return _supply(host, "83EB007F00000000")
        return _supply(host, "2222222200000000")

    # First entry owns the serial via its supplies dict, not only unique_id.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "hosts": ["10.1.1.1"],
            "supplies": {"10.1.1.1": {"serial_hex": "83EB007F00000000"}},
        },
        options={
            "hosts": ["10.1.1.1"],
            "supplies": {"10.1.1.1": {"serial_hex": "83EB007F00000000"}},
        },
        unique_id="something-else",
    )
    entry.add_to_hass(hass)
    monkeypatch_probe = pytest.MonkeyPatch()
    monkeypatch_probe.setattr(flow_module, "async_probe_host", _same)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    rejected = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOSTS: "10.9.9.9"}
    )
    monkeypatch_probe.undo()
    assert rejected["type"] is FlowResultType.FORM
    assert rejected["errors"]["hosts"] == "already_configured"


async def test_options_flow_updates_protocol_manual_fixtures_and_hosts(hass, probes):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    created = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOSTS: "172.16.10.41"}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = created["result"]

    options = await hass.config_entries.options.async_init(entry.entry_id)
    assert options["type"] is FlowResultType.FORM
    bad = await hass.config_entries.options.async_configure(
        options["flow_id"],
        {
            CONF_HOSTS: "172.16.10.41",
            CONF_PROTOCOL: "portout",
            CONF_PORTOUT_VERSION: "1",
            CONF_OUTPUT_PORT: 1,
            CONF_MANUAL_FIXTURES: "not a fixture",
        },
    )
    assert bad["errors"]["manual_fixtures"] == "invalid_manual"

    offline = await hass.config_entries.options.async_configure(
        bad["flow_id"],
        {
            CONF_HOSTS: "172.16.10.99",
            CONF_PROTOCOL: "dmxout",
            CONF_PORTOUT_VERSION: "2",
            CONF_OUTPUT_PORT: 4,
            CONF_MANUAL_FIXTURES: "",
        },
    )
    assert offline["errors"]["base"] == "cannot_connect"

    saved = await hass.config_entries.options.async_configure(
        offline["flow_id"],
        {
            CONF_HOSTS: "172.16.10.41\n172.16.10.42",
            CONF_PROTOCOL: "dmxout",
            CONF_PORTOUT_VERSION: "2",
            CONF_OUTPUT_PORT: 4,
            CONF_MANUAL_FIXTURES: "172.16.10.41, 880055BE, 1, Oak left",
        },
    )
    assert saved["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_PROTOCOL] == "dmxout"
    assert entry.options[CONF_PORTOUT_VERSION] == 2
    assert entry.options[CONF_OUTPUT_PORT] == 4
    assert "880055BE" in entry.options[CONF_MANUAL_FIXTURES]
    assert entry.options[CONF_HOSTS] == ["172.16.10.41", "172.16.10.42"]


async def test_replacing_the_supply_frees_its_serial_for_a_new_entry(hass, probes):
    created = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    created = await hass.config_entries.flow.async_configure(
        created["flow_id"], {CONF_HOSTS: "172.16.10.41"}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = created["result"]
    assert entry.unique_id == "83EB007F00000000"

    options = await hass.config_entries.options.async_init(entry.entry_id)
    saved = await hass.config_entries.options.async_configure(
        options["flow_id"],
        {
            CONF_HOSTS: "172.16.10.42",
            CONF_PROTOCOL: "portout",
            CONF_PORTOUT_VERSION: "1",
            CONF_OUTPUT_PORT: 1,
            CONF_MANUAL_FIXTURES: "",
        },
    )
    assert saved["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.unique_id == "1111111100000000"
    assert "172.16.10.42" in entry.title

    again = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    reused = await hass.config_entries.flow.async_configure(
        again["flow_id"], {CONF_HOSTS: "172.16.10.41"}
    )
    assert reused["type"] is FlowResultType.CREATE_ENTRY
    assert reused["result"].unique_id == "83EB007F00000000"
    await hass.async_block_till_done()


async def test_options_flow_rejects_a_host_owned_by_another_entry(hass, probes):
    first = MockConfigEntry(
        domain=DOMAIN,
        data={"hosts": ["172.16.10.41"], "supplies": {}},
        options={"hosts": ["172.16.10.41"], "supplies": {}},
        unique_id="entry-a",
    )
    second = MockConfigEntry(
        domain=DOMAIN,
        data={"hosts": ["172.16.10.42"], "supplies": {}},
        options={
            "hosts": ["172.16.10.42"],
            "supplies": {},
            "protocol": "portout",
            "portout_version": 1,
            "output_port": 1,
            "manual_fixtures": "",
        },
        unique_id="entry-b",
    )
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(second.entry_id)
    rejected = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_HOSTS: "172.16.10.41",
            CONF_PROTOCOL: "portout",
            CONF_PORTOUT_VERSION: "1",
            CONF_OUTPUT_PORT: 1,
            CONF_MANUAL_FIXTURES: "",
        },
    )
    assert rejected["errors"]["hosts"] == "already_configured"

    owned = MockConfigEntry(
        domain=DOMAIN,
        data={
            "hosts": ["10.0.0.8"],
            "supplies": {"10.0.0.8": {"serial_hex": "1111111100000000"}},
        },
        options={
            "hosts": ["10.0.0.8"],
            "supplies": {"10.0.0.8": {"serial_hex": "1111111100000000"}},
        },
        unique_id="owned-serial",
    )
    owned.add_to_hass(hass)
    again = await hass.config_entries.options.async_init(second.entry_id)
    serial_clash = await hass.config_entries.options.async_configure(
        again["flow_id"],
        {
            CONF_HOSTS: "172.16.10.50",
            CONF_PROTOCOL: "portout",
            CONF_PORTOUT_VERSION: "1",
            CONF_OUTPUT_PORT: 1,
            CONF_MANUAL_FIXTURES: "",
        },
    )
    assert serial_clash["errors"]["hosts"] == "already_configured"
