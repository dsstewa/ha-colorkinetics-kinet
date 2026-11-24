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

All fixtures and each group light accept an effect. The list below is the order in the effect dropdown. Patterns are a clean-room reading of common light-show behavior. WLED is EUPL-1.2 and none of its source is included.

| Effect | What it does | Intensity (1–10) |
|---|---|---|
| Solid | One color. This also stops an effect on that light. | Ignored. The light's brightness is the level. |
| Breathe | The chosen color fades in and out together. | How far the brightness falls. 1 is a shallow dip, 10 reaches dark. 5 matches the older breathe. |
| Fade | Holds each palette color, then blends into the next. Every fixture matches. | How long each color holds before the blend. |
| Colorloop | Walks the palette smoothly. Every fixture shows the same color. | How much of the palette one cycle walks. 5 and above walk the whole palette. |
| Heartbeat | Two quick pulses, then a rest. | How dark the rest is between the pulses. |
| Sunrise | A slow warm ramp. Reverse runs it back down. | How dark the ramp starts. 10 starts from off. |
| Gradient | A still spread of the palette along the order. | How much of the palette is spread. 10 uses the whole palette. |
| Rainbow | Every fixture shows the same hue, and the hue turns. | Saturation. 5 and above are fully saturated. |
| Rainbow cycle | The hue wheel spread along the order, rotating once per cycle. | How much of the wheel is spread. 5 and above use the full wheel. |
| Wipe | Fills the order from one end. | Softness of the leading edge. 5 and below are a hard edge. |
| Sweep | Wipes to the end and back in one cycle. | Softness of the leading edge. 5 and below are a hard edge. |
| Theater chase | Every nth fixture, stepping along the order. | Gap. 1–3 light every 2nd, 4–7 every 3rd, 8–10 every 4th. |
| Running lights | A sine wave of brightness traveling along the order. | Wave count. 8 and above draw two waves. |
| Chase color | The light's color runs along the order. | Tail length. 5 and below are a single fixture. 10 leaves a six-fixture tail. |
| Scanner | One dot bounces end to end and leaves a tail. | Tail length of the dot. |
| Dual scanner | Two dots bounce toward and away from each other. | Tail length of both dots. |
| Meteor | A bright head with a short tail, one pass per cycle. | Tail length, about one to four fixtures. |
| Palette flow | The palette slides along the order. | How many times the palette repeats. 8 and above repeat twice. |
| Ripple | A ring expands from the center. Reverse draws it back in. | Width of the ring. |
| Twinkle | A few fixtures sparkle. The same phase always draws the same frame. | How many fixtures sparkle. Lower is sparser. |
| Twinkle fade | Fewer fixtures than Twinkle, each one fading in and out. | How many fixtures fade. Sparser than Twinkle. |
| Candle | A gentle per-fixture flicker that stays lit. | Depth of the flicker. |
| Fireplace | One warm flicker across the run. | Depth of the shared flicker. |
| Lightning | Dim, then one flash per cycle, with one fixture leading it. | How long that single flash lasts. |
| Police | Even and odd fixtures swap two colors. Custom is red and blue. | How long each color holds before it blends. Higher holds longer. 5 is the 30% blend. |
| Alternate | The light's color and off swap between even and odd fixtures. | How long each half holds before it blends. Higher holds longer. 5 is the 30% blend. |
| Christmas | Red and green swap between even and odd fixtures. | How long each half holds before it blends. Higher holds longer. 5 is the 30% blend. |
| Christmas chase | Red, green, and white slide along the order. | Stripe width. 8 and above are two fixtures wide. |
| Halloween | Orange and purple swap between even and odd fixtures. | How long each half holds before it blends. Higher holds longer. 5 is the 30% blend. |

Effects use the selected palette where a palette makes sense. Rainbow, Rainbow cycle, Christmas, Christmas chase, Halloween, Candle, and Fireplace keep their own colors (Candle and Fireplace stay warm and can take a tint from another palette). Brightness still scales every channel.

Names from 0.1.x still start an effect, and the light stores the new name: Static is Solid, Color wheel is Rainbow, Rainbow chase is Rainbow cycle, Color chase is Chase color, Warm twinkle is Twinkle (and selects Warm white), Candy cane is Christmas chase, Red white blue is Palette flow (and selects July 4th).

**Effect speed** is a slider on the same device, from 1 to 10. The entity id is unchanged. One cycle takes `60 * (3/60) ** ((speed - 1) / 9)` seconds: 60 s at 1, about 15.9 s at 5, and 3 s at 10. Speed 10 is as fast as speed 2 was in 0.1.2. Effects are drawn by the existing frame loop, at most 40 times a second, and the loop sleeps when nothing is animating. The keepalive still applies. Christmas, Halloween, and Alternate use that same loop to blend across each color change. Changing speed, intensity, palette, or direction while an effect is running keeps the current point in the cycle. Turning the light off, or choosing a color without an effect, stops the effect. An effect does not write fixtures that are unavailable, and it does not clear a fixture that has never been set.

