"""Per-hub scenes and the control-panel state stored beside them.

A scene is a name plus a color for each fixture. Built-in scenes match the
looks already used on this rig: one purple, alternating red and green, blue,
warm white, and orange. The options flow can replace any of them with
explicit per-fixture colors. Applying a scene is the hub's job; this module
only stores and resolves the colors.

The panel record is the selected effect, scene, target, and brightness.
Entities read it. They do not keep a second copy.
"""

from __future__ import annotations

from typing import Any

from .effects import EFFECT_NAMES, resolve_effect

EFFECT_NONE = "None"
TARGET_ALL = "all"
TARGET_ALL_LABEL = "All fixtures"

MODE_IDLE = "idle"
MODE_EFFECT = "effect"
MODE_SCENE = "scene"
MODE_OFF = "off"

PATTERN_SOLID = "solid"
PATTERN_ALTERNATE = "alternate"
PATTERN_FIXTURES = "fixtures"
PATTERNS = (PATTERN_SOLID, PATTERN_ALTERNATE, PATTERN_FIXTURES)

SCENE_HALLOWEEN = "Halloween Purple"
SCENE_CHRISTMAS = "Christmas Red Green"
SCENE_BLUE = "All Blue"
SCENE_CAMP = "Camp"
SCENE_ORANGE = "All Orange"

# Camp uses the warm white already painted by the effect palettes.
# All Orange uses the same orange as the Halloween palette.
HALLOWEEN_PURPLE = (238, 0, 255)
CHRISTMAS_RED = (255, 0, 0)
CHRISTMAS_GREEN = (0, 255, 0)
ALL_BLUE = (0, 0, 255)
CAMP_WHITE = (255, 180, 80)
ALL_ORANGE = (255, 80, 0)

SCENE_NAMES = (
    SCENE_HALLOWEEN,
    SCENE_CHRISTMAS,
    SCENE_BLUE,
    SCENE_CAMP,
    SCENE_ORANGE,
)


def effect_options() -> list[str]:
    """None, then every effect the light dropdown already offers.

    None stops the animation and leaves the current channel bytes. Solid is
    still in the list because it is a real effect: it paints one color.
    """
    return [EFFECT_NONE, *EFFECT_NAMES]


def normalize_effect_option(value: str | None) -> str:
    """A panel effect name. Empty and unknown values are None."""
    text = str(value or "").strip()
    if not text or text.casefold() in {"none", "off", "stop"}:
        return EFFECT_NONE
    resolved = resolve_effect(text)
    if resolved in EFFECT_NAMES:
        return resolved
    if text in EFFECT_NAMES:
        return text
    return EFFECT_NONE


def clamp_channel(value: Any) -> int | None:
    """One RGB channel, 0 through 255. Unusable input is None."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 255:
        return None
    return number


def clamp_percent(value: Any, fallback: int = 100) -> int:
    """Brightness percent, 0 through 100."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number <= 0:
        return 0
    if number >= 100:
        return 100
    return int(round(number))


def percent_to_brightness(percent: Any) -> int:
    """Map a 0–100 panel level onto Home Assistant's 0–255 brightness.

    100 percent is 255, so a full scene is the same bytes as a full light.
    """
    level = clamp_percent(percent, fallback=0)
    return int(round(level * 255 / 100))


