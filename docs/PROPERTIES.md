# KiNET property map

Decoded from `tests/fixtures/kinet_packets.json` (QuickPlay Pro 2.2.4 on this Data Enabler) and checked against the QuickPlay screens. The integration labels a field only when the captured value matches that screen. Everything else that was read is shown as `property_0xNNNN` on the **Raw properties** sensor.

Reads use the existing allow-list: supply `0x0105` and fixture `0x0203`. They run during setup and rescan, not on a poll. A timeout or a bad status leaves that one field unknown and does not drop the fixture.

## Supply serial

The poll reply (`0x0002`) carries eight serial bytes `83 eb 00 7f 00 00 00 00`. QuickPlay shows `7F00EB83`, which is the first four bytes as a little-endian word (`0x7F00EB83`). That is the serial stored on the device. Name, IP, MAC, and firmware `SFT-000155-00 V34` come from the same poll reply.

## Confirmed

| Id | Where | Captured value | QuickPlay |
|---|---|---|---|
| `0x40`, `0x41` | Fixture | High and low DMX byte. `0x41` is 0, 3, 6, 9, 12, 15, 18, 21 | DMX address 1, 4, 7, 10, 13, 16, 19, 22 (1-based) |
| `0x43`, `0x44`, `0x45` | Both fixture types | 0, 0, 0 | Startup channel 1 / 2 / 3 |
| `0x60`, `0x61`, `0x62` | Type `0x18` only | 255, 255, 255 | Channel 1 / 2 / 3 scale factor. Not read for type `0x04`, and QuickPlay does not show scale factors there |
| `0x05` | Fixture | `0x18` or `0x04` | Device model: `0x18` Blast PowerCore gen4 3-Ch, `0x04` iW/iColor Burst Powercore |
| `0x0052` | Supply | `2c 01 00 00` = 300 seconds | Power time 5 minutes |

## Ambiguous

These were read and the value is stable on this rig, but more than one QuickPlay label could fit. They are not named.

| Id | Captured value | Why it is unlabelled |
|---|---|---|
| Fixture `0x6e` | 1 on both types | Could be resolution (8 bit) or LED chemistry (RGB). Both types show both labels |
| Fixture `0xc0` | 4, gen4 only | Candidate for dimming curve, Chromasync, LED speed, or livescan |
| Fixture `0x6d` | 1, gen4 only | Same set of candidates |
| Fixture `0x4f` | 1, gen4 only | Same set of candidates |
| Fixture `0xcc` | 0, gen4 only | Same set of candidates |
| Fixture `0xc2` | 0, gen4 only | Same set of candidates |
| Supply `0x0051` | 32 (`20 00 00 00`) | Candidate for power mode or KiNET universe. Universe on this screen is 0, so the word itself is not the universe |
| Supply `0x044d` | `ff ff 01 80` | Candidate for KiNET or DMX input routing |
| Supply `0x044e` | `ff ff 02 80` | The other routing candidate. The two replies differ, and the screen has two routing lines, but nothing in the value picks which is which |

## Not in the capture

The fixture firmware string (`SFT-000226-00 V24`) is not in any reply. Fixture devices leave `sw_version` unset. The supply firmware above is the poll reply, and it is `sw_version` on the Data Enabler device.

Gen4-only properties are requested only after `0x05` comes back as `0x18`, so a Burst fixture is not asked for scale factors it does not have.
