"""JSON template loading and value transformation for ARC ideas."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from _arc_workflows.ideas_config import ConfigError
from _arc_workflows.workflow_io import (
    NonObjectJsonError,
    read_json_object,
    require_strict_int,
)


def merged_worker_payload(
    template: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    return deep_merge(template, overrides)


def deep_merge(
    base: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def replace_placeholders(
    value: Any,
    replacements: Mapping[str, str],
) -> Any:
    if isinstance(value, str):
        result = value
        for old, new in replacements.items():
            result = result.replace(old, new)
        return result
    if isinstance(value, list):
        return [replace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: replace_placeholders(item, replacements)
            for key, item in value.items()
        }
    return value


def read_json(path: Path) -> dict[str, Any]:
    try:
        return read_json_object(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read JSON file {path}: {exc}") from exc
    except NonObjectJsonError as exc:
        raise ConfigError(
            f"JSON file must contain an object: {path}"
        ) from exc


def required_text(
    payload: Mapping[str, Any],
    key: str,
    source: Path,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{source}.{key} must be a non-empty string")
    return value.strip()


def positive_template_int(value: Any, key: str) -> int:
    return require_strict_int(
        value,
        key,
        minimum=1,
        requirement="a positive integer",
        error_type=ConfigError,
    )
