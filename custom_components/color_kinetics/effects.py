"""Effect frames for an ordered run of fixtures.

Speed is an integer 1 (slow) through 10 (fast). One cycle lasts

``60 * (3/60) ** ((clip(speed, 1, 10) - 1) / 9)`` seconds, so speed 1 is 60 s
and speed 10 is 3 s. The 1–10 slider and its entity id are unchanged. A saved
look keeps that speed number, and the same number is a slower cycle than it
was before. Phase is the position in the cycle, 0 at the start. Changing the
speed keeps the current phase and continues from there.

Hue offsets and holiday stripes use the index among the fixtures that are
actually being drawn. Unavailable fixtures are omitted by the caller and are
not written.

Holiday effects use fixed palettes. Brightness still scales every channel.
Christmas, Halloween, and Alternate each hold one of two colors for a half
cycle, then blend into the other over the last 30% of that half. The blend is
drawn by the existing frame loop. Warm twinkle is a deterministic sparkle, so
the same phase always draws the same frame. Latest command wins: a fixture
belongs to at most one running effect.
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
EFFECT_CHRISTMAS = "Christmas"
EFFECT_WARM_TWINKLE = "Warm twinkle"
EFFECT_CANDY_CANE = "Candy cane"
EFFECT_RED_WHITE_BLUE = "Red white blue"
EFFECT_HALLOWEEN = "Halloween"

EFFECT_NAMES = (
    EFFECT_SOLID,
    EFFECT_COLOR_WHEEL,
    EFFECT_RAINBOW,
    EFFECT_COLOR_CHASE,
    EFFECT_THEATER,
    EFFECT_ALTERNATE,
    EFFECT_BREATHE,
    EFFECT_CHRISTMAS,
    EFFECT_WARM_TWINKLE,
    EFFECT_CANDY_CANE,
    EFFECT_RED_WHITE_BLUE,
    EFFECT_HALLOWEEN,
)

RED = (255, 0, 0)
GREEN = (0, 255, 0)
WHITE = (255, 255, 255)
BLUE = (0, 0, 255)
WARM_WHITE = (255, 180, 80)
ORANGE = (255, 80, 0)
PURPLE = (128, 0, 255)
WARM_TWINKLE_BASE = 22
_TWINKLE_BUCKETS = 8

DEFAULT_EFFECT_SPEED = 5

# Two-color effects hold, then blend across the last 30% of each half cycle.
_CROSSFADE_HOLD = 0.70


def cycle_seconds(speed: float) -> float:
    """Seconds for one effect cycle. Speed 1 is 60 s, speed 10 is 3 s.

    The steps are logarithmic, so each one is the same ratio. Speed 10 matches
    the old speed 2 (one cycle every 3 s). Values outside 1–10 are clipped.
    """
    clipped = min(10.0, max(1.0, float(speed)))
    return 60.0 * (3.0 / 60.0) ** ((clipped - 1.0) / 9.0)


def cycle_phase(now: float, speed: float, origin: float = 0.0) -> float:
    """Position in the cycle, in ``[0, 1)``.

    ``origin`` is the time at which this speed's phase is 0. A running effect
    moves the origin when the speed changes so the phase does not jump.
    """
    return ((float(now) - float(origin)) / cycle_seconds(speed)) % 1.0


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


def _mix_rgb(
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    progress: float,
) -> tuple[int, int, int]:
    """Blend two colors. A full-scale channel is 128 at the midpoint."""
    clamped = min(1.0, max(0.0, float(progress)))
    step = int(round(clamped * 1000))
    mixed: list[int] = []
    for origin, dest in zip(start, end, strict=True):
        value = (int(origin) * (1000 - step) + int(dest) * step + 500) // 1000
        mixed.append(max(0, min(255, value)))
    return mixed[0], mixed[1], mixed[2]


def _half_cycle_color(
    phase: float,
    current: tuple[int, int, int],
    upcoming: tuple[int, int, int],
) -> tuple[int, int, int]:
    """Hold ``current``, then blend into ``upcoming`` over the last 30% of the half."""
    along = (float(phase) * 2.0) % 1.0
    if along <= _CROSSFADE_HOLD:
        return current
    return _mix_rgb(current, upcoming, (along - _CROSSFADE_HOLD) / (1.0 - _CROSSFADE_HOLD))


def _pair_for_half(
    index: int,
    phase: float,
    first: tuple[int, int, int],
    second: tuple[int, int, int],
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """This half's color and the next half's color for one fixture."""
    step = int(phase * 2.0) % 2
    if index % 2 == step:
        return first, second
    return second, first


def _alternating(
    index: int,
    phase: float,
    first: tuple[int, int, int],
    second: tuple[int, int, int],
    brightness: int,
) -> bytes:
    """Swap two colors between even and odd fixtures, blending across the change."""
    current, upcoming = _pair_for_half(index, phase, first, second)
    return _scale(_half_cycle_color(phase, current, upcoming), brightness)


def _moving_stripe(
    index: int,
    count: int,
    phase: float,
    colors: tuple[tuple[int, int, int], ...],
    width: int,
    brightness: int,
) -> bytes:
    """Slide a repeating stripe along the drawn order."""
    drawn = max(count, 1)
    offset = int(phase * drawn) % drawn
    period = max(width, 1) * len(colors)
    band = (index - offset) % period
    return _scale(colors[band // max(width, 1)], brightness)


def twinkle_is_sparkle(index: int, phase: float) -> bool:
    """True when this fixture is the bright sparkle for the current phase.

    Eight buckets per cycle. A fixture sparkles when ``(index + bucket)`` is
    a multiple of 4, so neighbors differ and the pattern walks as the phase
    advances. The same inputs always return the same answer.
    """
    bucket = int(phase * _TWINKLE_BUCKETS) % _TWINKLE_BUCKETS
    return (index + bucket) % 4 == 0


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
        current, upcoming = _pair_for_half(index, phase, rgb, (0, 0, 0))
        return _scale(_half_cycle_color(phase, current, upcoming), brightness)
    if effect == EFFECT_BREATHE:
        level = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
        scaled = round(brightness * (0.2 + 0.8 * level))
        return _scale(rgb, scaled)
    if effect == EFFECT_CHRISTMAS:
        return _alternating(index, phase, RED, GREEN, brightness)
    if effect == EFFECT_WARM_TWINKLE:
        if twinkle_is_sparkle(index, phase):
            level = brightness
        else:
            level = (brightness * WARM_TWINKLE_BASE) // 100
        return _scale(WARM_WHITE, level)
    if effect == EFFECT_CANDY_CANE:
        return _moving_stripe(index, count, phase, (RED, WHITE), 2, brightness)
    if effect == EFFECT_RED_WHITE_BLUE:
        return _moving_stripe(index, count, phase, (RED, WHITE, BLUE), 1, brightness)
    if effect == EFFECT_HALLOWEEN:
        return _alternating(index, phase, ORANGE, PURPLE, brightness)
    return _scale(rgb, brightness)


@dataclass
class EffectRun:
    """One running effect and the fixtures it currently owns, in order."""

    effect: str
    order: list[str]
    rgb: tuple[int, int, int]
    brightness: int
    speed: float
    phase_origin: float = 0.0
    phase_speed: float = field(init=False)

    def __post_init__(self) -> None:
        """Remember which speed the phase origin was built for."""
        self.phase_speed = float(self.speed)

    def phase_at(self, now: float) -> float:
        """Cycle position at ``now``. A new speed continues from this phase."""
        moment = float(now)
        speed = float(self.speed)
        if speed != self.phase_speed:
            held = cycle_phase(moment, self.phase_speed, self.phase_origin)
            self.phase_origin = moment - held * cycle_seconds(speed)
            self.phase_speed = speed
        return cycle_phase(moment, speed, self.phase_origin)


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
            phase = run.phase_at(now)
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
