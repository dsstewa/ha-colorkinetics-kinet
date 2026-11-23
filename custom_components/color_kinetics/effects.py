"""Effect frames for an ordered run of fixtures.

Speed is an integer 1 (slow) through 10 (fast). One cycle lasts ``6 / speed``
seconds. Phase is that cycle position, 0 at the start of a cycle. Hue offsets
use the index among the fixtures that are actually being drawn. Unavailable
fixtures are omitted by the caller and are not written.

Latest command wins: a fixture belongs to at most one running effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

EFFECT_SOLID = "Solid"
EFFECT_COLOR_WHEEL = "Color wheel"
EFFECT_RAINBOW = "Rainbow chase"
EFFECT_COLOR_CHASE = "Color chase"
EFFECT_THEATER = "Theater chase"
EFFECT_ALTERNATE = "Alternate"
EFFECT_BREATHE = "Breathe"

EFFECT_NAMES = (
    EFFECT_SOLID,
    EFFECT_COLOR_WHEEL,
    EFFECT_RAINBOW,
    EFFECT_COLOR_CHASE,
    EFFECT_THEATER,
    EFFECT_ALTERNATE,
    EFFECT_BREATHE,
)

DEFAULT_EFFECT_SPEED = 5


def cycle_seconds(speed: float) -> float:
    """Seconds for one effect cycle. Speed 1 is 6 s, speed 10 is 0.6 s."""
    clipped = min(10.0, max(1.0, float(speed)))
    return 6.0 / clipped


def cycle_phase(now: float, speed: float) -> float:
    """Position in the cycle, in ``[0, 1)``."""
    return (float(now) / cycle_seconds(speed)) % 1.0


def _scale(rgb: tuple[int, int, int], brightness: int) -> bytes:
    level = max(0, min(255, int(brightness)))
    return bytes((max(0, min(255, channel)) * level + 127) // 255 for channel in rgb)


def _hue_rgb(hue: float) -> tuple[int, int, int]:
    """Fully saturated RGB for a hue in degrees."""
    sector = (hue % 360.0) / 60.0
    index = int(sector)
    fraction = sector - index
    down = round(255 * (1.0 - fraction))
    up = round(255 * fraction)
    ladder = (
        (255, up, 0),
        (down, 255, 0),
        (0, 255, up),
        (0, down, 255),
        (up, 0, 255),
        (255, 0, down),
    )
    return ladder[index % 6]


def fixture_channels(
    effect: str,
    index: int,
    count: int,
    phase: float,
    rgb: tuple[int, int, int],
    brightness: int,
) -> bytes:
    """Channel bytes for one fixture. ``index`` is its place in the drawn order."""
    if effect == EFFECT_COLOR_WHEEL:
        return _scale(_hue_rgb(phase * 360.0), brightness)
    if effect == EFFECT_RAINBOW:
        span = 360.0 / max(count, 1)
        return _scale(_hue_rgb(phase * 360.0 + index * span), brightness)
    if effect == EFFECT_COLOR_CHASE:
        head = int(phase * max(count, 1)) % max(count, 1)
        if index == head:
            return _scale(rgb, brightness)
        return b"\x00\x00\x00"
    if effect == EFFECT_THEATER:
        step = int(phase * 3.0) % 3
        if index % 3 == step:
            return _scale(rgb, brightness)
        return b"\x00\x00\x00"
    if effect == EFFECT_ALTERNATE:
        step = int(phase * 2.0) % 2
        if index % 2 == step:
            return _scale(rgb, brightness)
        return b"\x00\x00\x00"
    if effect == EFFECT_BREATHE:
        level = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
        scaled = round(brightness * (0.2 + 0.8 * level))
        return _scale(rgb, scaled)
    return _scale(rgb, brightness)


@dataclass
class EffectRun:
    """One running effect and the fixtures it currently owns, in order."""

    effect: str
    order: list[str]
    rgb: tuple[int, int, int]
    brightness: int
    speed: float


@dataclass
class EffectEngine:
    """Which effect owns each fixture. The latest start or release wins."""

    runs: dict[str, EffectRun] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        """True when at least one animated effect still has fixtures."""
        return any(run.order for run in self.runs.values())

    def start(
        self,
        owner: str,
        effect: str,
        order: list[str],
        rgb: tuple[int, int, int],
        brightness: int,
        speed: float,
    ) -> None:
        """Give ``order`` to ``owner``. Those fixtures leave any previous effect."""
        if effect == EFFECT_SOLID:
            self.stop(owner)
            return
        self._detach_owner(owner)
        claimed: list[str] = []
        seen: set[str] = set()
        for key in order:
            if key in seen:
                continue
            seen.add(key)
            previous = self.owners.get(key)
            if previous is not None and previous != owner:
                self._drop_key(previous, key)
            self.owners[key] = owner
            claimed.append(key)
        if not claimed:
            self.runs.pop(owner, None)
            return
        self.runs[owner] = EffectRun(
            effect=effect,
            order=claimed,
            rgb=rgb,
            brightness=brightness,
            speed=speed,
        )

    def stop(self, owner: str) -> None:
        """Stop ``owner`` and free its fixtures."""
        self._detach_owner(owner)

    def release_fixture(self, key: str) -> str | None:
        """Remove one fixture from whichever effect owns it."""
        owner = self.owners.pop(key, None)
        if owner is None:
            return None
        self._drop_key(owner, key)
        return owner

    def _detach_owner(self, owner: str) -> None:
        run = self.runs.pop(owner, None)
        if run is None:
            return
        for key in run.order:
            if self.owners.get(key) == owner:
                self.owners.pop(key, None)

    def _drop_key(self, owner: str, key: str) -> None:
        run = self.runs.get(owner)
        if run is None:
            return
        run.order = [item for item in run.order if item != key]
        if not run.order:
            self.runs.pop(owner, None)

    def frames(
        self,
        now: float,
        dmx_of,
        available,
    ) -> dict[str, list[tuple[int, bytes]]]:
        """Channel writes grouped by host. Unavailable fixtures are skipped."""
        grouped: dict[str, list[tuple[int, bytes]]] = {}
        for run in self.runs.values():
            drawn = [key for key in run.order if available(key)]
            phase = cycle_phase(now, run.speed)
            count = len(drawn)
            for index, key in enumerate(drawn):
                located = dmx_of(key)
                if located is None:
                    continue
                host, start = located
                payload = fixture_channels(
                    run.effect, index, count, phase, run.rgb, run.brightness
                )
                grouped.setdefault(host, []).append((start, payload))
        return grouped
