from __future__ import annotations

import copy
import hashlib
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from arc_jobs import canonical_json_bytes
from arc_paper import normalize_paper_id

from _arc_workflows.domain_seed_provenance import (
    SEED_PROVENANCE_SCHEMA_VERSION,
    SeedProvenanceError,
    validate_seed_provenance,
)
from _arc_workflows.workflow_io import (
    NonObjectJsonError,
    read_json_object,
    require_safe_id,
)
from _arc_workflows.source_checkout import validate_strict_checkout_path


IDEAS_CONFIG_SCHEMA = "arc.workflow.ideas.config.v3"
IDEAS_VARIANT_SCHEMA = "arc.workflow.ideas.variant.v2"
DOMAIN_MANIFEST_SCHEMA = "arc.workflow.domain_manifest.v4"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ContextPolicy:
    domain_markdown_workspace_input_required: bool
    include_domain_markdown_workspace_input: bool


@dataclass(frozen=True)
class VariantConfig:
    variant_id: str
    path: Path
    loop_template: Path
    proposer_template: Path
    reviewer_template: Path
    reviewer_output_schema: Path
    marking_scheme: Path
    context_policy: ContextPolicy
    proposer_overrides: dict[str, Any]
    description: str


@dataclass(frozen=True)
class IdeasConfig:
    schema_version: str
    run_id: str
    run_dir: Path
    project_dir: Path
    user_intent: str
    variant_config_dir: Path
    variant_glob: str
    loops_per_variant: int
    variants: list[VariantConfig]
    domain_manifest_path: Path
    domain_manifest: dict[str, Any]
    exploration_profiles: list[dict[str, str]]
    context_warnings: list[str]


