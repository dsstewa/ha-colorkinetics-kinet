"""Effect frames for an ordered run of fixtures.

Speed is an integer 1 (slow) through 10 (fast). One cycle lasts

``60 * (3/60) ** ((clip(speed, 1, 10) - 1) / 9)`` seconds, so speed 1 is 60 s
and speed 10 is 3 s. The 1–10 slider and its entity id are unchanged. A saved
look keeps that speed number, and the same number is a slower cycle than it
was before. Phase is the position in the cycle, 0 at the start. Changing the
speed, intensity, palette, or direction keeps the current phase and continues
from there.

These patterns are a clean-room reading of how common light-show effects look.
WLED is EUPL-1.2 and none of its source is copied here.

Hue offsets and holiday stripes use the index among the fixtures that are
actually being drawn. Unavailable fixtures are omitted by the caller and are
not written.

Holiday effects use fixed palettes. Brightness still scales every channel.
Christmas, Halloween, Alternate, and Police each hold one of two colors for a
half cycle, then blend into the other. Intensity 5 blends over the last 30% of
that half. A higher intensity holds each color longer. The blend is drawn by
the existing frame loop. Warm twinkle is a
deterministic sparkle, so the same phase always draws the same frame. Latest
command wins: a fixture belongs to at most one running effect.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import math

from .const import (
    DEFAULT_EFFECT_DIRECTION,
    DEFAULT_EFFECT_INTENSITY,
    DEFAULT_EFFECT_PALETTE,
    DEFAULT_EFFECT_SPEED,
)

EFFECT_SOLID = "Solid"
EFFECT_GRADIENT = "Gradient"
EFFECT_BREATHE = "Breathe"
EFFECT_FADE = "Fade"
EFFECT_COLORLOOP = "Colorloop"
EFFECT_HEARTBEAT = "Heartbeat"
EFFECT_SUNRISE = "Sunrise"
EFFECT_RAINBOW_ALL = "Rainbow"
EFFECT_RAINBOW_CYCLE = "Rainbow cycle"
EFFECT_WIPE = "Wipe"
EFFECT_SWEEP = "Sweep"
EFFECT_THEATER = "Theater chase"
EFFECT_RUNNING = "Running lights"
EFFECT_CHASE = "Chase color"
EFFECT_SCANNER = "Scanner"
EFFECT_DUAL_SCANNER = "Dual scanner"
EFFECT_METEOR = "Meteor"
EFFECT_PALETTE_FLOW = "Palette flow"
EFFECT_RIPPLE = "Ripple"
EFFECT_TWINKLE = "Twinkle"
EFFECT_TWINKLE_FADE = "Twinkle fade"
EFFECT_CANDLE = "Candle"
EFFECT_FIREPLACE = "Fireplace"
EFFECT_LIGHTNING = "Lightning"
EFFECT_POLICE = "Police"
EFFECT_ALTERNATE = "Alternate"
EFFECT_CHRISTMAS = "Christmas"
EFFECT_CHRISTMAS_CHASE = "Christmas chase"
EFFECT_HALLOWEEN = "Halloween"

# Names from 0.1.x. They still start an effect; the light stores the new name.
EFFECT_COLOR_WHEEL = "Color wheel"
EFFECT_RAINBOW = "Rainbow chase"
EFFECT_COLOR_CHASE = "Color chase"
EFFECT_WARM_TWINKLE = "Warm twinkle"
EFFECT_CANDY_CANE = "Candy cane"
EFFECT_RED_WHITE_BLUE = "Red white blue"
EFFECT_STATIC = "Static"

# Grouped for the light's effect dropdown. Home Assistant shows this order.
EFFECT_NAMES = (
    EFFECT_SOLID,
    EFFECT_BREATHE,
    EFFECT_FADE,
    EFFECT_COLORLOOP,
    EFFECT_HEARTBEAT,
    EFFECT_SUNRISE,
    EFFECT_GRADIENT,
    EFFECT_RAINBOW_ALL,
    EFFECT_RAINBOW_CYCLE,
    EFFECT_WIPE,
    EFFECT_SWEEP,
    EFFECT_THEATER,
    EFFECT_RUNNING,
    EFFECT_CHASE,
    EFFECT_SCANNER,
    EFFECT_DUAL_SCANNER,
    EFFECT_METEOR,
    EFFECT_PALETTE_FLOW,
    EFFECT_RIPPLE,
    EFFECT_TWINKLE,
    EFFECT_TWINKLE_FADE,
    EFFECT_CANDLE,
    EFFECT_FIREPLACE,
    EFFECT_LIGHTNING,
    EFFECT_POLICE,
    EFFECT_ALTERNATE,
    EFFECT_CHRISTMAS,
    EFFECT_CHRISTMAS_CHASE,
    EFFECT_HALLOWEEN,
)

EFFECT_ALIASES = {
    EFFECT_STATIC: EFFECT_SOLID,
    EFFECT_COLOR_WHEEL: EFFECT_RAINBOW_ALL,
    EFFECT_RAINBOW: EFFECT_RAINBOW_CYCLE,
    EFFECT_COLOR_CHASE: EFFECT_CHASE,
    EFFECT_WARM_TWINKLE: EFFECT_TWINKLE,
    EFFECT_CANDY_CANE: EFFECT_CHRISTMAS_CHASE,
    EFFECT_RED_WHITE_BLUE: EFFECT_PALETTE_FLOW,
}

# What the 1–10 intensity slider does. Brightness stays on the light.
INTENSITY_NOTES = {
    EFFECT_SOLID: "Ignored. The light's brightness is the level.",
    EFFECT_BREATHE: "How far the brightness falls. 1 is a shallow dip, 10 reaches dark.",
    EFFECT_FADE: "How long each palette color holds before it blends into the next.",
    EFFECT_COLORLOOP: "How much of the palette one cycle walks. 5 and above walk the whole palette.",
    EFFECT_HEARTBEAT: "How dark the rest is between the two pulses.",
    EFFECT_SUNRISE: "How dark the ramp starts. 10 starts from off.",
    EFFECT_GRADIENT: "How much of the palette is spread along the order. 10 uses the whole palette.",
    EFFECT_RAINBOW_ALL: "Saturation. 5 and above are fully saturated.",
    EFFECT_RAINBOW_CYCLE: "How much of the hue wheel is spread along the order. 5 and above use the full wheel.",
    EFFECT_WIPE: "Softness of the leading edge. 5 and below are a hard edge.",
    EFFECT_SWEEP: "Softness of the leading edge. 5 and below are a hard edge.",
    EFFECT_THEATER: "Gap. 1–3 light every 2nd fixture, 4–7 every 3rd, 8–10 every 4th.",
    EFFECT_RUNNING: "Wave count. 8 and above draw two waves along the order.",
    EFFECT_CHASE: "Tail length. 5 and below are a single fixture; 10 leaves a six-fixture tail.",
    EFFECT_SCANNER: "Tail length of the bouncing dot.",
    EFFECT_DUAL_SCANNER: "Tail length of both bouncing dots.",
    EFFECT_METEOR: "Tail length. Stays short (about one to four fixtures) so it reads on eight pixels.",
    EFFECT_PALETTE_FLOW: "How many times the palette repeats along the order.",
    EFFECT_RIPPLE: "Width of the ring that expands from the center.",
    EFFECT_TWINKLE: "How many fixtures sparkle at once. Lower is sparser.",
    EFFECT_TWINKLE_FADE: "How many fixtures fade in and out. Sparser than Twinkle.",
    EFFECT_CANDLE: "Depth of each fixture's flicker. The fixture never goes out.",
    EFFECT_FIREPLACE: "Depth of the shared warm flicker.",
    EFFECT_LIGHTNING: "How long the single flash lasts. It still happens once per cycle.",
    EFFECT_POLICE: "How long each color holds before it blends. Higher holds longer. 5 is the 30% blend.",
    EFFECT_ALTERNATE: "How long each half holds before it blends. Higher holds longer. 5 is the 30% blend.",
    EFFECT_CHRISTMAS: "How long each half holds before it blends. Higher holds longer. 5 is the 30% blend.",
    EFFECT_CHRISTMAS_CHASE: "Stripe width. 8 and above are two fixtures wide.",
    EFFECT_HALLOWEEN: "How long each half holds before it blends. Higher holds longer. 5 is the 30% blend.",
}

PALETTE_CHRISTMAS = "Christmas"
PALETTE_HALLOWEEN = "Halloween"
PALETTE_RAINBOW = "Rainbow"
PALETTE_WARM = "Warm white"
PALETTE_ICE = "Ice"
PALETTE_FIRE = "Fire"
PALETTE_OCEAN = "Ocean"
PALETTE_FOREST = "Forest"
PALETTE_SUNSET = "Sunset"
PALETTE_PARTY = "Party"
PALETTE_JULY = "July 4th"
PALETTE_CUSTOM = "Custom"

PALETTE_NAMES = (
    PALETTE_CHRISTMAS,
    PALETTE_HALLOWEEN,
    PALETTE_RAINBOW,
    PALETTE_WARM,
    PALETTE_ICE,
    PALETTE_FIRE,
    PALETTE_OCEAN,
    PALETTE_FOREST,
    PALETTE_SUNSET,
    PALETTE_PARTY,
    PALETTE_JULY,
    PALETTE_CUSTOM,
)

PALETTE_COLORS: dict[str, tuple[tuple[int, int, int], ...]] = {
    PALETTE_CHRISTMAS: ((255, 0, 0), (0, 140, 0), (255, 255, 255)),
    PALETTE_HALLOWEEN: ((255, 80, 0), (128, 0, 255)),
    PALETTE_WARM: ((255, 180, 80), (255, 150, 60), (255, 214, 170)),
    PALETTE_ICE: ((170, 220, 255), (60, 130, 255), (255, 255, 255)),
    PALETTE_FIRE: ((160, 0, 0), (255, 72, 0), (255, 180, 40)),
    PALETTE_OCEAN: ((0, 30, 140), (0, 130, 190), (0, 210, 180)),
    PALETTE_FOREST: ((0, 60, 0), (0, 150, 40), (120, 210, 40)),
    PALETTE_SUNSET: ((255, 48, 0), (255, 24, 80), (80, 0, 150)),
    PALETTE_PARTY: ((255, 0, 120), (0, 255, 200), (255, 220, 0), (130, 0, 255)),
    PALETTE_JULY: ((255, 0, 0), (255, 255, 255), (0, 40, 220)),
}

DIRECTION_FORWARD = DEFAULT_EFFECT_DIRECTION
DIRECTION_REVERSE = "Reverse"
DIRECTIONS = (DIRECTION_FORWARD, DIRECTION_REVERSE)

RED = (255, 0, 0)
GREEN = (0, 255, 0)
WHITE = (255, 255, 255)
BLUE = (0, 0, 255)
WARM_WHITE = (255, 180, 80)
ORANGE = (255, 80, 0)
PURPLE = (128, 0, 255)
WARM_TWINKLE_BASE = 22
_TWINKLE_BUCKETS = 8

# Two-color effects hold, then blend across the last 30% of each half cycle.
_CROSSFADE_HOLD = 0.70

_LEGACY_FRAME_NAMES = frozenset(
    {
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
    }
)

Color = tuple[int, int, int]
Painter = Callable[[int, int, float, Color, int, int, str, str, tuple[Color, ...]], bytes]


def resolve_effect(name: str) -> str:
    """Map a 0.1.x effect name onto the effect the light stores."""
    return EFFECT_ALIASES.get(name, name)


def legacy_palette(name: str) -> str | None:
    """Palette a renamed effect should select so the old look stays recognizable.

    Christmas chase keeps its own red, green, and white. Candy cane therefore
    does not force a palette. Red white blue and Warm twinkle do, because
    Palette flow and Twinkle follow the palette selector.
    """
    if name == EFFECT_RED_WHITE_BLUE:
        return PALETTE_JULY
    if name == EFFECT_WARM_TWINKLE:
        return PALETTE_WARM
    return None


def clip_intensity(value: float) -> int:
    """Intensity slider as an integer 1 through 10."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_EFFECT_INTENSITY
    if math.isnan(number) or math.isinf(number):
        return DEFAULT_EFFECT_INTENSITY
    return int(min(10, max(1, round(number))))


