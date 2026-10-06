"""Portable JSON and scalar helpers shared by ARC Skill workflows."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ac_jobs import InvalidRunIdError, atomic_write_bytes, validate_simple_id


def llm_pause_document(outcome: Any, *, run_root: str | Path, run_id: str | None) -> dict[str, Any]:
    """Expose the owning workflow's public pause and coordinator locations."""
    from ac_jobs import RunRepository, encode_artifact_ref

    stopped = run_id is not None and RunRepository(run_root).inspect(run_id).stop_request is not None
    document = {"run_root": str(run_root), "run_id": run_id, "reason": outcome.reason.value,
                "resume_key": outcome.resume_key, "input_required": outcome.input_required,
                "response_contract": outcome.response_contract, "details": dict(outcome.details),
                "request_ref": None if outcome.request_ref is None else encode_artifact_ref(outcome.request_ref),
                "stop_requested": stopped}
    if outcome.details.get("code") == "awaiting_host" and run_id is not None:
        from ac_llm import HostTaskService
        document["host_tasks"] = HostTaskService().pending(run_root=run_root, run_id=run_id)
    return document


SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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
    error_type: type[Exception] = ValueError,
) -> str:
    """Require a portable identifier while preserving caller error taxonomy."""

    try:
        return validate_simple_id(value, label=field_name)
    except InvalidRunIdError as exc:
        raise error_type(
            f"{field_name} must match {SAFE_ID_RE.pattern}"
        ) from exc


__all__ = [
    "NonObjectJsonError",
    "SAFE_ID_RE",
    "read_json_object",
    "require_safe_id",
    "require_strict_int",
    "write_json_object",
]
