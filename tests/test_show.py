"""Show phase, combined order, and follow-cycle checks."""

from __future__ import annotations

import pytest

from custom_components.color_kinetics.effects import (
    EFFECT_CHASE,
    EffectEngine,
    cycle_seconds,
    fixture_channels,
)
from custom_components.color_kinetics.show import (
    chain_members,
    combined_positions,
    follow_problem,
    normalize_leader,
    retarget_origin,
    root_of,
    show_phase,
    would_cycle,
)


def test_shared_phase_matches_on_two_hubs_and_survives_a_speed_change():
    origin = 1_000.0
    speed = 5
    now = origin + cycle_seconds(speed) * 0.25
    leader = show_phase(now, speed, origin)
    follower = show_phase(now, speed, origin)
    assert leader == pytest.approx(0.25)
    assert follower == pytest.approx(leader)

    moved = retarget_origin(now, origin, speed, 10)
    assert show_phase(now, 10, moved) == pytest.approx(leader)
    later = now + 0.5
    assert show_phase(later, 10, moved) == pytest.approx(show_phase(later, 10, moved))
    assert show_phase(later, 10, moved) != pytest.approx(show_phase(later, speed, origin))


def test_combined_order_puts_the_leader_first_and_keeps_the_first_index():
    positions, total = combined_positions([["a", "b", "c"], ["d", "e"]])
    assert total == 5
    assert positions == {"a": 0, "b": 1, "c": 2, "d": 3, "e": 4}

    again, total = combined_positions([["a", "b"], ["b", "c"]])
    assert again == {"a": 0, "b": 1, "c": 2}
    assert total == 3
    assert combined_positions([]) == ({}, 0)


def test_follow_cycles_are_rejected_and_a_chain_stays_in_order():
    links = {"L": "", "A": "L", "B": "L", "C": "A"}
    assert normalize_leader(" none ") == ""
    assert normalize_leader(None) == ""
    assert would_cycle(links, "C", "") is False
    assert would_cycle(links, "C", "C") is True
    assert would_cycle(links, "C", "A") is False
    assert would_cycle(links, "L", "C") is True
    assert would_cycle(links, "A", "B") is False
    assert root_of(links, "C") == "L"
    assert chain_members(links, "L") == ["L", "A", "C", "B"]
    assert root_of({"A": "missing"}, "A") == "A"
    assert chain_members({"A": "missing"}, "A") == ["A"]

    loop = {"A": "B", "B": "A"}
    assert follow_problem(links, "C", "", set(links)) is None
    assert follow_problem(links, "C", "missing", set(links)) == "follow_missing"
    assert follow_problem(links, "L", "C", set(links)) == "follow_cycle"
    assert follow_problem(links, "C", "L", set(links)) is None
    assert would_cycle(loop, "A", "B") is True
    assert root_of(loop, "A") == "A"
    assert chain_members(loop, "A") == ["A"]
    assert root_of({}, "missing") == "missing"


def test_a_synced_frame_uses_the_combined_index_and_shared_phase():
    """Follower fixtures continue the chase instead of starting a new one."""
    order = ["a", "b", "c", "d", "e"]
    phase = 0.2
    expected = {
        key: fixture_channels(EFFECT_CHASE, index, len(order), phase, (255, 0, 0), 255)
        for index, key in enumerate(order)
    }
    leader = EffectEngine()
    follower = EffectEngine()
    leader.start("all", EFFECT_CHASE, ["a", "b", "c"], (255, 0, 0), 255, 5)
    follower.start("all", EFFECT_CHASE, ["d", "e"], (255, 0, 0), 255, 9)

    def locate(key: str):
        return "host", order.index(key) * 3

    def placement(keys: list[str]):
        def _place(key: str, _keys: list[str] = keys):
            if key not in _keys:
                return None
            return order.index(key), len(order)

        return _place

    leader_frames = leader.frames(
        0.0,
        locate,
        lambda _key: True,
        syncs={"all": (placement(["a", "b", "c"]), phase)},
    )
    follower_frames = follower.frames(
        99.0,
        locate,
        lambda _key: True,
        syncs={"all": (placement(["d", "e"]), phase)},
    )
    painted = {start: payload for start, payload in leader_frames["host"]}
    painted.update({start: payload for start, payload in follower_frames["host"]})
    for key, payload in expected.items():
        assert painted[order.index(key) * 3] == payload

    skipped = follower.frames(
        0.0,
        lambda _key: None,
        lambda _key: True,
        syncs={"all": (lambda _key: None, phase)},
    )
    assert skipped == {}
