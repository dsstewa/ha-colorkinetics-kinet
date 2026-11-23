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

PROTOCOL_PORTOUT: Final = "portout"
PROTOCOL_DMXOUT: Final = "dmxout"

DEFAULT_PROTOCOL: Final = PROTOCOL_PORTOUT
DEFAULT_PORTOUT_VERSION: Final = 1
DEFAULT_OUTPUT_PORT: Final = 1

CHANNELS_PER_FIXTURE: Final = 3
MAX_FPS: Final = 40
KEEPALIVE_SECONDS: Final = 1.0

PLATFORMS: Final = ["light", "button", "sensor"]
