"""Configuration, path, and JSON helpers for the ARC calculate workflow."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from _arc_workflows.source_checkout import validate_strict_checkout_path
from _arc_workflows.workflow_io import (
    NonObjectJsonError,
    read_json_object,
    require_safe_id,
    write_json_object,
)


CALCULATE_CONFIG_SCHEMA = "arc.workflow.calculate.config.v3"
CALCULATE_RESULT_SCHEMA = "arc.workflow.calculate.result.v2"
CALCULATION_STEP_KINDS = frozenset(
    {"new_derivation", "check_known_result", "formal_setup"}
)
DEFAULT_HUMAN_GATE_PAUSE_STATUSES = (
    "reference_disagrees",
    "two_agree",
    "all_disagree",
    "unresolved",
    "failed",
)
class ConfigError(ValueError):
    """Invalid calculate-workflow configuration."""


@dataclass(frozen=True)
class CalculateStep:
    step_id: str
    prompt: str
    kind: str
    allowed_context: dict[str, Any]
    reviewer_reference_claim: dict[str, Any] | None


@dataclass(frozen=True)
class CalculateConfig:
    schema_version: str
    run_id: str
    run_dir: Path
    workflow_json_dir: Path
    proposer_count: int
    max_recalculations: int
    human_gate: dict[str, Any]
    defaults: dict[str, Any]
    steps: list[CalculateStep]


def load_calculation_config(payload: Mapping[str, Any]) -> CalculateConfig:
    data = copy.deepcopy(dict(payload))
    _reject_unknown_fields(
        data,
        {
            "schema_version",
            "run_id",
            "run_dir",
            "workflow_json_dir",
            "proposer_count",
            "max_recalculations",
            "human_gate",
            "defaults",
            "steps",
        },
        "calculate config",
    )
    schema_version = _required_text(data, "schema_version")
    if schema_version != CALCULATE_CONFIG_SCHEMA:
        raise ConfigError(f"schema_version must be {CALCULATE_CONFIG_SCHEMA}")

    run_id = _safe_id(_required_text(data, "run_id"), "run_id")
    run_dir = Path(_required_text(data, "run_dir")).expanduser().resolve()
    workflow_json_dir = Path(
        str(data.get("workflow_json_dir") or _default_workflow_json_dir())
    ).expanduser().resolve()
    validate_strict_checkout_path(
        workflow_json_dir,
        expected_relative_path="plugins/arc/skills/arc/workflows/json",
        field_name="workflow_json_dir",
        error_type=ConfigError,
    )
    proposer_count = _positive_int(data.get("proposer_count", 2), "proposer_count")
    max_recalculations = _nonnegative_int(data.get("max_recalculations", 1), "max_recalculations")
    human_gate = _parse_human_gate(data.get("human_gate", {}))
    defaults = _dict(data.get("defaults", {}), "defaults")
    _reject_unknown_fields(
        defaults,
        {
            "provider",
            "model",
            "model_tier",
            "integrity_reference_path",
        },
        "defaults",
    )
    if defaults.get("model") is not None and str(defaults.get("provider", "auto") or "auto") == "auto":
        raise ConfigError("defaults.model requires explicit provider")
    _validate_strict_integrity_reference(defaults.get("integrity_reference_path"))

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ConfigError("steps must be a non-empty list")

    steps: list[CalculateStep] = []
    seen_step_ids: set[str] = set()
    for raw_step in raw_steps:
        step_data = _dict(raw_step, "steps[]")
        step_id = _safe_id(_required_text(step_data, "step_id"), "step_id")
        if step_id in seen_step_ids:
            raise ConfigError(f"duplicate step_id: {step_id}")
        seen_step_ids.add(step_id)
        kind = _required_text(step_data, "kind")
        if kind not in CALCULATION_STEP_KINDS:
            allowed = ", ".join(sorted(CALCULATION_STEP_KINDS))
            raise ConfigError(f"step.kind must be one of: {allowed}")
        allowed_context = _dict(step_data.get("allowed_context", {}), f"{step_id}.allowed_context")
        steps.append(
            CalculateStep(
                step_id=step_id,
                prompt=_required_text(step_data, "prompt"),
                kind=kind,
                allowed_context=allowed_context,
                reviewer_reference_claim=_optional_dict(
                    step_data.get("reviewer_reference_claim"),
                    f"{step_id}.reviewer_reference_claim",
                ),
            )
        )
    if proposer_count < 2 and any(
        step.reviewer_reference_claim is not None for step in steps
    ):
        raise ConfigError("blind reference checks require at least two proposers")

    return CalculateConfig(
        schema_version=schema_version,
        run_id=run_id,
        run_dir=run_dir,
        workflow_json_dir=workflow_json_dir,
        proposer_count=proposer_count,
        max_recalculations=max_recalculations,
        human_gate=human_gate,
        defaults=defaults,
        steps=steps,
    )


def _reject_unknown_fields(
    payload: Mapping[str, Any],
    allowed: set[str],
    field_name: str,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ConfigError(
            f"{field_name} contains unsupported fields: {', '.join(unknown)}"
        )


def _integrity_reference(path_value: Any = None) -> dict[str, str]:
    path = _resolve_integrity_path(path_value)
    if path is None:
        raise FileNotFoundError("integrity.md was not found")
    return {"content": path.read_text(encoding="utf-8")}


def _resolve_integrity_path(path_value: Any = None) -> Path | None:
    if path_value:
        requested = Path(str(path_value)).expanduser()
        candidates = [requested] if requested.is_absolute() else []
        if not requested.is_absolute():
            candidates.extend(root / requested for root in [Path.cwd(), *Path.cwd().parents])
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return None
    path = Path(__file__).resolve().parents[2] / "rules/integrity.md"
    return path if path.exists() else None


def _validate_strict_integrity_reference(path_value: Any = None) -> None:
    path = _resolve_integrity_path(path_value)
    candidate = (
        path
        if path is not None
        else (
            Path(str(path_value)).expanduser()
            if path_value
            else Path(__file__).resolve().parents[2] / "rules/integrity.md"
        )
    )
    validate_strict_checkout_path(
        candidate,
        expected_relative_path="plugins/arc/skills/arc/rules/integrity.md",
        field_name="integrity_reference_path",
        error_type=ConfigError,
    )


def _read_template(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    return copy.deepcopy(payload)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return read_json_object(path)
    except NonObjectJsonError as exc:
        raise ValueError(f"Expected JSON object at {path}") from exc


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace a calculation-owned JSON record."""

    write_json_object(path, payload, sort_keys=True)


