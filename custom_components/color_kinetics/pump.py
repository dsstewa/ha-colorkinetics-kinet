"""Coalesce KiNET light frames to at most ``max_fps``, plus a keepalive.

The Data Enabler's hold time when frames stop is unknown, so the last
frame for each supply is resent about once a second. Changes that arrive
faster than the frame cap are merged into one datagram.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
import time

_LOGGER = logging.getLogger(__name__)

SendCallback = Callable[[str], bool]


class FramePump:
    """Schedule per-host light-data sends."""

    def __init__(
        self,
        *,
        min_interval: float,
        keepalive: float,
        send: SendCallback,
        now: Callable[[], float] | None = None,
    ) -> None:
        """Create a pump. ``send`` returns true when a datagram was written."""
        self._min_interval = min_interval
        self._keepalive = keepalive
        self._send = send
        self._now = now or time.monotonic
        self._dirty: dict[str, bool] = {}
        self._last: dict[str, float] = {}
        self._armed: set[str] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the scheduler task."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="color_kinetics frames")

    async def stop(self) -> None:
        """Cancel the scheduler task."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def mark_dirty(self, host: str) -> None:
        """Request a send of ``host``'s current frame, subject to the fps cap."""
        self._dirty[host] = True
        self._wake.set()

    def forget(self, host: str) -> None:
        """Stop sending to a supply that is no longer configured."""
        self._dirty.pop(host, None)
        self._last.pop(host, None)
        self._armed.discard(host)

    @staticmethod
    def _remaining(last: float | None, interval: float, now: float) -> float:
        """Seconds until ``interval`` has elapsed. Tiny float undershoot counts as due."""
        if last is None:
            return 0.0
        remaining = (last + interval) - now
        if remaining <= 1e-6:
            return 0.0
        return remaining

    def seconds_until_due(self) -> float:
        """Seconds until the next send should happen. Zero means send now."""
        now = self._now()
        due: list[float] = []
        for host, dirty in self._dirty.items():
            last = self._last.get(host)
            if dirty:
                due.append(self._remaining(last, self._min_interval, now))
            elif host in self._armed and last is not None:
                due.append(self._remaining(last, self._keepalive, now))
        if not due:
            return self._keepalive
        return min(due)

    def flush(self) -> None:
        """Send every host whose change or keepalive is due."""
        now = self._now()
        for host, dirty in list(self._dirty.items()):
            last = self._last.get(host)
            if dirty:
                if self._remaining(last, self._min_interval, now) > 0:
                    continue
            elif host not in self._armed or self._remaining(last, self._keepalive, now) > 0:
                continue
            try:
                sent = self._send(host)
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                _LOGGER.exception("KiNET send to %s failed", host)
                sent = False
            if sent:
                self._last[host] = now
                self._dirty[host] = False
                self._armed.add(host)
            elif dirty:
                # Nothing to send (no fixtures yet). Don't spin.
                self._dirty[host] = False

    async def _run(self) -> None:
        while True:
            delay = self.seconds_until_due()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                self._wake.clear()
            self.flush()
