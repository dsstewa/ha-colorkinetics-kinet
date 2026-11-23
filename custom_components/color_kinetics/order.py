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


def _deduped(serials: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for serial in serials:
        if serial in seen:
            continue
        seen.add(serial)
        result.append(serial)
    return result


def place_anchored(visible: list[str], saved: list[str], hidden: set[str]) -> list[str]:
    """Keep each hidden serial after the kept serial that originally preceded it.

    A hidden serial whose predecessor was removed stays at the start, in the
    order those leading serials had in ``saved``. Reordering the visible
    serials does not pull a hidden serial away from its predecessor.
    """
    visible_set = set(visible)
    anchors: dict[str, list[str]] = {}
    leading: list[str] = []
    seen: set[str] = set()
    last_kept: str | None = None
    for serial in saved:
        if serial in seen:
            continue
        seen.add(serial)
        if serial in visible_set:
            last_kept = serial
            continue
        if serial not in hidden:
            continue
        if last_kept is None:
            leading.append(serial)
        else:
            anchors.setdefault(last_kept, []).append(serial)
    result = list(leading)
    for serial in visible:
        result.append(serial)
        result.extend(anchors.get(serial, []))
    return result


def apply_submitted_order(
    saved: list[str],
    submitted: list[str],
    discovered: list[tuple[str, int]],
) -> list[str]:
    """Apply a reorder of the fixtures the user could see.

    Serials that were saved but not submitted stay after the kept serial that
    preceded them. Discovered serials that are still absent are appended by
    DMX address.
    """
    visible = _deduped(submitted)
    hidden = {serial for serial in saved if serial not in set(visible)}
    return effective_order(place_anchored(visible, saved, hidden), discovered)


def merge_group_members(
    saved: list[str], submitted: list[str], on_form: set[str]
) -> list[str]:
    """Keep serials the form could not show, anchored to their old neighbor.

    ``submitted`` is the user's order of the fixtures that were on the form
    and still selected. A serial with no light entity is not on the form, so
    a rename or a no-op save leaves it after the same predecessor. A serial
    the user unchecked is removed.
    """
    visible = _deduped(submitted)
    hidden = {serial for serial in saved if serial not in on_form}
    return place_anchored(visible, saved, hidden)
