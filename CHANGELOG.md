# Changelog

## 0.1.0

First release of the Philips Color Kinetics KiNET integration for Home Assistant.

- Add a Data Enabler by IP. Each discovered fixture is one RGB light, keyed by serial.
- Rescan from a button. A fixture becomes unavailable only after two scans miss it, and its last color is left on the wire.
- Options: KiNET v1 DMXOUT or v2 PORTOUT, PORTOUT header version, output port, and manual fixtures.
- Outgoing packets are allow-listed. Configuration writes and broadcast destinations, including a last octet of 0 or 255, are refused.
