# Philips Color Kinetics for Home Assistant

A [HACS](https://hacs.xyz/) custom integration that drives Philips Color Kinetics fixtures **directly over KiNET**. There is no WLED and no OLA in the path.

It speaks UDP to a Data Enabler (David's is a Data Enabler Pro at `172.16.10.41`). Each fixture becomes one Home Assistant light with RGB and brightness. Discovery reads serial numbers and DMX start addresses; it does not configure anything.

## What it will and will not send

Outgoing datagrams are checked against an allow-list before they hit the socket:

| Allowed | Type |
|---|---|
| Supply poll | `0x0001` |
| Port list | `0x000a` |
| Supply property read | `0x0105` (setup and rescan only) |
| Fixture property read | `0x0203` |
| Fixture scan | `0x0207` |
| Light data | `0x0101` DMXOUT or `0x0108` PORTOUT |

Set-IP (`0x0003`), set-universe (`0x0005`), set-name (`0x0006`), write-properties (`0x0103`), sync/discover-v2 (`0x0109`), and every other type are rejected in code. Light data is **unicast only**. Limited broadcast, multicast, and addresses whose last octet is 0 or 255 (a directed broadcast on a normal LAN) are rejected.

## Install (HACS custom repository)

1. In Home Assistant: **HACS → Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/dsstewa/ha-colorkinetics-kinet` and choose category **Integration**.
3. Download **Philips Color Kinetics** and restart Home Assistant.
4. **Settings → Devices & services → Add Integration → Philips Color Kinetics**.
5. Enter one IPv4 address per line. The first one is `172.16.10.41`. Add another line later, or add another integration entry if that supply needs a different KiNET mode.

The dialog sends one read-only supply poll per address (UDP port 6038). It does not scan fixtures until the entry is created.

## What you get

- One device per Data Enabler (model, firmware, MAC, serial `7F00EB83`) and a **Rescan fixtures** button on it.
- An **All fixtures** light on that device. It sets every available discovered fixture to the same color in one frame, in the saved fixture order.
- Named groups (Tree Run, Oaks, and so on) from the options flow. Each group is its own light, with a stable id, and follows its own fixture order.
- One device and one `light` per fixture, keyed by serial (`880055BE`, …). Renames and areas stick across restarts and rescans.
- RGB color and brightness (`ColorMode.RGB`). State is optimistic and restored after a restart.
- Fixture type `0x18` is **Blast PowerCore gen4 3-Ch**. Type `0x04` is **iW/iColor Burst Powercore**. A fixture that does not answer a property is still created. The DMX address needs both bytes (`0x40` and `0x41`) with status `0x09`. A missing or rejected high byte leaves the light unavailable instead of assuming the address is under 256.
- Diagnostic sensors for the fields QuickPlay's values confirmed: DMX address, startup channels, gen4 scale factors, and the supply power time. Other captured properties sit on **Raw properties** without a guessed name. See `docs/PROPERTIES.md`.
- A fixture has to be missing from two scans in a row before it becomes unavailable. Its last channel values stay in the frame. It is not deleted.
- Light frames go out on change, at most 40 per second, and the last frame is repeated about once a second so the Data Enabler keeps holding the color.

A scan takes about 14 seconds. It runs once per supply that has never completed a scan, and when you press the button. It is not polled.

## Fixture order, groups, and effects

Open **Configure** on the integration entry.

- **Light output** is the host, protocol, and manual-fixture form.
- **Fixture order** is a drag-and-drop list of the discovered fixture lights (`reorder` on an entity selector). The saved value is the serial list. Renames do not change it. A fixture found later is added at the end, sorted by DMX address. A fixture that drops off a scan keeps its place in the list.
- **Groups** adds, renames, reorders, or deletes a named group. Deleting a group removes its light. The group's id does not change when you rename it.

All fixtures and each group light accept an effect:

| Effect | What it does |
|---|---|
| Solid | One color. This also stops an effect on that light. |
| Color wheel | Every fixture in the run cycles hue together. |
| Rainbow chase | Same cycle, with a hue offset per fixture along the order. |
| Color chase | The light's color runs along the order; the other fixtures in the run go dark. |
| Theater chase | Every third fixture, stepping along the order. |
| Alternate | Even and odd fixtures swap. |
| Breathe | The light's color fades in and out together. |

**Effect speed** is a slider on the same device, from 1 to 10. One cycle takes `6 / speed` seconds (6 s at 1, 0.6 s at 10). Effects are drawn by the existing frame loop, at most 40 times a second, and the keepalive still applies when nothing is animating. Brightness scales the effect. Unavailable fixtures are left out of the pattern and are not written, so a missed fixture is not zeroed.

A fixture is in at most one effect. The latest command wins: starting an effect takes those fixtures away from any effect that had them. Turning a member on, or setting Solid, drops that fixture out of the effect and leaves it on the solid color. The effect continues on whatever fixtures it still owns. A group that has lost every fixture reports off with Solid, and turning that group off does not write fixtures another effect still owns. The last color sent to each fixture is stored and restored before the frame pump starts, so a reload does not black out an unavailable fixture. A PORTOUT frame is not zero-padded across a fixture that has never been set.

## Options

Open the integration's **Configure** dialog:

| Option | Default | What to change |
|---|---|---|
| Addresses | the ones you entered | One IPv4 address per line. A new address is polled before it is saved. |
| Light protocol | KiNET v2 PORTOUT | Switch to KiNET v1 DMXOUT if PORTOUT does nothing. |
| PORTOUT header version | 1 (`01 00`, OLA) | Try 2 (`02 00`, Haukcode) if the lights stay dark on v2. Ignored for DMXOUT. |
| Output port | 1 | This Data Enabler Pro has a single output, port 1. Ignored for DMXOUT. |
| Manual fixtures | empty | Used when discovery misses a fixture. |

Manual line format, DMX start **1-based** (the number QuickPlay shows):

```text
880055BE, 1, Oak left
Porch wash, 25
```

With more than one supply, start the line with that supply's IP. An 8-digit hex token is a serial and is the stable key. Anything else is a label, and the label is the key. Prefer the serial when you know it.

These output settings apply to every supply in the entry. Use a second integration entry for a supply that needs a different mode.

## Safe test plan

Do this only when you are next to the lights. The integration does not contact a device by itself in CI or in unit tests.

1. **Pause OLA first.** Two KiNET senders fight, and the lights flicker. Stop OLA's KiNET output (or unload that universe) and leave OLA installed.
2. Add the integration with `172.16.10.41`. Wait for the scan (about 14 seconds). You should see 8 lights at DMX 1, 4, 7, 10, 13, 16, 19, and 22.
3. **One fixture.** Pick one light, set it red, then green, then off. Leave the others off.
4. If that fixture does not change:
   - Options → PORTOUT header version → **2**. Try the same light again.
   - Still dark: Light protocol → **KiNET v1 DMXOUT**. Output port is ignored in that mode.
   - Port is already 1. Change it only if you know the supply has another output.
5. If discovery fails or a fixture is missing, add a manual line (`serial, dmx_start`) instead of scanning again and again. A scan may blink fixtures; that behaviour is not confirmed.
6. When you are done experimenting, either leave this integration as the only sender, or roll back (next section) before starting OLA again.

## Roll back to OLA

1. Disable or delete the Philips Color Kinetics config entry so it stops sending. Unloading the entry closes the socket.
2. Start OLA's KiNET output again, the way it was before the test.
3. Confirm one fixture, then the rest.

Nothing in this integration writes an address, a name, an IP, or firmware, so rollback is "stop sending and let OLA send."

## Known unknowns

Confirmed from the QuickPlay Pro 2 capture of this Data Enabler Pro, and still open where noted:

- **No light data was in the capture.** v2 PORTOUT is the default because the poll reply advertises protocol version 2 and the supply's own scan replies use header version 2. Whether the supply accepts PORTOUT, and whether the header version must be `01 00` or `02 00`, is not confirmed on this hardware. Both are options for that reason.
- The Data Enabler's hold time when frames stop is unknown. The 1-second keepalive is a hedge, not a measured timeout.
- Whether a fixture scan blinks fixtures or pauses output is unknown. That is why scan is a button, not a poll.
- The poll argument `0a ff ff ff` is replayed as captured. Its meaning is unknown.
- Most fixture and supply property IDs are unknown. DMX start is `(property 0x40 << 8) | property 0x41`, 0-based, which matches DMX 1, 4, …, 22 on this rig. Type code is property `0x05` (`0x18` and `0x04`).
- How QuickPlay prints a serial, compared with the 8-digit hex used here, is unconfirmed. Compare the device serial with the QuickPlay screen before trusting a manual entry.
- 16-bit (6-channel) mode is not implemented. This rig is 3-channel RGB.
- There is no per-fixture online/health packet in any public source.
- Netmask, gateway, and DHCP were not in the capture and are not read.

## GitHub settings HACS still needs

The HACS action checks repository metadata that a pull request cannot change. On the repository page, open the gear next to **About** and add at least one topic. HACS treats an empty topic list as "no valid topics" and fails the check. Suggested topics:

- `home-assistant`
- `homeassistant`
- `hacs`
- `color-kinetics`
- `kinet`
- `lighting`

The repository description is already set, so that check passes. After the topics are saved, re-run the **HACS** workflow. The integration also ships `custom_components/color_kinetics/brand/icon.png`, which is the brand file that check looks for. It is an original mark for this integration, not a Philips logo.

## Development

Python 3.14 and Home Assistant 2026.10 (what `pytest-homeassistant-custom-component` 0.13.375 vendors).

```bash
pip install -r requirements_test.txt
pytest --cov=custom_components/color_kinetics --cov-report=term-missing --cov-fail-under=90
```

CI runs that suite, hassfest, and HACS validation. Coverage fails under 90% line coverage. Tests use the captured QuickPlay packets and OLA's DMXOUT/PORTOUT vectors. They do not open a real KiNET socket.

## License

MIT. See [LICENSE](LICENSE).