def _rgb_list(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    channels = [clamp_channel(channel) for channel in value]
    if any(channel is None for channel in channels):
        return None
    return [int(channel) for channel in channels if channel is not None]


def solid_scene(rgb: tuple[int, int, int], brightness: int = 100) -> dict[str, Any]:
    """Every fixture shows one color."""
    return {
        "pattern": PATTERN_SOLID,
        "rgb": [int(channel) for channel in rgb],
        "brightness": clamp_percent(brightness),
        "fixtures": {},
    }


def alternate_scene(
    first: tuple[int, int, int],
    second: tuple[int, int, int],
    brightness: int = 100,
) -> dict[str, Any]:
    """Even fixture indexes take ``first``. The next fixture takes ``second``."""
    return {
        "pattern": PATTERN_ALTERNATE,
        "colors": [
            [int(channel) for channel in first],
            [int(channel) for channel in second],
        ],
        "brightness": clamp_percent(brightness),
        "fixtures": {},
    }


def default_scenes() -> dict[str, dict[str, Any]]:
    """The five built-in scenes, keyed by the name shown in the selector."""
    return {
        SCENE_HALLOWEEN: solid_scene(HALLOWEEN_PURPLE),
        SCENE_CHRISTMAS: alternate_scene(CHRISTMAS_RED, CHRISTMAS_GREEN),
        SCENE_BLUE: solid_scene(ALL_BLUE),
        SCENE_CAMP: solid_scene(CAMP_WHITE),
        SCENE_ORANGE: solid_scene(ALL_ORANGE),
    }


def _normalize_fixtures(raw: Any) -> dict[str, dict[str, Any]]:
    fixtures: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return fixtures
    for serial, item in raw.items():
        if not isinstance(item, dict):
            continue
        rgb = _rgb_list(item.get("rgb"))
        if rgb is None:
            continue
        fixtures[str(serial)] = {
            "rgb": rgb,
            "brightness": clamp_percent(item.get("brightness"), fallback=100),
        }
    return fixtures


def normalize_scene(raw: Any) -> dict[str, Any] | None:
    """A stored scene, or None when the record has no usable color."""
    if not isinstance(raw, dict):
        return None
    pattern = str(raw.get("pattern") or PATTERN_FIXTURES)
    if pattern not in PATTERNS:
        pattern = PATTERN_FIXTURES
    brightness = clamp_percent(raw.get("brightness"), fallback=100)
    fixtures = _normalize_fixtures(raw.get("fixtures"))
    scene: dict[str, Any] = {
        "pattern": pattern,
        "brightness": brightness,
        "fixtures": fixtures,
    }
    if pattern == PATTERN_SOLID:
        rgb = _rgb_list(raw.get("rgb"))
        if rgb is None and not fixtures:
            return None
        if rgb is not None:
            scene["rgb"] = rgb
    elif pattern == PATTERN_ALTERNATE:
        colors = raw.get("colors")
        parsed: list[list[int]] = []
        if isinstance(colors, list):
            for color in colors:
                rgb = _rgb_list(color)
                if rgb is not None:
                    parsed.append(rgb)
        if len(parsed) < 2 and not fixtures:
            return None
        if parsed:
            scene["colors"] = parsed
    elif not fixtures:
        return None
    return scene


def normalize_scene_map(raw: Any) -> dict[str, dict[str, Any]]:
    """Drop scene records that do not resolve to a color."""
    scenes: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return scenes
    for name, item in raw.items():
        clean = str(name).strip()
        scene = normalize_scene(item)
        if clean and scene is not None:
            scenes[clean] = scene
    return scenes


def find_scene_name(scenes: dict[str, Any], name: str) -> str | None:
    """Stored scene name matching ``name``, ignoring case."""
    folded = str(name).strip().casefold()
    if not folded:
        return None
    for existing in scenes:
        if str(existing).casefold() == folded:
            return str(existing)
    return None


def resolve_scene(
    scene: dict[str, Any],
    keys: list[str],
    *,
    offset: int = 0,
) -> list[tuple[str, tuple[int, int, int], int]]:
    """Colors for ``keys`` in order.

    ``offset`` is how many fixtures sit ahead of this hub in a combined show,
    so an alternating scene continues the stripe instead of starting over.
    A serial listed under ``fixtures`` wins over the pattern. Brightness is a
    percent. Keys with no color are omitted.
    """
    pattern = str(scene.get("pattern") or PATTERN_FIXTURES)
    brightness = clamp_percent(scene.get("brightness"), fallback=100)
    overrides = scene.get("fixtures") if isinstance(scene.get("fixtures"), dict) else {}
    colors = scene.get("colors") if isinstance(scene.get("colors"), list) else []
    solid = _rgb_list(scene.get("rgb"))
    assigned: list[tuple[str, tuple[int, int, int], int]] = []
    for index, key in enumerate(keys):
        override = overrides.get(key) if isinstance(overrides, dict) else None
        if isinstance(override, dict) and _rgb_list(override.get("rgb")) is not None:
            rgb = _rgb_list(override.get("rgb"))
            level = clamp_percent(override.get("brightness"), fallback=brightness)
            assigned.append((key, (rgb[0], rgb[1], rgb[2]), level))  # type: ignore[index]
            continue
        if pattern == PATTERN_SOLID and solid is not None:
            assigned.append((key, (solid[0], solid[1], solid[2]), brightness))
            continue
        if pattern == PATTERN_ALTERNATE and colors:
            choice = colors[(offset + index) % len(colors)]
            rgb = _rgb_list(choice)
            if rgb is None:
                continue
            assigned.append((key, (rgb[0], rgb[1], rgb[2]), brightness))
    return assigned


def parse_color_text(text: str) -> tuple[int, int, int] | None:
    """Parse ``r, g, b``. Blank input is None so an optional field can be empty."""
    raw = str(text or "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.replace("/", ",").split(",")]
    if len(parts) != 3:
        return None
    channels = [clamp_channel(part) for part in parts]
    if any(channel is None for channel in channels):
        return None
    return (int(channels[0]), int(channels[1]), int(channels[2]))  # type: ignore[arg-type]


def parse_fixture_lines(text: str) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Parse ``serial, r, g, b[, brightness]`` lines.

    Blank lines are ignored. The error string names the first bad line.
    """
    fixtures: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(str(text or "").splitlines(), start=1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) not in (4, 5) or not parts[0]:
            return {}, f"Line {line_number} must be serial, r, g, b, brightness."
        rgb = [clamp_channel(part) for part in parts[1:4]]
        if any(channel is None for channel in rgb):
            return {}, f"Line {line_number} has a color outside 0-255."
        brightness = 100
        if len(parts) == 5:
            token = parts[4].strip()
            if not token.lstrip("-").isdigit():
                return {}, f"Line {line_number} brightness must be 0-100."
            brightness = int(token)
            if brightness < 0 or brightness > 100:
                return {}, f"Line {line_number} brightness must be 0-100."
        fixtures[parts[0]] = {
            "rgb": [int(channel) for channel in rgb if channel is not None],
            "brightness": brightness,
        }
    return fixtures, None


def scene_fixture_text(scene: dict[str, Any], keys: list[str]) -> str:
    """One line per known fixture, so the options form can edit the colors."""
    lines: list[str] = []
    resolved = {key: (rgb, level) for key, rgb, level in resolve_scene(scene, keys)}
    ordered = list(keys)
    for serial in scene.get("fixtures") or {}:
        if serial not in ordered:
            ordered.append(str(serial))
    for key in ordered:
        found = resolved.get(key)
        if found is None:
            continue
        rgb, level = found
        lines.append(f"{key}, {rgb[0]}, {rgb[1]}, {rgb[2]}, {level}")
    return "\n".join(lines)


def default_panel() -> dict[str, Any]:
    """Idle panel. Nothing is running and the target is every fixture."""
    return {
        "effect": EFFECT_NONE,
        "scene": None,
        "target": TARGET_ALL,
        "brightness": 100,
        "mode": MODE_IDLE,
        "show_origin": None,
        "show_speed": None,
        "show_adopted": False,
    }


def normalize_panel(raw: Any) -> dict[str, Any]:
    """Fill a stored panel. Unknown modes become idle."""
    panel = default_panel()
    if not isinstance(raw, dict):
        return panel
    panel["effect"] = normalize_effect_option(raw.get("effect"))
    scene = raw.get("scene")
    panel["scene"] = str(scene).strip() if isinstance(scene, str) and scene.strip() else None
    target = raw.get("target")
    panel["target"] = str(target).strip() if isinstance(target, str) and target.strip() else TARGET_ALL
    panel["brightness"] = clamp_percent(raw.get("brightness"), fallback=100)
    mode = str(raw.get("mode") or MODE_IDLE)
    if mode not in (MODE_IDLE, MODE_EFFECT, MODE_SCENE, MODE_OFF):
        mode = MODE_IDLE
    if mode == MODE_EFFECT and panel["effect"] == EFFECT_NONE:
        mode = MODE_IDLE
    if mode == MODE_SCENE and not panel["scene"]:
        mode = MODE_IDLE
    panel["mode"] = mode
    panel["show_adopted"] = bool(raw.get("show_adopted"))
    try:
        origin = float(raw["show_origin"]) if raw.get("show_origin") is not None else None
    except (TypeError, ValueError):
        origin = None
    try:
        speed = float(raw["show_speed"]) if raw.get("show_speed") is not None else None
    except (TypeError, ValueError):
        speed = None
    panel["show_origin"] = origin
    panel["show_speed"] = speed
    return panel
