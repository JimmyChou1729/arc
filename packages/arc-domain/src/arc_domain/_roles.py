"""Shared ordering for domain graph node roles."""

from __future__ import annotations

from typing import Final


_ROLE_ORDER: Final = {
    "foundation": 0,
    # Current graph artifacts use this name for the foundation node.
    "selected_foundation": 0,
    "parent_foundation": 1,
    "domain_paper": 2,
    "common_reference": 3,
}
_UNKNOWN_ROLE_ORDER: Final = 99


def role_order(role: object) -> int:
    """Return the stable display order for a domain graph node role."""

    return _ROLE_ORDER.get(str(role), _UNKNOWN_ROLE_ORDER)
