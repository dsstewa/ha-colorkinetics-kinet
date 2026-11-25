# Changelog

## 0.3.0

- Control panel on each Data Enabler, next to All fixtures. Effect runs the existing effects (or None, which stops and leaves the colors). Scene applies a saved look. Target is All fixtures or a group such as Pine, Oak, or Tree Run. Brightness is 0–100%. Stop effect and All off sit beside the existing speed, intensity, palette, and direction controls.
- Scenes are stored per Data Enabler and edited in the options flow (name, pattern, and per-fixture RGB and brightness). The built-ins are Halloween Purple (238, 0, 255), Christmas Red Green (red then green along fixture order), All Blue (0, 0, 255), Camp (warm white 255, 180, 80), and All Orange (255, 80, 0). Applying a scene stops any effect. The selection is restored after a restart.
- Follow hub, in the options flow, mirrors another KiNet entry's effect, scene, speed, and brightness. A chase uses one shared clock and the combined fixture order, leader first, so it crosses hubs without drifting. Stop effect and Effect None stop the follower too. All off turns it off. If the leader reloads or is removed, the follower keeps the phase from the clock it already copied. A follow link that loops is rejected. Removing an entry clears hubs that followed it. Palette, intensity, direction, and the target stay on each hub.

## 0.2.1

- Lights fade on and off by default. Default fade (seconds) is a configuration number on the Data Enabler and a field in the options flow, from 0 to 10, starting at 1.5. 0 snaps. An explicit `transition` overrides it. The fade runs in the existing frame loop, one frame per Data Enabler per tick, on a gamma 2.2 curve. A command during a fade continues from the current level. The light reports off when a turn-off fade finishes.
- Color temperature from 2000 K to 6500 K on every fixture, group, and All fixtures light, alongside RGB. Kelvin uses a blackbody approximation scaled so the brightest channel is 255. The mode and kelvin are restored after a restart. Effects that use the light's color use that RGB.

## 0.2.0

- A clean-room effect pack for eight fixtures: Solid, Breathe, Fade, Colorloop, Heartbeat, Sunrise, Gradient, Rainbow, Rainbow cycle, Wipe, Sweep, Theater chase, Running lights, Chase color, Scanner, Dual scanner, Meteor, Palette flow, Ripple, Twinkle, Twinkle fade, Candle, Fireplace, Lightning, Police, Alternate, Christmas, Christmas chase, and Halloween. WLED source is not included.
- Effect intensity (1–10), Effect palette, and Effect direction on the Data Enabler and on each group. Speed, intensity, palette, and direction persist across a restart and are stored on a look. Changing one while an effect is running keeps the current point in the cycle.
- Older effect names still start: Static, Color wheel, Rainbow chase, Color chase, Warm twinkle, Candy cane, and Red white blue. The light stores the new name.
- Choosing a color without an effect, or turning the light off, stops the effect. Unavailable fixtures are still left alone.

## 0.1.3

- Effect speed is still the 1–10 slider with the same entity id. One cycle is now on a log scale from 60 s at 1 to 3 s at 10 (`60 * (3/60) ** ((speed - 1) / 9)`). Speed 10 is one cycle every 3 s, which is what speed 2 did before. A saved look keeps its speed number, so a look from 0.1.2 runs slower at the same number.
- Christmas, Halloween, and Alternate blend from one color into the next over the last 30% of each half cycle. The existing frame loop draws the blend, still at most 40 times a second.
- Changing the speed while an effect is running keeps the current point in the cycle and continues from there.

## 0.1.2

- Holiday effects on All fixtures and every group light: Christmas, Warm twinkle, Candy cane, Red white blue, and Halloween. They follow fixture order and the 1–10 speed slider. The latest command still owns a fixture.
- Save the current colors, brightness, and any running effect and speed as a named look. Recall it from the Look selector on the Data Enabler, or with `color_kinetics.save_look`, `recall_look`, and `delete_look`. Looks are stored by serial in the existing cache, and a 0.1.1 cache is migrated in place. Duplicate names are rejected regardless of case. A fixture that is gone is skipped.
- Transitions on fixture, group, and All fixtures lights, and on `recall_look`. The fade runs in the existing frame loop. A newer command cancels the fade for the fixtures it takes. An animated effect with a transition fades into its first frame, then runs.
- Optional blueprint `blueprints/automation/color_kinetics/sunset_to_bedtime.yaml`. It is not installed automatically. Sunset can name a Data Enabler, and bedtime fades off the lights you choose for the look as well as the effect light.
- A fixture owned by a running effect is on, at that effect's color and brightness. Recalling it fades toward the effect's first frame, not an older color. Starting another effect with a transition stops the one that was running, fades into the new first frame, then runs the new effect. The Look selector keeps its value across a restart. A fixture device passed as `device_id` selects that fixture's Data Enabler.

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
