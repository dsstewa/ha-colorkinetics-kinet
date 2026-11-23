# Changelog

## 0.1.2

- Holiday effects on All fixtures and every group light: Christmas, Warm twinkle, Candy cane, Red white blue, and Halloween. They follow fixture order and the 1–10 speed slider. The latest command still owns a fixture.
- Save the current colors, brightness, and any running effect and speed as a named look. Recall it from the Look selector on the Data Enabler, or with `color_kinetics.save_look`, `recall_look`, and `delete_look`. Looks are stored by serial in the existing cache, and a 0.1.1 cache is migrated in place. Duplicate names are rejected regardless of case. A fixture that is gone is skipped.
- Transitions on fixture, group, and All fixtures lights, and on `recall_look`. The fade runs in the existing frame loop. A newer command cancels the fade for the fixtures it takes. An animated effect with a transition fades into its first frame, then runs.
- Optional blueprint `blueprints/automation/color_kinetics/sunset_to_bedtime.yaml`. It is not installed automatically. Sunset can name a Data Enabler, and bedtime fades off the lights you choose for the look as well as the effect light.
- A fixture owned by a running effect is on, at that effect's brightness, so saving and recalling the look does not fade it to black. The Look selector keeps its value across a restart. A fixture device passed as `device_id` selects that fixture's Data Enabler.

## 0.1.1

- Add an **All fixtures** light on each Data Enabler. One frame sets every available discovered fixture.
- Read confirmed KiNET properties during setup and rescan, and show them as diagnostic sensors. Unconfirmed ids stay on a Raw properties sensor.
- Show the Data Enabler serial as the little-endian word `7F00EB83`. An entry created by 0.1.0 is migrated in place, so the supply device, the Rescan button, and fixture devices keep their ids.
- Name fixtures from the type code: Blast PowerCore gen4 3-Ch and iW/iColor Burst Powercore. Fixture firmware is not in the capture, so `sw_version` is left unset.
- Fixture order and named groups in the options flow. All fixtures and each group light can run Solid, Color wheel, Rainbow chase, Color chase, Theater chase, Alternate, or Breathe. Effect speed is a 1–10 slider. The latest command owns a fixture.
- An unavailable fixture keeps the last color that was sent to it, including across a reload and in the middle of a later frame. A fixture that has never been set is off, so those slots are zero. Every light frame is at least 24 bytes and covers the addressed fixtures.
- A timed-out DMX address read keeps the previous address. A group member with no light entity stays after the fixture that preceded it. A group that loses every fixture reports off, and turning it off does not black out fixtures another effect owns.
- Deleting the config entry removes the stored fixture colors.

## 0.1.0

First release of the Philips Color Kinetics KiNET integration for Home Assistant.

- Add a Data Enabler by IP. Each discovered fixture is one RGB light, keyed by serial.
- Rescan from a button. A fixture becomes unavailable only after two scans miss it, and its last color is left on the wire.
- Options: KiNET v1 DMXOUT or v2 PORTOUT, PORTOUT header version, output port, and manual fixtures.
- Outgoing packets are allow-listed. Configuration writes and broadcast destinations, including a last octet of 0 or 255, are refused.
