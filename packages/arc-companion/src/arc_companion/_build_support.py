"""Shared deterministic helpers for the current Companion build handler."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from arc_jobs import (
    JsonValue,
    RunContext,
    canonical_json_bytes,
)

from .generation_validation import CompanionContentError


def task_id(prefix: str, semantic: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(dict(semantic))
    ).hexdigest()
    return f"{prefix}-{digest[:24]}"


def ref_document(ref: Any) -> dict[str, JsonValue]:
    return {
        "artifact_id": ref.artifact_id,
        "digest": {
            "algorithm": ref.digest.algorithm,
            "value": ref.digest.value,
            "size_bytes": ref.digest.size_bytes,
        },
        "media_type": ref.media_type,
        "relative_path": ref.relative_path,
    }


def read_json(
    context: RunContext, ref: Any, description: str
) -> dict[str, Any]:
    try:
        value = json.loads(
            context.artifacts.read_bytes(ref).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompanionContentError(
            "companion_artifact_invalid",
            f"cannot decode {description}: {exc}",
        ) from exc
    return mapping(value, description)


def mapping(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompanionContentError(
            "companion_artifact_invalid",
            f"{description} must be an object",
        )
    return dict(value)


def mapping_list(
    value: Any, description: str
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise CompanionContentError(
            "companion_artifact_invalid",
            f"{description} must be an array of objects",
        )
    return [dict(item) for item in value]


__all__ = [
    "mapping",
    "mapping_list",
    "read_json",
    "ref_document",
    "task_id",
]