def _default_workflow_json_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "workflows" / "json"


def _required_text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None:
        raise ConfigError(f"{key} is required")
    text = str(value).strip()
    if not text:
        raise ConfigError(f"{key} is required")
    return text


def _safe_id(value: str, field_name: str) -> str:
    return require_safe_id(value, field_name, error_type=ConfigError)


def _positive_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except Exception as exc:
        raise ConfigError(f"{field_name} must be a positive integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{field_name} must be a positive integer")
    return parsed


def _nonnegative_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except Exception as exc:
        raise ConfigError(f"{field_name} must be a nonnegative integer") from exc
    if parsed < 0:
        raise ConfigError(f"{field_name} must be a nonnegative integer")
    return parsed


def _dict(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be an object")
    return copy.deepcopy(value)


def _bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{field_name} must be a boolean")


def _bool_default(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def _optional_dict(value: Any, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _dict(value, field_name)


def _parse_human_gate(value: Any) -> dict[str, Any]:
    data = _dict(value, "human_gate")
    enabled = _bool(data.get("enabled", False), "human_gate.enabled")
    pause_statuses = data.get("pause_on_statuses", DEFAULT_HUMAN_GATE_PAUSE_STATUSES)
    if not isinstance(pause_statuses, (list, tuple)):
        raise ConfigError("human_gate.pause_on_statuses must be a list")
    return {
        "enabled": enabled,
        "pause_on_statuses": [str(item) for item in pause_statuses],
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "CALCULATE_CONFIG_SCHEMA",
    "CALCULATE_RESULT_SCHEMA",
    "CALCULATION_STEP_KINDS",
    "DEFAULT_HUMAN_GATE_PAUSE_STATUSES",
    "CalculateConfig",
    "CalculateStep",
    "ConfigError",
    "_bool_default",
    "_dict",
    "_integrity_reference",
    "_jsonable",
    "_read_json",
    "_read_template",
    "_write_json",
    "load_calculation_config",
]
