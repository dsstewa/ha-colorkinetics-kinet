"""Fixture order stored by serial.

The saved list is the user's order. Fixtures that are not in it yet are
appended by DMX start. Serials that disappear from a scan stay in the list.
"""

from __future__ import annotations


def effective_order(
    saved: list[str], discovered: list[tuple[str, int]]
) -> list[str]:
    """Saved serials first, then newly discovered fixtures by DMX address."""
    result: list[str] = []
    seen: set[str] = set()
    for serial in saved:
        if serial in seen:
            continue
        result.append(serial)
        seen.add(serial)
    extras = [serial for serial, _dmx in discovered if serial not in seen]
    dmx = {serial: address for serial, address in discovered}
    extras.sort(key=lambda serial: (dmx.get(serial, 1_000_000), serial))
    result.extend(extras)
    return result


def apply_submitted_order(
    saved: list[str],
    submitted: list[str],
    discovered: list[tuple[str, int]],
) -> list[str]:
    """Apply a reorder of the fixtures the user could see.

    Serials that were saved but not on the form keep the nearest slot.
    Discovered serials that are still absent are appended by DMX address.
    """
    visible: list[str] = []
    seen: set[str] = set()
    for serial in submitted:
        if serial in seen:
            continue
        visible.append(serial)
        seen.add(serial)
    hidden = [(index, serial) for index, serial in enumerate(saved) if serial not in seen]
    result = list(visible)
    for index, serial in hidden:
        if serial in seen:
            continue
        result.insert(min(index, len(result)), serial)
        seen.add(serial)
    return effective_order(result, discovered)
