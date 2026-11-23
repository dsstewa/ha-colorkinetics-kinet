"""Frame pump: 40 fps cap and the one-second keepalive."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.color_kinetics.pump import FramePump


def _pump(clock, sent, *, min_interval=0.025, keepalive=1.0, result=True, boom=False):
    def send(host):
        if boom:
            raise RuntimeError("send failed")
        sent.append((host, clock["t"]))
        return result

    return FramePump(
        min_interval=min_interval,
        keepalive=keepalive,
        send=send,
        now=lambda: clock["t"],
    )


def test_changes_coalesce_to_the_frame_cap_and_keepalive_repeats_the_frame():
    clock = {"t": 0.0}
    sent: list[tuple[str, float]] = []
    pump = _pump(clock, sent, min_interval=1 / 40, keepalive=1.0)
    assert pump.seconds_until_due() == pytest.approx(1.0)

    pump.mark_dirty("172.16.10.41")
    assert pump.seconds_until_due() == 0
    pump.flush()
    assert sent == [("172.16.10.41", 0.0)]

    pump.mark_dirty("172.16.10.41")
    pump.mark_dirty("172.16.10.41")
    assert pump.seconds_until_due() == pytest.approx(1 / 40)
    pump.flush()
    assert len(sent) == 1

    clock["t"] = 1 / 40
    pump.flush()
    assert len(sent) == 2

    clock["t"] = 1 / 40 + 1.0
    assert pump.seconds_until_due() == 0
    pump.flush()
    assert len(sent) == 3
    assert sent[-1][0] == "172.16.10.41"


def test_two_hosts_are_independent_and_forget_stops_keepalive():
    clock = {"t": 5.0}
    sent: list[tuple[str, float]] = []
    pump = _pump(clock, sent, min_interval=0.025, keepalive=1)
    pump.mark_dirty("a")
    pump.mark_dirty("b")
    pump.flush()
    assert {host for host, _time in sent} == {"a", "b"}
    pump.forget("b")
    clock["t"] = 6.1
    pump.flush()
    assert [host for host, _time in sent if host == "b"] == ["b"]
    assert any(host == "a" for host, _time in sent[2:])


def test_a_failed_or_empty_send_does_not_spin_or_arm_keepalive():
    clock = {"t": 0.0}
    sent: list[tuple[str, float]] = []
    pump = _pump(clock, sent, result=False)
    pump.mark_dirty("h")
    pump.flush()
    assert sent == [("h", 0.0)]
    assert pump.seconds_until_due() == pytest.approx(1.0)

    boom = _pump(clock, sent, boom=True)
    boom.mark_dirty("h")
    boom.flush()
    clock["t"] = 10
    boom.flush()


async def test_start_is_idempotent_and_stop_is_safe_before_start():
    clock = {"t": 0.0}
    pump = _pump(clock, [])
    await pump.stop()
    pump.start()
    pump.start()
    pump.mark_dirty("h")
    await asyncio.sleep(0.02)
    await pump.stop()


async def test_scheduler_loop_sends_then_keepalives():
    sent: list[str] = []
    pump = FramePump(
        min_interval=0.05,
        keepalive=0.08,
        send=lambda host: sent.append(host) or True,
    )
    pump.start()
    try:
        pump.mark_dirty("h")
        await asyncio.sleep(0.02)
        assert sent == ["h"]
        await asyncio.sleep(0.12)
        assert len(sent) >= 2
    finally:
        await pump.stop()
