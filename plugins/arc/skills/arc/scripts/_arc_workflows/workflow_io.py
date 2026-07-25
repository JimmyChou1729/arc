"""Portable JSON and scalar helpers shared by ARC Skill workflows."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Pattern

from arc_jobs import InvalidRunIdError, atomic_write_bytes, validate_simple_id


SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
UNBOUNDED_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class NonObjectJsonError(ValueError):
    """A JSON document decoded successfully but its root was not an object."""


def read_json_object(path: str | Path) -> dict[str, Any]:
    """Read one UTF-8 JSON document and require an object root."""

    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if type(payload) is not dict:
        raise NonObjectJsonError(f"JSON root must be an object: {source}")
    return payload


def write_json_object(
    path: str | Path,
    payload: dict[str, Any],
    *,
    sort_keys: bool = False,
) -> None:
    """Atomically write one indented UTF-8 JSON object with a final newline."""

    if type(payload) is not dict:
        raise TypeError("JSON payload must be an object")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=sort_keys,
    )
    atomic_write_bytes(path, f"{encoded}\n".encode("utf-8"))


def require_strict_int(
    value: Any,
    field_name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    requirement: str = "an integer",
    error_type: type[Exception] = ValueError,
) -> int:
    """Require an exact integer scalar, excluding bool and numeric coercion."""

    if (
        type(value) is not int
        or (minimum is not None and value < minimum)
        or (maximum is not None and value > maximum)
    ):
        raise error_type(f"{field_name} must be {requirement}")
    return value


def require_safe_id(
    value: str,
    field_name: str,
    *,
    pattern: Pattern[str] | None = None,
    error_type: type[Exception] = ValueError,
) -> str:
    """Require a portable identifier while preserving caller error taxonomy."""

    selected_pattern = pattern or SAFE_ID_RE
    if pattern is None:
        try:
            return validate_simple_id(value, label=field_name)
        except InvalidRunIdError as exc:
            raise error_type(
                f"{field_name} must match {selected_pattern.pattern}"
            ) from exc
    if selected_pattern.fullmatch(value) is None:
        raise error_type(f"{field_name} must match {selected_pattern.pattern}")
    return value


__all__ = [
    "NonObjectJsonError",
    "SAFE_ID_RE",
    "UNBOUNDED_SAFE_ID_RE",
    "read_json_object",
    "require_safe_id",
    "require_strict_int",
    "write_json_object",
]
