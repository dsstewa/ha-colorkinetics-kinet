"""Per-fixture channel fades drawn by the frame loop.

A fade belongs to one fixture. A newer command cancels it for the fixtures
that command takes. Interpolation is linear in channel space and uses integer
math so the midpoint of a full-scale change is 128.
"""

from __future__ import annotations

from dataclasses import dataclass


def interpolate(start: bytes, target: bytes, progress: float) -> bytes:
    """Blend ``start`` toward ``target``. ``progress`` is clamped to 0..1."""
    clamped = min(1.0, max(0.0, float(progress)))
    step = int(round(clamped * 1000))
    blended = bytearray(len(start))
    for index, (origin, dest) in enumerate(zip(start, target, strict=False)):
        value = (int(origin) * (1000 - step) + int(dest) * step + 500) // 1000
        blended[index] = max(0, min(255, value))
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
