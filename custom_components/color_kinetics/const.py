"""Constants for the Philips Color Kinetics integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "color_kinetics"
MANUFACTURER: Final = "Philips Color Kinetics"

CONF_HOSTS: Final = "hosts"
CONF_SUPPLIES: Final = "supplies"
CONF_PROTOCOL: Final = "protocol"
CONF_PORTOUT_VERSION: Final = "portout_version"
CONF_OUTPUT_PORT: Final = "output_port"
CONF_MANUAL_FIXTURES: Final = "manual_fixtures"
CONF_FIXTURE_ORDER: Final = "fixture_order"
CONF_GROUPS: Final = "groups"
CONF_DEFAULT_FADE: Final = "default_fade"
CONF_FOLLOW_ENTRY: Final = "follow_entry"

PROTOCOL_PORTOUT: Final = "portout"
PROTOCOL_DMXOUT: Final = "dmxout"

DEFAULT_PROTOCOL: Final = PROTOCOL_PORTOUT
DEFAULT_PORTOUT_VERSION: Final = 1
DEFAULT_OUTPUT_PORT: Final = 1

CHANNELS_PER_FIXTURE: Final = 3
MAX_FPS: Final = 40
KEEPALIVE_SECONDS: Final = 1.0

DEFAULT_EFFECT_SPEED: Final = 5
DEFAULT_EFFECT_INTENSITY: Final = 5
DEFAULT_EFFECT_PALETTE: Final = "Custom"
DEFAULT_EFFECT_DIRECTION: Final = "Forward"

DEFAULT_FADE_SECONDS: Final = 1.5
MIN_COLOR_TEMP_KELVIN: Final = 2000
MAX_COLOR_TEMP_KELVIN: Final = 6500

PLATFORMS: Final = ["light", "button", "sensor", "number", "select"]