**Effect intensity**, **Effect palette**, and **Effect direction** sit on the Data Enabler with Effect speed, in the configuration section of the device. Each named group has the same four controls. Intensity is 1–10 and defaults to 5. Palette defaults to Custom, which uses the light's current color and, when only one color is set, a second color so a chase still has contrast. The other palettes are Christmas, Halloween, Rainbow, Warm white, Ice, Fire, Ocean, Forest, Sunset, Party, and July 4th. Direction is Forward or Reverse along the fixture order. The four controls restore after a restart, and a saved look stores them.

A sample Lovelace card for the Data Enabler. Entity ids follow the device name, so replace the host slug if yours differs:

```yaml
type: grid
columns: 2
square: false
cards:
  - type: tile
    entity: light.data_enabler_pro_172_16_10_41_all_fixtures
    name: All fixtures
    features:
      - type: light-brightness
  - type: tile
    entity: number.data_enabler_pro_172_16_10_41_effect_speed
    name: Effect speed
  - type: tile
    entity: number.data_enabler_pro_172_16_10_41_effect_intensity
    name: Effect intensity
  - type: tile
    entity: select.data_enabler_pro_172_16_10_41_effect_palette
    name: Effect palette
  - type: tile
    entity: select.data_enabler_pro_172_16_10_41_effect_direction
    name: Effect direction
```

A fixture is in at most one effect. The latest command wins: starting an effect takes those fixtures away from any effect that had them. Turning a member on, or setting Solid, drops that fixture out of the effect and leaves it on the solid color. The effect continues on whatever fixtures it still owns. A group that has lost every fixture reports off with Solid, and turning that group off does not write fixtures another effect still owns.

Fixture, group, and All fixtures lights accept a transition. The channels fade from what is on the wire to the new color, in the same frame loop (at most 40 times a second). The one-second keepalive and the send backoff are unchanged. Turning a light off with a transition fades those channels to zero. A newer command cancels the fade for the fixtures it takes and leaves every other fade running. Turning on an animated effect with a transition stops whatever effect those fixtures were in, fades each fixture to the new effect's first frame, then the effect runs. Recalling a look does the same for a saved effect: the fade target is that first frame.

## Saved looks

The **Look** selector on the Data Enabler recalls a saved look. Its options are the look names. The last look you saved or recalled stays selected across a restart.

Services, on the `color_kinetics` domain:

| Service | What it does |
|---|---|
| `save_look` | Save the current per-fixture color and brightness, plus the effect, speed, intensity, palette, and direction on All fixtures and each group. `name` is required. |
| `recall_look` | Restore that look. `transition` is optional, in seconds. |
| `delete_look` | Delete a look. |

Names match without regard to case, and a duplicate is rejected. The look is keyed by fixture serial and by group id, so renaming a fixture or a group does not break it. A serial that is no longer present is skipped. Fixtures that are not in the look are left as they are. Looks live in the same `.storage/color_kinetics_{entry_id}` file as the fixture cache. A cache written by 0.1.1 is migrated to storage version 2, which adds the look map. Deleting the config entry still removes that file.

A saved look keeps the speed number, 1 through 10. That number is not rewritten when the timing changes. In 0.1.3 the same number is a slower cycle than it was in 0.1.2: speed 1 takes 60 seconds and speed 10 takes 3 seconds. A look saved at speed 5 is still speed 5.

With more than one Color Kinetics entry loaded, pass `device_id` for the Data Enabler. A fixture device on that supply selects the same entry.

## Sunset to bedtime

The repo includes an automation blueprint that is not installed for you. At sunset, plus or minus an offset, it recalls a look when you set one, or turns a chosen light on with an effect. An optional Data Enabler input is sent as `device_id` so the recall hits the right entry when you have more than one. At a time you choose, it fades off the lights you pick for bedtime (All fixtures, and any group the look uses) and the effect light.

[Import the sunset to bedtime blueprint](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fdssstewa%2Fha-colorkinetics-kinet%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fcolor_kinetics%2Fsunset_to_bedtime.yaml).

The file in the repo is `blueprints/automation/color_kinetics/sunset_to_bedtime.yaml`.

## Light frames

Every PORTOUT and DMXOUT frame is at least 24 bytes, the OLA minimum, and it covers every fixture that has a DMX address. For each fixture:

- If this integration has sent it a color, including a color restored after a reload, that color is what goes out. Turning the fixture off is the only way those saved bytes become zeros.
- If it has never been set and nothing is saved, it is off. The frame carries zeros for those slots, and the light's state is off.

An unavailable fixture is not driven by a new effect or by All fixtures, but its last saved color stays in the frame so a reload or a later fixture does not black it out. Deleting the config entry removes the stored colors.

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
