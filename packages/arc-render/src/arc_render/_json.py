"""Canonical JSON helpers shared by render contracts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any


JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | tuple["JsonValue", ...]
    | Mapping[str, "JsonValue"]
)


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a previously validated JSON value deterministically."""

    return json.dumps(
        thaw_json(freeze_json(value)),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def freeze_json(value: Any, description: str = "value") -> JsonValue:
    """Validate JSON compatibility and return an immutable projection."""

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{description} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{description} contains a non-string key")
            frozen[key] = freeze_json(item, description)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return tuple(freeze_json(item, description) for item in value)
    raise ValueError(f"{description} is not JSON-compatible")


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def require_exact(
    value: Any, fields: set[str], description: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{description} has invalid fields")
    return dict(value)


def require_string(value: Any, description: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        qualifier = "" if empty else " non-empty"
        raise ValueError(f"{description} must be a{qualifier} string")
    return value


def require_integer(value: Any, description: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
    ):
        raise ValueError(
            f"{description} must be an integer greater than or equal to {minimum}"
        )
    return value


def require_list(value: Any, description: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{description} must be an array")
    return value


__all__ = [
    "JsonValue",
    "canonical_json_bytes",
    "freeze_json",
    "require_exact",
    "require_integer",
    "require_list",
    "require_string",
    "thaw_json",
]
