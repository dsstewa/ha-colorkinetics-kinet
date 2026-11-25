"""Confirmed KiNET property ids.

Labels here are the ones whose captured value matches QuickPlay. Anything
else the capture contains is left unlabelled. See docs/PROPERTIES.md.
"""

from __future__ import annotations

import struct

TYPE_BLAST = 0x18
TYPE_BURST = 0x04

MODEL_BY_TYPE = {
    TYPE_BLAST: "Blast PowerCore gen4 3-Ch",
    TYPE_BURST: "iW/iColor Burst Powercore",
}

# Read on every discovered fixture. 0x6e is unlabelled; it is still read so
# the raw sensor can show it.
COMMON_FIXTURE_PROPS = (0x40, 0x41, 0x43, 0x44, 0x45, 0x6E, 0x05)
# Read only after 0x05 says this is a gen4. Burst fixtures do not answer them.
GEN4_FIXTURE_PROPS = (0xC0, 0x6D, 0xCC, 0xC2, 0x4F, 0x60, 0x61, 0x62)

SUPPLY_PROPS = (0x0052, 0x0051, 0x044D, 0x044E)
SUPPLY_POWER_TIME = 0x0052
RAW_SUPPLY_PROPS = (0x0051, 0x044D, 0x044E)

STARTUP_PROPS = (0x43, 0x44, 0x45)
SCALE_PROPS = (0x60, 0x61, 0x62)
RAW_FIXTURE_PROPS = (0x6E, 0xC0, 0x6D, 0x4F, 0xCC, 0xC2)

SUFFIX_ALL_FIXTURES = "all_fixtures"
SUFFIX_POWER_TIME = "power_time"
SUFFIX_RAW = "raw_properties"
SUFFIX_DMX = "dmx_address"
SUFFIX_STARTUP = "startup_channels"
SUFFIX_SCALE = "scale_factors"
SUFFIX_EFFECT_SPEED = "effect_speed"
SUFFIX_EFFECT_INTENSITY = "effect_intensity"
SUFFIX_EFFECT_PALETTE = "effect_palette"
SUFFIX_EFFECT_DIRECTION = "effect_direction"
SUFFIX_DEFAULT_FADE = "default_fade"
SUFFIX_LOOK = "look"
SUFFIX_PANEL_EFFECT = "panel_effect"
SUFFIX_PANEL_SCENE = "panel_scene"
SUFFIX_PANEL_TARGET = "panel_target"
SUFFIX_PANEL_BRIGHTNESS = "brightness"
SUFFIX_STOP_EFFECT = "stop_effect"
SUFFIX_ALL_OFF = "all_off"


def supply_serial_hex(serial: bytes) -> str:
    """QuickPlay's serial is the first four bytes as a little-endian word."""
    if len(serial) < 4:
        return serial.hex().upper()
    return f"{struct.unpack_from('<I', serial)[0]:08X}"


def canonical_supply_serial(serial_hex: str) -> str:
    """Return the 8-hex supply serial, including the 0.1.0 16-hex form.

    0.1.0 stored the eight raw poll bytes (``83EB007F00000000``). The
    QuickPlay serial is the first four of those bytes, little-endian
    (``7F00EB83``). An 8-character value is already canonical, which is
    also why fixture serials are not rewritten.
    """
    text = serial_hex.strip().upper()
    if len(text) == 16 and all(char in "0123456789ABCDEF" for char in text):
        return supply_serial_hex(bytes.fromhex(text))
    return text


def model_name(type_code: int | None) -> str:
    """Product name for a fixture type code. Unknown codes stay generic."""
    if type_code is None:
        return "Color Kinetics fixture"
    return MODEL_BY_TYPE.get(type_code, f"Type 0x{type_code:02X}")


def power_time_text(value: bytes) -> str:
    """0x0052 is a timeout in seconds. 300 is QuickPlay's 5 minutes."""
    seconds = int.from_bytes(value[:4], "little")
    if seconds % 60 == 0:
        minutes = seconds // 60
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    return f"{seconds} seconds"


def channel_triple(props: dict[int, int], ids: tuple[int, ...]) -> str | None:
    """Format three channel properties. Missing bytes read as unknown."""
    if not any(prop in props for prop in ids):
        return None
    parts = [str(props[prop]) if prop in props else "unknown" for prop in ids]
    return " / ".join(parts)


def raw_property_key(prop: int) -> str:
    """Attribute name for an unlabelled property."""
    return f"property_0x{prop:04x}"
