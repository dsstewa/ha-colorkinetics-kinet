"""Named looks, matched without regard to case.

A look is stored under the name the user typed, after stripping whitespace.
A second save with the same name in any case is a duplicate, not a replacement.
"""

from __future__ import annotations

from typing import Any


def normalize_look_name(name: str) -> str:
    """Strip a look name. An empty result is not a usable name."""
    return str(name).strip()


def find_look_name(looks: dict[str, Any], name: str) -> str | None:
    """Return the stored name that matches ``name``, ignoring case."""
    folded = normalize_look_name(name).casefold()
    if not folded:
        return None
    for existing in looks:
        if str(existing).casefold() == folded:
            return str(existing)
    return None
