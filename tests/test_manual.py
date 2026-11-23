"""Host list and manual-fixture parsing."""

from __future__ import annotations

import pytest

from custom_components.color_kinetics.manual import (
    HostListError,
    ManualFixtureError,
    parse_host_list,
    parse_manual_fixtures,
)


def test_host_list_accepts_lines_commas_and_comments():
    hosts = parse_host_list("172.16.10.41\n# comment\n172.16.10.42, 172.16.10.41\n")
    assert hosts == ["172.16.10.41", "172.16.10.42"]


@pytest.mark.parametrize(
    "text",
    ["", "   ", "# only", "255.255.255.255", "224.0.0.1", "0.0.0.0", "name.local", "::1"],
)
def test_host_list_rejects_empty_and_non_unicast(text):
    with pytest.raises(HostListError) as caught:
        parse_host_list(text)
    assert caught.value.code in {"invalid_host", "no_hosts"}


def test_manual_fixture_forms():
    hosts = ["172.16.10.41"]
    parsed = parse_manual_fixtures(
        "\n".join(
            [
                "# comment",
                "880055BE, 1, Oak left",
                "0x88002CD9, 4",
                "Porch wash, 25",
                "172.16.10.41, 760054A3, 13, Drive",
            ]
        ),
        hosts,
    )
    assert [item.key for item in parsed] == [
        "880055BE",
        "88002CD9",
        "manual:172.16.10.41:porch-wash",
        "760054A3",
    ]
    assert parsed[0].dmx_start == 0
    assert parsed[0].label == "Oak left"
    assert parsed[2].serial_hex is None
    assert parsed[2].dmx_start == 24
    assert parsed[3].label == "Drive"
    assert parse_manual_fixtures("  \n# none\n", hosts) == []


def test_manual_fixture_with_several_supplies_requires_the_ip():
    hosts = ["172.16.10.41", "172.16.10.42"]
    parsed = parse_manual_fixtures("172.16.10.42, Stair, 7, North", hosts)
    assert parsed[0].host == "172.16.10.42"
    assert parsed[0].dmx_start == 6
    with pytest.raises(ManualFixtureError):
        parse_manual_fixtures("880055BE, 1", hosts)


@pytest.mark.parametrize(
    "text",
    [
        "880055BE, 0",
        "880055BE, 511",
        "880055BE, no",
        "880055BE",
        "172.16.10.99, 880055BE, 1",
        "880055BE, 1\n880055BE, 4",
        ", 1",
        "172.16.10.41, 880055BE, 1, extra, nope",
        "???, 1",
    ],
)
def test_manual_fixture_errors_name_the_line(text):
    with pytest.raises(ManualFixtureError) as caught:
        parse_manual_fixtures(text, ["172.16.10.41"])
    assert "Line" in caught.value.detail


def test_manual_fixtures_need_a_supply():
    with pytest.raises(ManualFixtureError):
        parse_manual_fixtures("880055BE, 1", [])