def normalize_palette(value: str | None) -> str:
    """A known palette name. Anything else is Custom."""
    if value in PALETTE_NAMES:
        return str(value)
    folded = {name.casefold(): name for name in PALETTE_NAMES}
    return folded.get(str(value or "").strip().casefold(), PALETTE_CUSTOM)


def normalize_direction(value: str | None) -> str:
    """Forward or Reverse along the fixture order."""
    if str(value or "").strip().casefold() in {"reverse", "backward", "backwards"}:
        return DIRECTION_REVERSE
    return DIRECTION_FORWARD


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


def _scale(rgb: Color, brightness: int) -> bytes:
    level = max(0, min(255, int(brightness)))
    return bytes((max(0, min(255, channel)) * level + 127) // 255 for channel in rgb)


def _hue_rgb(hue: float) -> Color:
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


def _mix_rgb(start: Color, end: Color, progress: float) -> Color:
    """Blend two colors. A full-scale channel is 128 at the midpoint."""
    clamped = min(1.0, max(0.0, float(progress)))
    step = int(round(clamped * 1000))
    mixed: list[int] = []
    for origin, dest in zip(start, end, strict=True):
        value = (int(origin) * (1000 - step) + int(dest) * step + 500) // 1000
        mixed.append(max(0, min(255, value)))
    return mixed[0], mixed[1], mixed[2]


def _half_cycle_color(phase: float, current: Color, upcoming: Color) -> Color:
    """Hold ``current``, then blend into ``upcoming`` over the last 30% of the half."""
    return _blend_hold(phase, current, upcoming, _CROSSFADE_HOLD)


def _blend_hold(phase: float, current: Color, upcoming: Color, hold: float) -> Color:
    """Hold ``current``, then blend across the rest of the half cycle."""
    along = (float(phase) * 2.0) % 1.0
    if along <= hold or hold >= 0.999:
        return current
    return _mix_rgb(current, upcoming, (along - hold) / (1.0 - hold))


def _pair_for_half(index: int, phase: float, first: Color, second: Color) -> tuple[Color, Color]:
    """This half's color and the next half's color for one fixture."""
    step = int(phase * 2.0) % 2
    if index % 2 == step:
        return first, second
    return second, first


def _alternating(index: int, phase: float, first: Color, second: Color, brightness: int) -> bytes:
    """Swap two colors between even and odd fixtures, blending across the change."""
    current, upcoming = _pair_for_half(index, phase, first, second)
    return _scale(_half_cycle_color(phase, current, upcoming), brightness)


def _moving_stripe(
    index: int,
    count: int,
    phase: float,
    colors: tuple[Color, ...],
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


def _legacy_frame(
    effect: str,
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
) -> bytes:
    """0.1.3 frames. Used when an old name runs at intensity 5, forward, Custom."""
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


def _uses_legacy(effect: str, intensity: float, palette: str, direction: str) -> bool:
    """True when the call must reproduce a 0.1.3 frame byte for byte."""
    if effect not in _LEGACY_FRAME_NAMES:
        return False
    if clip_intensity(intensity) != DEFAULT_EFFECT_INTENSITY:
        return False
    if normalize_palette(palette) != PALETTE_CUSTOM:
        return False
    return normalize_direction(direction) == DIRECTION_FORWARD


def _is_reverse(direction: str) -> bool:
    return normalize_direction(direction) == DIRECTION_REVERSE


def _spatial(index: int, count: int, direction: str) -> int:
    """Index along the order. Reverse flips the order and does not move phase."""
    if count <= 1 or not _is_reverse(direction):
        return index
    return count - 1 - index


def _temporal(phase: float, direction: str) -> float:
    """Cycle position. Reverse runs the cycle backward for effects with no order."""
    wrapped = float(phase) % 1.0
    if _is_reverse(direction):
        return (1.0 - wrapped) % 1.0
    return wrapped


def _accent(rgb: Color) -> Color:
    """A second color so a one-color Custom palette still has something to chase."""
    red, green, blue = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    if max(red, green, blue) - min(red, green, blue) < 18:
        return (255, 96, 0)
    return (255 - red, 255 - green, 255 - blue)


def _stops(palette: str, rgb: Color, custom: tuple[Color, ...]) -> tuple[Color, ...]:
    """Discrete colors for a palette. Rainbow is sampled separately."""
    name = normalize_palette(palette)
    if name == PALETTE_CUSTOM:
        picked: list[Color] = []
        for color in custom:
            triple = (int(color[0]), int(color[1]), int(color[2]))
            if triple not in picked:
                picked.append(triple)
        if not picked:
            picked.append((int(rgb[0]), int(rgb[1]), int(rgb[2])))
        if len(picked) == 1:
            picked.append(_accent(picked[0]))
        return tuple(picked)
    return PALETTE_COLORS.get(name, PALETTE_COLORS[PALETTE_PARTY])


def _sample(palette: str, position: float, rgb: Color, custom: tuple[Color, ...]) -> Color:
    """Color at ``position`` in ``[0, 1)`` on the selected palette."""
    pos = float(position) % 1.0
    if normalize_palette(palette) == PALETTE_RAINBOW:
        return _hue_rgb(pos * 360.0)
    colors = _stops(palette, rgb, custom)
    scaled = pos * len(colors)
    index = int(scaled) % len(colors)
    frac = scaled - int(scaled)
    return _mix_rgb(colors[index], colors[(index + 1) % len(colors)], frac)


def _hold_for(intensity: int) -> float:
    """Fraction of each half cycle spent on one color before the blend.

    Intensity 5 is the historical 70% hold, so the blend is the last 30%.
    A higher intensity holds longer (intensity 10 holds 90%, intensity 1 holds 54%).
    """
    if intensity == DEFAULT_EFFECT_INTENSITY:
        return _CROSSFADE_HOLD
    return min(0.90, max(0.40, _CROSSFADE_HOLD + (intensity - 5) * 0.04))


def _wave(phase: float) -> float:
    return 0.5 - 0.5 * math.cos(2.0 * math.pi * (float(phase) % 1.0))


def _triangle(phase: float) -> float:
    """0 at the ends of the cycle and 1 in the middle."""
    return 1.0 - abs((float(phase) % 1.0) * 2.0 - 1.0)


def _head(phase: float, count: int) -> float:
    if count <= 1:
        return 0.0
    return _triangle(phase) * (count - 1)


def _paint_solid(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """One color. The engine does not animate Solid."""
    del index, count, phase, intensity, palette, direction, custom
    return _scale(rgb, brightness)


def _paint_breathe(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Every fixture brightens and dims together."""
    del index, count
    travel = _temporal(phase, direction)
    wave = _wave(travel)
    floor = 0.2 if intensity == 5 else max(0.0, min(0.85, (45 - 5 * intensity) / 100))
    color = rgb if palette == PALETTE_CUSTOM else _sample(palette, travel, rgb, custom)
    return _scale(color, round(brightness * (floor + (1.0 - floor) * wave)))


def _paint_fade(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Hold each palette color, then blend into the next. Every fixture matches."""
    del index, count
    if palette == PALETTE_RAINBOW:
        colors = tuple(_hue_rgb(step * 60.0) for step in range(6))
    else:
        colors = _stops(palette, rgb, custom)
    travel = _temporal(phase, direction) * len(colors)
    slot = int(travel) % len(colors)
    frac = travel - int(travel)
    hold = min(0.85, max(0.20, 0.50 + (5 - intensity) * 0.06))
    if frac <= hold:
        color = colors[slot]
    else:
        color = _mix_rgb(colors[slot], colors[(slot + 1) % len(colors)], (frac - hold) / (1.0 - hold))
    return _scale(color, brightness)


def _paint_colorloop(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Walk the palette smoothly. Every fixture shows the same color."""
    del index, count
    span = 1.0 if intensity >= 5 else 0.35 + 0.65 * (intensity / 5.0)
    travel = _temporal(phase, direction) * span
    return _scale(_sample(palette, travel, rgb, custom), brightness)


def _paint_heartbeat(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Two quick pulses, then a rest. Every fixture beats together."""
    del index, count

    def pulse(center: float, width: float) -> float:
        distance = abs(_temporal(phase, direction) - center)
        if distance >= width:
            return 0.0
        raised = 1.0 - distance / width
        return math.sin(raised * math.pi / 2.0) ** 2

    beat = max(pulse(0.10, 0.07), pulse(0.24, 0.09) * 0.7)
    floor = max(0.04, 0.40 - 0.036 * intensity)
    color = rgb if palette == PALETTE_CUSTOM else _sample(palette, 0.0, rgb, custom)
    return _scale(color, round(brightness * (floor + (1.0 - floor) * beat)))


def _paint_sunrise(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """A slow warm ramp. Reverse runs it back down."""
    del index, count
    travel = _temporal(phase, direction)
    smooth = travel * travel * (3.0 - 2.0 * travel)
    start = max(0.0, 0.40 - 0.04 * intensity)
    level = start + (1.0 - start) * smooth
    if palette == PALETTE_CUSTOM:
        target = _sample(PALETTE_SUNSET, 0.25 + 0.5 * smooth, rgb, custom)
    else:
        target = _sample(palette, 0.2 + 0.6 * smooth, rgb, custom)
    return _scale(_mix_rgb((80, 16, 0), target, smooth), round(brightness * level))


def _paint_gradient(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """A still spread of the palette. Phase does not move it."""
    del phase
    span = 0.25 + 0.75 * (intensity - 1) / 9.0
    if count <= 1:
        position = 0.0
    else:
        position = _spatial(index, count, direction) / (count - 1) * span
    return _scale(_sample(palette, position, rgb, custom), brightness)


def _desaturate(rgb: Color, saturation: float) -> Color:
    if saturation >= 1:
        return rgb
    gray = sum(rgb) // 3
    return _mix_rgb(rgb, (gray, gray, gray), 1.0 - saturation)


def _paint_rainbow(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Every fixture shows the same hue, and the hue turns through the cycle."""
    del index, count, rgb, palette, custom
    travel = phase % 1.0 if not _is_reverse(direction) else _temporal(phase, direction)
    color = _hue_rgb(travel * 360.0)
    if intensity < 5:
        color = _desaturate(color, intensity / 5.0)
    return _scale(color, brightness)


def _paint_rainbow_cycle(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """The hue wheel spread along the order, rotating once per cycle."""
    del rgb, palette, custom
    drawn = max(count, 1)
    span = 360.0 if intensity >= 5 else 360.0 * intensity / 5.0
    spatial = _spatial(index, drawn, direction)
    color = _hue_rgb((phase % 1.0) * 360.0 + spatial * (span / drawn))
    return _scale(color, brightness)


def _edge_level(spatial: int, edge: float, softness: float) -> float:
    if spatial + 1 <= edge:
        return 1.0
    if spatial >= edge + softness:
        return 0.0
    if softness <= 0:
        return max(0.0, min(1.0, edge - spatial))
    return max(0.0, min(1.0, (edge + softness - spatial) / (1.0 + softness)))


def _paint_wipe(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Fill the order from one end. Reverse fills from the other end."""
    drawn = max(count, 1)
    spatial = _spatial(index, drawn, direction)
    softness = 0.0 if intensity <= 5 else (intensity - 5) * 0.35
    level = _edge_level(spatial, (phase % 1.0) * drawn, softness)
    if level <= 0:
        return b"\x00\x00\x00"
    color = rgb if palette == PALETTE_CUSTOM else _sample(palette, spatial / drawn, rgb, custom)
    return _scale(color, round(brightness * level))


def _paint_sweep(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Wipe to the end and back in one cycle."""
    drawn = max(count, 1)
    spatial = _spatial(index, drawn, direction)
    softness = 0.0 if intensity <= 5 else (intensity - 5) * 0.35
    level = _edge_level(spatial, _triangle(phase) * drawn, softness)
    if level <= 0:
        return b"\x00\x00\x00"
    color = rgb if palette == PALETTE_CUSTOM else _sample(palette, spatial / drawn, rgb, custom)
    return _scale(color, round(brightness * level))


def _paint_theater(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Every nth fixture, stepping along the order."""
    gap = 4 if intensity >= 8 else 3 if intensity >= 4 else 2
    step = int((phase % 1.0) * gap) % gap
    spatial = _spatial(index, max(count, 1), direction)
    if spatial % gap != step:
        return b"\x00\x00\x00"
    color = rgb if palette == PALETTE_CUSTOM else _sample(palette, 0.0, rgb, custom)
    return _scale(color, brightness)


def _paint_running(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """A sine wave of brightness traveling along the order."""
    drawn = max(count, 1)
    spatial = _spatial(index, drawn, direction)
    waves = 2 if intensity >= 8 else 1
    wave = _wave(phase + spatial / drawn * waves)
    floor = 0.05 if intensity >= 8 else 0.2
    color = rgb if palette == PALETTE_CUSTOM else _sample(palette, spatial / drawn, rgb, custom)
    return _scale(color, round(brightness * (floor + (1.0 - floor) * wave)))


def _paint_chase(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """A dot, and at higher intensity a fading tail, running along the order."""
    drawn = max(count, 1)
    head = int((phase % 1.0) * drawn) % drawn
    spatial = _spatial(index, drawn, direction)
    tail = 1 if intensity <= 5 else min(drawn, intensity - 4)
    behind = (head - spatial) % drawn
    if behind >= tail:
        return b"\x00\x00\x00"
    color = rgb if palette == PALETTE_CUSTOM else _sample(palette, spatial / drawn, rgb, custom)
    if behind == 0:
        return _scale(color, brightness)
    return _scale(color, round(brightness * (1.0 - behind / tail)))


def _paint_scanner(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """One dot bounces end to end and leaves a short tail."""
    drawn = max(count, 1)
    spatial = float(_spatial(index, drawn, direction))
    tail = max(1.0, intensity / 2.0)
    distance = abs(spatial - _head(phase, drawn))
    if distance >= tail:
        return b"\x00\x00\x00"
    color = rgb if palette == PALETTE_CUSTOM else _sample(palette, 0.15, rgb, custom)
    return _scale(color, round(brightness * (1.0 - distance / tail)))


def _paint_dual_scanner(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Two dots bounce toward and away from each other."""
    drawn = max(count, 1)
    spatial = float(_spatial(index, drawn, direction))
    tail = max(1.0, intensity / 3.0)
    head_a = _head(phase, drawn)
    head_b = (drawn - 1) - head_a
    distance_a = abs(spatial - head_a)
    distance_b = abs(spatial - head_b)
    distance = min(distance_a, distance_b)
    if distance >= tail:
        return b"\x00\x00\x00"
    stops = _stops(palette, rgb, custom)
    # The two dots keep different colors so Reverse swaps which end is which.
    color = stops[0] if distance_a <= distance_b else stops[min(1, len(stops) - 1)]
    return _scale(color, round(brightness * (1.0 - distance / tail)))


def _paint_meteor(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """A bright head with a short tail, one pass per cycle."""
    drawn = max(count, 1)
    travel = _temporal(phase, direction)
    head = travel * drawn
    behind = (index - head) if _is_reverse(direction) else (head - index)
    tail = 1.0 + intensity // 3
    if behind < 0 or behind > tail:
        return b"\x00\x00\x00"
    base = rgb if palette == PALETTE_CUSTOM else _sample(palette, 0.12, rgb, custom)
    color = _mix_rgb(base, WHITE, 0.35) if behind < 0.6 else base
    return _scale(color, round(brightness * (1.0 - behind / (tail + 0.01))))


def _paint_flow(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """The palette slides along the order."""
    drawn = max(count, 1)
    spatial = _spatial(index, drawn, direction)
    repeats = 2 if intensity >= 8 else 1
    position = (spatial / drawn * repeats + (phase % 1.0)) % 1.0
    return _scale(_sample(palette, position, rgb, custom), brightness)


def _paint_ripple(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """A ring expands from the center. Reverse draws it back in."""
    drawn = max(count, 1)
    center = (drawn - 1) / 2.0
    radius = _temporal(phase, direction) * (center + 1.2)
    width = 0.55 + intensity * 0.12
    delta = abs(abs(index - center) - radius)
    level = 0.06 if delta >= width else 0.06 + 0.94 * (1.0 - delta / width)
    color = rgb if palette == PALETTE_CUSTOM else _sample(palette, 0.45, rgb, custom)
    return _scale(color, round(brightness * level))


def _paint_twinkle(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """A few fixtures sparkle. The pattern is fixed for a given phase."""
    period = max(2, 11 - intensity)
    bucket = int((phase % 1.0) * 8) % 8
    spatial = _spatial(index, max(count, 1), direction)
    on = (spatial + bucket) % period == 0
    if palette == PALETTE_CUSTOM:
        color = rgb
    else:
        color = _sample(palette, (index % 7) / 7.0, rgb, custom)
    if on:
        return _scale(color, brightness)
    return _scale(color, (brightness * 16) // 100)


def _paint_twinkle_fade(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Fewer fixtures than Twinkle, each one fading in and out."""
    period = max(3, 14 - intensity)
    bucket_phase = (phase % 1.0) * 8.0
    bucket = int(bucket_phase) % 8
    local = bucket_phase - int(bucket_phase)
    spatial = _spatial(index, max(count, 1), direction)
    selected = (spatial * 3 + bucket) % period == 0
    wave = _wave(local) if selected else 0.0
    if palette == PALETTE_CUSTOM:
        color = rgb
    else:
        color = _sample(palette, (index % 5) / 5.0, rgb, custom)
    return _scale(color, round(brightness * (0.10 + 0.90 * wave)))


def _bounded_wobble(*parts: float) -> float:
    return max(-1.0, min(1.0, sum(parts)))


def _paint_candle(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """A gentle per-fixture flicker that stays lit."""
    del count
    sign = -1.0 if _is_reverse(direction) else 1.0
    wobble = _bounded_wobble(
        sign * 0.55 * math.sin(2.0 * math.pi * (phase * 2.0 + index * 0.17)),
        sign * 0.30 * math.sin(2.0 * math.pi * (phase * 4.6 + index * 0.41)),
        sign * 0.15 * math.sin(2.0 * math.pi * (phase * 1.2 + index * 0.73)),
    )
    depth = 0.08 + 0.035 * (intensity - 1)
    level = 1.0 - depth * (0.5 + 0.5 * wobble)
    if palette == PALETTE_WARM:
        color: Color = (255, 176, 90)
    elif palette in (PALETTE_CUSTOM, PALETTE_FIRE):
        color = (255, 132, 36)
    else:
        color = _mix_rgb((255, 132, 36), _sample(palette, 0.2, rgb, custom), 0.40)
    return _scale(color, round(brightness * level))


def _paint_fireplace(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """One warm flicker across the run, with only a small per-fixture difference."""
    sign = -1.0 if _is_reverse(direction) else 1.0
    wobble = _bounded_wobble(
        sign * 0.65 * math.sin(2.0 * math.pi * phase * 1.6),
        sign * 0.35 * math.sin(2.0 * math.pi * (phase * 2.8 + index * 0.04)),
    )
    depth = 0.05 + 0.025 * (intensity - 1)
    level = 1.0 - depth * (0.5 + 0.5 * wobble)
    fire = _sample(PALETTE_FIRE, (index / max(count, 1)) * 0.65, rgb, ())
    if palette not in (PALETTE_CUSTOM, PALETTE_FIRE):
        fire = _mix_rgb(fire, _sample(palette, 0.25, rgb, custom), 0.35)
    return _scale(fire, round(brightness * level))


def _paint_lightning(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Dim, then one flash per cycle. A single fixture leads the flash."""
    travel = _temporal(phase, direction)
    window = 0.012 + 0.007 * (intensity - 1)
    delta = abs(travel - 0.81)
    if delta > 0.5:
        delta = 1.0 - delta
    dim: Color = (10, 12, 28)
    if palette not in (PALETTE_CUSTOM, PALETTE_RAINBOW):
        dim = _mix_rgb(dim, _sample(palette, 0.1, rgb, custom), 0.5)
    leader = _spatial(0, max(count, 1), direction)
    if window <= delta < window * 2.4 and index == leader:
        return _scale(_mix_rgb(dim, WHITE, 0.55), brightness)
    if delta < window * 0.30:
        return _scale(WHITE, brightness)
    if delta < window:
        fade = 1.0 - (delta - window * 0.30) / (window * 0.70)
        return _scale(_mix_rgb(dim, WHITE, fade), brightness)
    return _scale(dim, max(1, (brightness * 10) // 100))


def _paint_police(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Even and odd fixtures swap two colors. Custom is red and blue."""
    if palette == PALETTE_CUSTOM:
        first, second = RED, BLUE
    else:
        colors = _stops(palette, rgb, custom)
        first = colors[0]
        second = colors[1] if len(colors) > 1 else _accent(first)
    spatial = _spatial(index, max(count, 1), direction)
    current, upcoming = _pair_for_half(spatial, phase, first, second)
    return _scale(_blend_hold(phase, current, upcoming, _hold_for(intensity)), brightness)


def _paint_pair(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
    first: Color,
    second: Color,
) -> bytes:
    """Even/odd swap of two fixed colors. ``rgb`` and the palette are not used."""
    del rgb, palette, custom
    spatial = _spatial(index, max(count, 1), direction)
    if intensity == 5 and not _is_reverse(direction):
        return _alternating(index, phase, first, second, brightness)
    current, upcoming = _pair_for_half(spatial, phase, first, second)
    return _scale(_blend_hold(phase, current, upcoming, _hold_for(intensity)), brightness)


def _paint_alternate(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """The light's color and off, swapping between even and odd fixtures."""
    color = rgb if palette == PALETTE_CUSTOM else _sample(palette, 0.0, rgb, custom)
    if intensity == 5 and palette == PALETTE_CUSTOM and not _is_reverse(direction):
        current, upcoming = _pair_for_half(index, phase, rgb, (0, 0, 0))
        return _scale(_half_cycle_color(phase, current, upcoming), brightness)
    spatial = _spatial(index, max(count, 1), direction)
    current, upcoming = _pair_for_half(spatial, phase, color, (0, 0, 0))
    return _scale(_blend_hold(phase, current, upcoming, _hold_for(intensity)), brightness)


def _paint_christmas(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Red and green, swapping between even and odd fixtures."""
    return _paint_pair(
        index, count, phase, rgb, brightness, intensity, palette, direction, custom, RED, GREEN
    )


def _paint_halloween(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Orange and purple, swapping between even and odd fixtures."""
    return _paint_pair(
        index,
        count,
        phase,
        rgb,
        brightness,
        intensity,
        palette,
        direction,
        custom,
        ORANGE,
        PURPLE,
    )


def _paint_christmas_chase(
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: int,
    palette: str,
    direction: str,
    custom: tuple[Color, ...],
) -> bytes:
    """Red, green, and white sliding along the order."""
    del rgb, palette, custom
    width = 2 if intensity >= 8 else 1
    travel = _temporal(phase, direction)
    return _moving_stripe(index, count, travel, (RED, GREEN, WHITE), width, brightness)


_PAINTERS: dict[str, Painter] = {
    EFFECT_SOLID: _paint_solid,
    EFFECT_GRADIENT: _paint_gradient,
    EFFECT_BREATHE: _paint_breathe,
    EFFECT_FADE: _paint_fade,
    EFFECT_COLORLOOP: _paint_colorloop,
    EFFECT_HEARTBEAT: _paint_heartbeat,
    EFFECT_SUNRISE: _paint_sunrise,
    EFFECT_RAINBOW_ALL: _paint_rainbow,
    EFFECT_RAINBOW_CYCLE: _paint_rainbow_cycle,
    EFFECT_WIPE: _paint_wipe,
    EFFECT_SWEEP: _paint_sweep,
    EFFECT_THEATER: _paint_theater,
    EFFECT_RUNNING: _paint_running,
    EFFECT_CHASE: _paint_chase,
    EFFECT_SCANNER: _paint_scanner,
    EFFECT_DUAL_SCANNER: _paint_dual_scanner,
    EFFECT_METEOR: _paint_meteor,
    EFFECT_PALETTE_FLOW: _paint_flow,
    EFFECT_RIPPLE: _paint_ripple,
    EFFECT_TWINKLE: _paint_twinkle,
    EFFECT_TWINKLE_FADE: _paint_twinkle_fade,
    EFFECT_CANDLE: _paint_candle,
    EFFECT_FIREPLACE: _paint_fireplace,
    EFFECT_LIGHTNING: _paint_lightning,
    EFFECT_POLICE: _paint_police,
    EFFECT_ALTERNATE: _paint_alternate,
    EFFECT_CHRISTMAS: _paint_christmas,
    EFFECT_CHRISTMAS_CHASE: _paint_christmas_chase,
    EFFECT_HALLOWEEN: _paint_halloween,
}


def fixture_channels(
    effect: str,
    index: int,
    count: int,
    phase: float,
    rgb: Color,
    brightness: int,
    intensity: float = DEFAULT_EFFECT_INTENSITY,
    palette: str = DEFAULT_EFFECT_PALETTE,
    direction: str = DEFAULT_EFFECT_DIRECTION,
    custom: tuple[Color, ...] = (),
) -> bytes:
    """Channel bytes for one fixture. ``index`` is its place in the drawn order.

    Old effect names at the default intensity, palette, and direction return
    the 0.1.3 frame so a saved scene that still sends that name matches.
    """
    if _uses_legacy(effect, intensity, palette, direction):
        return _legacy_frame(effect, index, count, phase, rgb, brightness)
    name = resolve_effect(effect)
    painter = _PAINTERS.get(name, _paint_solid)
    return painter(
        index,
        max(count, 0),
        float(phase),
        rgb,
        brightness,
        clip_intensity(intensity),
        normalize_palette(palette),
        normalize_direction(direction),
        tuple(custom),
    )


@dataclass
class EffectRun:
    """One running effect and the fixtures it currently owns, in order."""

    effect: str
    order: list[str]
    rgb: Color
    brightness: int
    speed: float
    intensity: float = DEFAULT_EFFECT_INTENSITY
    palette: str = DEFAULT_EFFECT_PALETTE
    direction: str = DEFAULT_EFFECT_DIRECTION
    custom: tuple[Color, ...] = ()
    phase_origin: float = 0.0
    phase_speed: float = field(init=False)

    def __post_init__(self) -> None:
        """Remember which speed the phase origin was built for."""
        self.phase_speed = float(self.speed)
        self.intensity = float(clip_intensity(self.intensity))
        self.palette = normalize_palette(self.palette)
        self.direction = normalize_direction(self.direction)

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
        rgb: Color,
        brightness: int,
        speed: float,
        intensity: float = DEFAULT_EFFECT_INTENSITY,
        palette: str = DEFAULT_EFFECT_PALETTE,
        direction: str = DEFAULT_EFFECT_DIRECTION,
        custom: tuple[Color, ...] = (),
    ) -> None:
        """Give ``order`` to ``owner``. Those fixtures leave any previous effect.

        Starting the same effect on the same fixtures keeps the cycle position.
        Intensity, palette, direction, color, and speed can change without a
        jump back to the start of the cycle. A different fixture order starts
        at the beginning.
        """
        effect = resolve_effect(effect)
        if effect == EFFECT_SOLID or effect not in EFFECT_NAMES:
            self.stop(owner)
            return
        previous = self.runs.get(owner)
        kept_origin = None
        kept_speed = None
        self._detach_owner(owner)
        claimed: list[str] = []
        seen: set[str] = set()
        for key in order:
            if key in seen:
                continue
            seen.add(key)
            prior = self.owners.get(key)
            if prior is not None and prior != owner:
                self._drop_key(prior, key)
            self.owners[key] = owner
            claimed.append(key)
        if not claimed:
            self.runs.pop(owner, None)
            return
        if previous is not None and previous.effect == effect and previous.order == claimed:
            kept_origin = previous.phase_origin
            kept_speed = previous.phase_speed
        run = EffectRun(
            effect=effect,
            order=claimed,
            rgb=rgb,
            brightness=brightness,
            speed=speed,
            intensity=intensity,
            palette=palette,
            direction=direction,
            custom=tuple(custom),
        )
        if kept_origin is not None and kept_speed is not None:
            run.phase_origin = kept_origin
            run.phase_speed = kept_speed
        self.runs[owner] = run

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
        *,
        syncs: dict | None = None,
    ) -> dict[str, list[tuple[int, bytes]]]:
        """Channel writes grouped by host. Unavailable fixtures are skipped.

        ``syncs`` maps an owner to ``(placement, phase)``. ``placement(key)``
        returns ``(index, count)`` in a combined show, and ``phase`` is the
        shared cycle position. Owners that are absent keep the local index
        and ``phase_at``, which is the single-hub behavior.
        """
        grouped: dict[str, list[tuple[int, bytes]]] = {}
        shared = syncs or {}
        for owner, run in self.runs.items():
            drawn = [key for key in run.order if available(key)]
            sync = shared.get(owner)
            if sync is None:
                run_phase = run.phase_at(now)
                slots = [(index, len(drawn), key) for index, key in enumerate(drawn)]
            else:
                placement, run_phase = sync
                slots = []
                for key in drawn:
                    found = placement(key)
                    if found is None:
                        continue
                    index, count = found
                    slots.append((int(index), int(count), key))
            for index, count, key in slots:
                located = dmx_of(key)
                if located is None:
                    continue
                host, start = located
                payload = fixture_channels(
                    run.effect,
                    index,
                    count,
                    run_phase,
                    run.rgb,
                    run.brightness,
                    run.intensity,
                    run.palette,
                    run.direction,
                    run.custom,
                )
                grouped.setdefault(host, []).append((start, payload))
        return grouped