def load_ideas_config(payload: Mapping[str, Any]) -> IdeasConfig:
    data = copy.deepcopy(dict(payload))
    _reject_unknown_fields(
        data,
        {
            "schema_version",
            "run_id",
            "run_dir",
            "project_dir",
            "user_intent",
            "variant_config_dir",
            "variant_glob",
            "loops_per_variant",
            "domain_manifest_path",
            "exploration_profiles",
        },
        "ideas config",
    )
    schema_version = _required_text(data, "schema_version")
    if schema_version != IDEAS_CONFIG_SCHEMA:
        raise ConfigError(f"schema_version must be {IDEAS_CONFIG_SCHEMA}")

    run_id = _safe_id(_required_text(data, "run_id"), "run_id")
    project_dir = Path(_required_text(data, "project_dir")).expanduser().resolve()
    run_dir = Path(_required_text(data, "run_dir")).expanduser().resolve()
    expected_run_dir = project_dir / ".arc" / "ideas"
    if run_dir != expected_run_dir:
        raise ConfigError(
            "run_dir must be the project-local ARC ideas directory: "
            f"{expected_run_dir}"
        )
    user_intent = _required_text(data, "user_intent")
    variant_config_dir = Path(_required_text(data, "variant_config_dir")).expanduser()
    _validate_strict_variant_config_dir(variant_config_dir)
    variant_glob = str(data.get("variant_glob", "ideas-*.variant.json") or "").strip()
    if not variant_glob:
        raise ConfigError("variant_glob is required")
    loops_per_variant = _positive_int(data.get("loops_per_variant", 3), "loops_per_variant")
    domain_manifest_path = _configured_manifest_path(
        data, project_dir=project_dir
    )
    domain_manifest, context_warnings = _load_domain_manifest(
        domain_manifest_path,
        project_dir=project_dir,
    )
    variants = _discover_variants(variant_config_dir, variant_glob)
    if not variants:
        raise ConfigError(
            f"No enabled ideas variants found in {variant_config_dir} with {variant_glob}"
        )
    exploration_profiles = _exploration_profiles(data.get("exploration_profiles"))
    if exploration_profiles and len(exploration_profiles) != loops_per_variant:
        raise ConfigError(
            "exploration_profiles must contain exactly one profile per loop"
        )
    return IdeasConfig(
        schema_version=schema_version,
        run_id=run_id,
        run_dir=run_dir,
        project_dir=project_dir,
        user_intent=user_intent,
        variant_config_dir=variant_config_dir,
        variant_glob=variant_glob,
        loops_per_variant=loops_per_variant,
        variants=variants,
        domain_manifest_path=domain_manifest_path,
        domain_manifest=domain_manifest,
        exploration_profiles=exploration_profiles,
        context_warnings=context_warnings,
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


def _discover_variants(root: Path, pattern: str) -> list[VariantConfig]:
    if not root.exists():
        raise ConfigError(f"variant_config_dir does not exist: {root}")
    variants: list[VariantConfig] = []
    seen_variant_ids: dict[str, Path] = {}
    for path in sorted(root.glob(pattern)):
        if "_inactivated" in path.name or ".disabled." in path.name:
            continue
        try:
            payload = read_json_object(path)
        except NonObjectJsonError as exc:
            raise ConfigError(f"variant config must be an object: {path}") from exc
        variant = _parse_variant(payload, path=path)
        if variant is not None:
            previous = seen_variant_ids.get(variant.variant_id)
            if previous is not None:
                raise ConfigError(
                    f"duplicate enabled variant_id {variant.variant_id!r}: "
                    f"{previous} and {path}"
                )
            seen_variant_ids[variant.variant_id] = path
            variants.append(variant)
    return variants


def _parse_variant(payload: Mapping[str, Any], *, path: Path) -> VariantConfig | None:
    _reject_unknown_fields(
        payload,
        {
            "schema_version",
            "enabled",
            "variant_id",
            "description",
            "loop_template",
            "proposer_template",
            "reviewer_template",
            "reviewer_output_schema",
            "marking_scheme",
            "context_policy",
            "proposer",
        },
        str(path),
    )
    schema_version = str(payload.get("schema_version", "")).strip()
    if not schema_version:
        raise ConfigError(f"{path}.schema_version is required")
    if schema_version != IDEAS_VARIANT_SCHEMA:
        raise ConfigError(f"{path}.schema_version must be {IDEAS_VARIANT_SCHEMA}")
    if payload.get("enabled", True) is False:
        return None
    variant_id = _safe_id(_variant_required_text(payload, "variant_id", path), f"{path}.variant_id")
    base = path.parent
    loop_template = _relative_path(base, _variant_required_text(payload, "loop_template", path))
    proposer_template = _relative_path(base, _variant_required_text(payload, "proposer_template", path))
    reviewer_template = _relative_path(base, str(payload.get("reviewer_template", "ideas-reviewer.template.json")))
    reviewer_output_schema = _relative_path(
        base,
        str(payload.get("reviewer_output_schema", "ideas-reviewer-output.schema.json")),
    )
    marking_scheme = _relative_path(base, str(payload.get("marking_scheme", "ideas-marking-scheme.json")))
    if not loop_template.exists():
        raise ConfigError(f"loop_template does not exist: {loop_template}")
    if not proposer_template.exists():
        raise ConfigError(f"proposer_template does not exist: {proposer_template}")
    if not reviewer_template.exists():
        raise ConfigError(f"reviewer_template does not exist: {reviewer_template}")
    if not reviewer_output_schema.exists():
        raise ConfigError(f"reviewer_output_schema does not exist: {reviewer_output_schema}")
    if not marking_scheme.exists():
        raise ConfigError(f"marking_scheme does not exist: {marking_scheme}")
    return VariantConfig(
        variant_id=variant_id,
        path=path,
        loop_template=loop_template,
        proposer_template=proposer_template,
        reviewer_template=reviewer_template,
        reviewer_output_schema=reviewer_output_schema,
        marking_scheme=marking_scheme,
        context_policy=_parse_context_policy(payload.get("context_policy", {}), path=path),
        proposer_overrides=_dict(payload.get("proposer", {}), f"{path}.proposer"),
        description=str(payload.get("description", "")),
    )


def _configured_manifest_path(
    data: Mapping[str, Any],
    *,
    project_dir: Path,
) -> Path:
    raw = str(data.get("domain_manifest_path", "") or "").strip()
    if not raw:
        return project_dir / ".arc" / "domain" / "domain-manifest.json"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else project_dir / path


def _validate_strict_variant_config_dir(path: Path) -> None:
    validate_strict_checkout_path(
        path,
        expected_relative_path="plugins/arc/skills/arc/workflows/json",
        field_name="variant_config_dir",
        error_type=ConfigError,
    )


def _load_domain_manifest(
    path: Path,
    *,
    project_dir: Path,
) -> tuple[dict[str, Any], list[str]]:
    if not path.is_file():
        raise ConfigError(f"domain_manifest_path does not exist: {path}")
    try:
        payload = read_json_object(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read domain manifest {path}: {exc}") from exc
    except NonObjectJsonError as exc:
        raise ConfigError(f"domain manifest must be an object: {path}") from exc
    if payload.get("schema_version") != DOMAIN_MANIFEST_SCHEMA:
        raise ConfigError(
            f"{path}.schema_version must be {DOMAIN_MANIFEST_SCHEMA}; "
            "regenerate the domain manifest before running Ideas"
        )
    packages = payload.get("domain_packages")
    if not isinstance(packages, list) or not packages:
        raise ConfigError(f"{path}.domain_packages must be a non-empty array")
    package_seeds: dict[str, str] = {}
    for index, item in enumerate(packages):
        if not isinstance(item, dict):
            raise ConfigError(
                f"{path}.domain_packages[{index}] must be an object"
            )
        package_id = str(
            item.get("domain_package_id", "")
        ).strip()
        seed_raw = item.get("seed_paper")
        if (
            not package_id
            or not isinstance(seed_raw, str)
            or not seed_raw.strip()
        ):
            raise ConfigError(
                f"{path}.domain_packages[{index}] requires "
                "domain_package_id and seed_paper"
            )
        if package_id in package_seeds:
            raise ConfigError(
                f"{path}.domain_packages requires unique "
                "domain_package_id values"
            )
        package_seeds[package_id] = normalize_paper_id(seed_raw)
    package_ids = list(package_seeds)
    if payload.get("package_count") != len(packages):
        raise ConfigError(f"{path}.package_count is inconsistent")
    _validate_seed_provenance_artifact(
        path,
        payload,
        package_seeds=package_seeds,
        project_dir=project_dir,
    )
    relationships = payload.get("domain_relationships")
    if not isinstance(relationships, dict):
        raise ConfigError(f"{path}.domain_relationships must be an object")
    if set(relationships) != {
        "status",
        "method",
        "pair_classifications",
        "warnings",
    }:
        raise ConfigError(
            f"{path}.domain_relationships must contain exactly status, "
            "method, pair_classifications, and warnings"
        )
    status = str(relationships.get("status", "")).strip()
    if status not in {"available", "not_applicable", "unavailable"}:
        raise ConfigError(
            f"{path}.domain_relationships.status is invalid"
        )
    method = str(relationships.get("method", "")).strip()
    pairs = relationships.get("pair_classifications")
    warnings = relationships.get("warnings")
    if not method:
        raise ConfigError(
            f"{path}.domain_relationships.method is required"
        )
    if not isinstance(pairs, list):
        raise ConfigError(
            f"{path}.domain_relationships.pair_classifications must be an array"
        )
    if not isinstance(warnings, list) or any(
        not isinstance(item, str) for item in warnings
    ):
        raise ConfigError(
            f"{path}.domain_relationships.warnings must be an array of strings"
        )
    _validate_domain_relationships(
        path,
        pairs=pairs,
        package_ids=package_ids,
        status=status,
    )
    return payload, [str(item) for item in warnings]


def _validate_domain_relationships(
    path: Path,
    *,
    pairs: list[Any],
    package_ids: list[str],
    status: str,
) -> None:
    expected = set(itertools.combinations(sorted(package_ids), 2))
    found: set[tuple[str, str]] = set()
    for index, item in enumerate(pairs):
        if not isinstance(item, dict):
            raise ConfigError(
                f"{path}.domain_relationships.pair_classifications"
                f"[{index}] must be an object"
            )
        pair = tuple(
            sorted(
                (
                    str(item.get("package_a", "")).strip(),
                    str(item.get("package_b", "")).strip(),
                )
            )
        )
        confidence = item.get("confidence")
        if (
            pair not in expected
            or pair in found
            or item.get("classification")
            not in {"same_field", "distinct_field", "uncertain"}
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
            or not isinstance(item.get("evidence"), dict)
        ):
            raise ConfigError(
                f"{path}.domain_relationships.pair_classifications"
                f"[{index}] is invalid"
            )
        found.add(pair)
    if status == "available" and found != expected:
        raise ConfigError(
            f"{path}.domain_relationships available evidence must classify "
            "every package pair"
        )
    if status != "available" and pairs:
        raise ConfigError(
            f"{path}.domain_relationships pair classifications require "
            "status=available"
        )
    if len(package_ids) == 1 and status != "not_applicable":
        raise ConfigError(
            f"{path}.domain_relationships requires "
            "status=not_applicable for one domain package"
        )
    if status == "not_applicable" and len(package_ids) != 1:
        raise ConfigError(
            f"{path}.domain_relationships status=not_applicable requires "
            "one domain package"
        )


def _validate_seed_provenance_artifact(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    *,
    package_seeds: Mapping[str, str],
    project_dir: Path,
) -> None:
    reference = manifest.get("seed_provenance_artifact")
    if not isinstance(reference, dict) or set(reference) != {
        "path",
        "sha256",
        "schema_version",
    }:
        raise ConfigError(
            f"{manifest_path}.seed_provenance_artifact must contain "
            "exactly path, sha256, and schema_version"
        )
    if (
        reference.get("schema_version")
        != SEED_PROVENANCE_SCHEMA_VERSION
    ):
        raise ConfigError(
            f"{manifest_path}.seed_provenance_artifact."
            f"schema_version must be {SEED_PROVENANCE_SCHEMA_VERSION}"
        )
    relative = str(reference.get("path", "")).strip()
    expected_digest = str(reference.get("sha256", "")).strip()
    if not relative:
        raise ConfigError(
            f"{manifest_path}.seed_provenance_artifact.path is "
            "required"
        )
    if (
        len(expected_digest) != 64
        or any(character not in "0123456789abcdef"
               for character in expected_digest)
    ):
        raise ConfigError(
            f"{manifest_path}.seed_provenance_artifact.sha256 must "
            "be a lowercase SHA-256 digest"
        )
    provenance_path = _project_artifact_path(
        project_dir,
        relative,
        field_name=(
            f"{manifest_path}.seed_provenance_artifact.path"
        ),
    )
    if not provenance_path.is_file():
        raise ConfigError(
            f"{manifest_path}.seed_provenance_artifact does not "
            f"exist: {provenance_path}"
        )
    try:
        provenance = read_json_object(provenance_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"Could not read seed provenance {provenance_path}: {exc}"
        ) from exc
    except NonObjectJsonError as exc:
        raise ConfigError(
            f"seed provenance must be an object: {provenance_path}"
        ) from exc
    actual_digest = hashlib.sha256(
        canonical_json_bytes(provenance)
    ).hexdigest()
    if actual_digest != expected_digest:
        raise ConfigError(
            f"{provenance_path} does not match manifest SHA-256"
        )
    try:
        validated = validate_seed_provenance(
            provenance,
            expected_domain_ids=set(package_seeds),
        )
    except SeedProvenanceError as exc:
        raise ConfigError(
            f"invalid seed provenance {provenance_path}: {exc}"
        ) from exc
    provenance_seeds = {
        item["domain_id"]: item["build_seed"]
        for item in validated["build_origins"]
    }
    if provenance_seeds != dict(package_seeds):
        raise ConfigError(
            f"{manifest_path}.domain_packages seed_paper values "
            "must match seed provenance build origins"
        )
    requested = manifest.get("requested_seed_papers")
    if requested != [
        item["requested_seed"]
        for item in validated["requested_seed_mappings"]
    ]:
        raise ConfigError(
            f"{manifest_path}.requested_seed_papers is inconsistent "
            "with seed provenance"
        )


def _project_artifact_path(
    project_dir: Path,
    relative: str,
    *,
    field_name: str,
) -> Path:
    candidate = Path(relative).expanduser()
    if candidate.is_absolute():
        raise ConfigError(f"{field_name} must be project-relative")
    project_root = project_dir.expanduser().resolve()
    resolved = (project_root / candidate).resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ConfigError(
            f"{field_name} must stay inside project_dir"
        ) from exc
    return resolved


def _exploration_profiles(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ConfigError("exploration_profiles must be a non-empty array")
    profiles: list[dict[str, str]] = []
    seen: set[str] = set()
    seen_missions: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"exploration_profiles[{index}] must be an object")
        profile_id = _safe_id(str(item.get("profile_id", "")).strip(), f"exploration_profiles[{index}].profile_id")
        mission = str(item.get("mission", "")).strip()
        if not mission:
            raise ConfigError(f"exploration_profiles[{index}].mission is required")
        if profile_id in seen:
            raise ConfigError(f"exploration_profiles contains duplicate profile_id: {profile_id}")
        normalized_mission = " ".join(mission.split()).casefold()
        if normalized_mission in seen_missions:
            raise ConfigError(
                "exploration_profiles contains duplicate mission text"
            )
        seen.add(profile_id)
        seen_missions.add(normalized_mission)
        profiles.append({"profile_id": profile_id, "mission": mission})
    return profiles


def _parse_context_policy(raw: Any, *, path: Path) -> ContextPolicy:
    data = _dict(raw, f"{path}.context_policy")
    _reject_unknown_fields(
        data,
        {
            "domain_markdown_workspace_input_required",
            "include_domain_markdown_workspace_input",
        },
        f"{path}.context_policy",
    )
    include_domain = _bool(
        data.get("include_domain_markdown_workspace_input", False),
        f"{path}.context_policy.include_domain_markdown_workspace_input",
    )
    return ContextPolicy(
        domain_markdown_workspace_input_required=_bool(
            data.get("domain_markdown_workspace_input_required", include_domain),
            f"{path}.context_policy.domain_markdown_workspace_input_required",
        ),
        include_domain_markdown_workspace_input=include_domain,
    )


def _relative_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return base / path


def _required_text(data: Mapping[str, Any], field_name: str) -> str:
    text = str(data.get(field_name, "")).strip()
    if not text:
        raise ConfigError(f"{field_name} is required")
    return text


def _variant_required_text(data: Mapping[str, Any], key: str, path: Path) -> str:
    text = str(data.get(key, "")).strip()
    if not text:
        raise ConfigError(f"{path}.{key} is required")
    return text


def _safe_id(value: str, field_name: str) -> str:
    return require_safe_id(
        value,
        field_name,
        error_type=ConfigError,
    )


def _dict(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be an object")
    return copy.deepcopy(value)


def _positive_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except Exception as exc:
        raise ConfigError(f"{field_name} must be a positive integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{field_name} must be a positive integer")
    return parsed


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
