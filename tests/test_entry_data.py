"""Config-entry readers fall back when options are missing or invalid."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.color_kinetics.const import DOMAIN
from custom_components.color_kinetics.entry_data import (
    hosts_of,
    manual_text_of,
    output_settings_of,
    supplies_of,
)


def test_readers_prefer_options_and_reject_invalid_output_settings():
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "hosts": ["10.0.0.1"],
            "supplies": {"10.0.0.1": {"serial_hex": "AAAA"}},
        },
        options={
            "hosts": ["10.0.0.2"],
            "supplies": {"10.0.0.2": {"serial_hex": "BBBB"}},
            "protocol": "nope",
            "portout_version": "x",
            "output_port": 99,
            "manual_fixtures": "880055BE, 1",
        },
    )
    assert hosts_of(entry) == ["10.0.0.2"]
    assert supplies_of(entry)["10.0.0.2"]["serial_hex"] == "BBBB"
    assert output_settings_of(entry) == ("portout", 1, 1)
    assert manual_text_of(entry) == "880055BE, 1"


def test_readers_fall_back_to_data_and_defaults():
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"hosts": ["10.0.0.1"], "supplies": {"10.0.0.1": {"serial_hex": "AAAA"}}},
        options={"portout_version": 2, "output_port": "7", "protocol": "dmxout"},
    )
    assert hosts_of(entry) == ["10.0.0.1"]
    assert supplies_of(entry)["10.0.0.1"]["serial_hex"] == "AAAA"
    assert output_settings_of(entry) == ("dmxout", 2, 7)
    assert manual_text_of(MockConfigEntry(domain=DOMAIN, data={})) == ""
    assert output_settings_of(
        MockConfigEntry(
            domain=DOMAIN,
            data={},
            options={"portout_version": None, "output_port": None},
        )
    ) == ("portout", 1, 1)
    assert output_settings_of(
        MockConfigEntry(
            domain=DOMAIN,
            data={},
            options={"portout_version": 9, "output_port": "nope"},
        )
    ) == ("portout", 1, 1)
