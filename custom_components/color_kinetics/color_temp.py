"""Kelvin to RGB for the tunable-white slider.

Tanner Helland's blackbody approximation, then scaled so the brightest
channel is 255. Brightness is applied later, when the light builds DMX bytes.
"""

from __future__ import annotations

import math

from .const import MAX_COLOR_TEMP_KELVIN, MIN_COLOR_TEMP_KELVIN


def clamp_kelvin(value: float) -> int:
    """Clamp a color temperature to the slider range, 2000 K to 6500 K."""
    try:
        kelvin = int(round(float(value)))
    except (TypeError, ValueError):
        return MIN_COLOR_TEMP_KELVIN
    return max(MIN_COLOR_TEMP_KELVIN, min(MAX_COLOR_TEMP_KELVIN, kelvin))


def kelvin_to_rgb(kelvin: float) -> tuple[int, int, int]:
    """RGB for ``kelvin``. The brightest channel is 255.

    Temperatures outside 1000 K to 40000 K are pulled in before the formula.
    The light still clamps the stored kelvin to 2000 K–6500 K.
    """
    temp = max(1000.0, min(40000.0, float(kelvin))) / 100.0
    if temp <= 66.0:
        red = 255.0
        green = 99.4708025861 * math.log(temp) - 161.1195681661
        if temp <= 19.0:
            blue = 0.0
        else:
            blue = 138.5177312231 * math.log(temp - 10.0) - 305.0447927307
    else:
        red = 329.698727446 * ((temp - 60.0) ** -0.1332047592)
        green = 288.1221695283 * ((temp - 60.0) ** -0.0755148492)
        blue = 255.0
    channels = [max(0.0, min(255.0, channel)) for channel in (red, green, blue)]
    peak = max(channels)
    if peak <= 0.0:
        return (0, 0, 0)
    scale = 255.0 / peak
    return tuple(int(round(channel * scale)) for channel in channels)  # type: ignore[return-value]
