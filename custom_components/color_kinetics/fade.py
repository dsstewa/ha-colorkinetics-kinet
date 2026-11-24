"""Per-fixture channel fades drawn by the frame loop.

A fade belongs to one fixture. A newer command cancels it for the fixtures
that command takes and starts again from the bytes currently in the buffer.

Interpolation is gamma-aware. Progress is linear in perceived brightness
(gamma 2.2). The DMX byte is the linear intensity that produces that step,
so a fade does not jump bright in the first half. Endpoints are exact: a
perceived level round-trips through every channel byte, and the integer
blend uses thousandths.
"""

from __future__ import annotations

from dataclasses import dataclass

_GAMMA = 2.2


def _perceived_mils(value: int) -> int:
    """Linear channel byte as a perceived level from 0 to 1000."""
    unit = (max(0, min(255, int(value))) / 255.0) ** (1.0 / _GAMMA)
    return int(round(unit * 1000.0))


def _channel_from_perceived(mils: int) -> int:
    """Perceived level, 0 to 1000, back to a linear channel byte."""
    perceived = max(0, min(1000, int(mils))) / 1000.0
    linear = perceived**_GAMMA
    return max(0, min(255, int(round(linear * 255.0))))


def interpolate(start: bytes, target: bytes, progress: float) -> bytes:
    """Blend ``start`` toward ``target``. ``progress`` is clamped to 0..1.

    The blend is linear in perceived brightness. The midpoint of a full-scale
    change is DMX 55, which is about half as bright to the eye as full.
    """
    clamped = min(1.0, max(0.0, float(progress)))
    step = int(round(clamped * 1000))
    blended = bytearray(len(start))
    for index, (origin, dest) in enumerate(zip(start, target, strict=False)):
        perceived = (
            _perceived_mils(origin) * (1000 - step) + _perceived_mils(dest) * step + 500
        ) // 1000
        blended[index] = _channel_from_perceived(perceived)
    return bytes(blended)


@dataclass
class _Fade:
    start: bytes
    target: bytes
    started: float
    duration: float


class FadeBook:
    """The fades currently in progress, keyed by fixture."""

    def __init__(self) -> None:
        """Start with no fades."""
        self._fades: dict[str, _Fade] = {}

    @property
    def active(self) -> bool:
        """True when at least one fixture is still fading."""
        return bool(self._fades)

    def contains(self, key: str) -> bool:
        """True when ``key`` has a fade that has not finished."""
        return key in self._fades

    def cancel(self, keys: list[str] | tuple[str, ...]) -> None:
        """Drop fades for these fixtures. Other fades keep running."""
        for key in keys:
            self._fades.pop(key, None)

    def clear(self) -> None:
        """Drop every fade."""
        self._fades.clear()

    def start(
        self,
        key: str,
        start: bytes,
        target: bytes,
        now: float,
        duration: float,
    ) -> bool:
        """Begin a fade. Return false when the caller should write ``target`` now."""
        if duration <= 0 or start == target:
            self._fades.pop(key, None)
            return False
        self._fades[key] = _Fade(
            start=bytes(start),
            target=bytes(target),
            started=float(now),
            duration=float(duration),
        )
        return True

    def frames(self, now: float) -> dict[str, bytes]:
        """Channel bytes for this instant. Finished fades are removed."""
        finished: list[str] = []
        rendered: dict[str, bytes] = {}
        for key, fade in self._fades.items():
            progress = (float(now) - fade.started) / fade.duration
            if progress >= 1.0:
                rendered[key] = fade.target
                finished.append(key)
            else:
                rendered[key] = interpolate(fade.start, fade.target, progress)
        for key in finished:
            self._fades.pop(key, None)
        return rendered

    def started_at(self, key: str) -> tuple[float, float] | None:
        """Return ``(started, duration)`` for a running fade."""
        fade = self._fades.get(key)
        if fade is None:
            return None
        return fade.started, fade.duration

    def target_of(self, key: str) -> bytes | None:
        """Channel bytes this fade is heading toward."""
        fade = self._fades.get(key)
        if fade is None:
            return None
        return fade.target
