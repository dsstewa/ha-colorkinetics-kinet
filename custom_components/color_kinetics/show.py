"""One show across several KiNet entries.

Follow hub points an entry at another entry. The leader's fixtures are
drawn first, then each follower's, so a chase walks off the leader and
onto the follower. Phase is ``(now - origin) / cycle`` from one shared
origin, so the two frame loops do not drift apart. A follow link that
would loop back is rejected.
"""

from __future__ import annotations

from .effects import cycle_phase, cycle_seconds


def normalize_leader(value: str | None) -> str:
    """An entry id, or empty when this hub follows nobody."""
    text = str(value or "").strip()
    if text.casefold() in {"", "none", "off"}:
        return ""
    return text


def follow_problem(
    links: dict[str, str], entry_id: str, leader_id: str, known_ids: set[str]
) -> str | None:
    """Error key when ``entry_id`` cannot follow ``leader_id``, or None.

    ``known_ids`` is every configured KiNet entry. An unknown leader is
    reported separately from a cycle so the options form can say which.
    """
    leader = normalize_leader(leader_id)
    if not leader:
        return None
    if leader not in known_ids:
        return "follow_missing"
    if would_cycle(links, entry_id, leader):
        return "follow_cycle"
    return None


def would_cycle(links: dict[str, str], entry_id: str, leader_id: str) -> bool:
    """True when making ``entry_id`` follow ``leader_id`` closes a loop.

    ``links`` maps an entry id to the entry it follows. Empty means it
    follows nobody. The proposed leader replaces ``entry_id``'s current link
    for the walk, so the check sees the graph as it would be after the save.
    """
    leader = normalize_leader(leader_id)
    if not leader:
        return False
    if leader == entry_id:
        return True
    seen = {entry_id}
    current = leader
    while current:
        if current in seen:
            return True
        seen.add(current)
        if current == entry_id:
            return True
        current = "" if current == entry_id else links.get(current, "")
        current = normalize_leader(current)
        if current == entry_id:
            return True
    return False


def root_of(links: dict[str, str], entry_id: str) -> str:
    """The entry at the top of ``entry_id``'s follow chain.

    A cycle, or a leader that is not in ``links``, stops the walk. The
    start entry is the root in that case so a broken link does not hang.
    """
    seen: set[str] = set()
    current = entry_id
    while current in links:
        leader = normalize_leader(links.get(current))
        if not leader or leader not in links:
            return current
        if leader in seen or leader == current:
            return entry_id
        seen.add(current)
        current = leader
    return entry_id if entry_id in links else entry_id


def chain_members(links: dict[str, str], root: str) -> list[str]:
    """``root``, then each follower chain. Siblings are sorted by entry id.

    The walk refuses to visit an entry twice, so a cycle already stored in
    ``links`` cannot loop.
    """
    children: dict[str, list[str]] = {}
    for entry_id, leader in links.items():
        leader_id = normalize_leader(leader)
        if not leader_id or leader_id not in links:
            continue
        if would_cycle(links, entry_id, leader_id):
            continue
        children.setdefault(leader_id, []).append(entry_id)
    ordered: list[str] = []
    seen: set[str] = set()

    def _walk(entry_id: str) -> None:
        if entry_id in seen or entry_id not in links:
            return
        seen.add(entry_id)
        ordered.append(entry_id)
        for child in sorted(children.get(entry_id, [])):
            _walk(child)

    _walk(root)
    return ordered


def combined_positions(
    orders: list[list[str]],
) -> tuple[dict[str, int], int]:
    """Index of each fixture in the concatenated order, and the total count.

    The first order is the leader. A key that appears twice keeps the earlier
    index so one fixture is not drawn at two places.
    """
    positions: dict[str, int] = {}
    cursor = 0
    for order in orders:
        for key in order:
            if key in positions:
                continue
            positions[key] = cursor
            cursor += 1
    return positions, cursor


def show_phase(now: float, speed: float, origin: float) -> float:
    """Position in the cycle at ``now``, in ``[0, 1)``.

    ``origin`` is the wall time at which this speed's phase is 0. Every hub
    in the show passes the same three numbers, so the phase matches.
    """
    return cycle_phase(now, speed, origin)


def retarget_origin(now: float, origin: float, old_speed: float, new_speed: float) -> float:
    """A new origin that keeps the phase when the speed changes.

    The leader writes this once. Followers read it. They do not recompute
    it from their own frame time, which would walk the phase apart.
    """
    held = show_phase(now, old_speed, origin)
    return float(now) - held * cycle_seconds(new_speed)
