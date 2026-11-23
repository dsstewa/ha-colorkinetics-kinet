# Changelog

## 0.1.1

- Add an **All fixtures** light on each Data Enabler. One frame sets every available discovered fixture.
- Read confirmed KiNET properties during setup and rescan, and show them as diagnostic sensors. Unconfirmed ids stay on a Raw properties sensor.
- Show the Data Enabler serial as the little-endian word `7F00EB83`. An entry created by 0.1.0 is migrated in place, so the supply device, the Rescan button, and fixture devices keep their ids.
- Name fixtures from the type code: Blast PowerCore gen4 3-Ch and iW/iColor Burst Powercore. Fixture firmware is not in the capture, so `sw_version` is left unset.
- Fixture order and named groups in the options flow. All fixtures and each group light can run Solid, Color wheel, Rainbow chase, Color chase, Theater chase, Alternate, or Breathe. Effect speed is a 1–10 slider. The latest command owns a fixture.

## 0.1.0

First release of the Philips Color Kinetics KiNET integration for Home Assistant.

- Add a Data Enabler by IP. Each discovered fixture is one RGB light, keyed by serial.
- Rescan from a button. A fixture becomes unavailable only after two scans miss it, and its last color is left on the wire.
- Options: KiNET v1 DMXOUT or v2 PORTOUT, PORTOUT header version, output port, and manual fixtures.
- Outgoing packets are allow-listed. Configuration writes and broadcast destinations, including a last octet of 0 or 255, are refused.
