"""Template and caller-context materialization for ARC ideas batches."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from arc_domain.summary import mathematical_opportunities_validation_error
from arc_llm import ModelSelection
from arc_proposer_reviewer import (
    BatchFailurePolicy,
    BatchRequest,
    LoopSpec,
    ProposerFailurePolicy,
    WorkerSpec,
)
from arc_proposer_reviewer.models import BATCH_SCHEMA_VERSION

from _arc_workflows.ideas_config import ConfigError, IdeasConfig, VariantConfig
from _arc_workflows.ideas_marking import (
    load_marking_scheme,
    marking_scheme_for_context,
    marks_schema,
)
from _arc_workflows.ideas_policy import cross_domain_profile
from _arc_workflows.workflow_io import (
    NonObjectJsonError,
    read_json_object,
    require_strict_int,
)


@dataclass(frozen=True)
class IdeaPlan:
    idea_id: str
    variant_id: str
    idea_index: int
    loop_id: str
    variant: VariantConfig
    caller_context: dict[str, Any]
    workspace_input_paths: tuple[Path, ...]


def materialize_ideas(config: IdeasConfig) -> list[IdeaPlan]:
    ideas: list[IdeaPlan] = []
    for variant in config.variants:
        for idea_index in range(1, config.loops_per_variant + 1):
            idea_id = f"{variant.variant_id}/idea_{idea_index:03d}"
            context, workspace_input_paths = caller_context(
                config,
                variant=variant,
                idea_id=idea_id,
                idea_index=idea_index,
            )
            ideas.append(
                IdeaPlan(
                    idea_id=idea_id,
                    variant_id=variant.variant_id,
                    idea_index=idea_index,
                    loop_id=f"{variant.variant_id}_idea_{idea_index:03d}",
                    variant=variant,
                    caller_context=context,
                    workspace_input_paths=workspace_input_paths,
                )
            )
    return ideas


def batch_request(config: IdeasConfig, ideas: list[IdeaPlan]) -> BatchRequest:
    return BatchRequest(
        schema_version=BATCH_SCHEMA_VERSION,
        batch_id=config.run_id,
        loops=tuple(idea_loop_spec(idea) for idea in ideas),
        failure_policy=BatchFailurePolicy.COLLECT,
    )


def idea_loop_spec(idea: IdeaPlan) -> LoopSpec:
    template = read_json(idea.variant.loop_template)
    max_rounds = positive_template_int(template.get("max_rounds"), "max_rounds")
    early_stop = template.get("early_stop", {})
    if not isinstance(early_stop, Mapping):
        raise ConfigError(
            f"{idea.variant.loop_template}.early_stop must be an object"
        )
    enabled = early_stop.get("enabled", False)
    if type(enabled) is not bool:
        raise ConfigError(
            f"{idea.variant.loop_template}.early_stop.enabled must be a boolean"
        )
    return LoopSpec(
        loop_id=idea.loop_id,
        context=idea.caller_context,
        proposers=(
            worker_spec(
                merged_worker_payload(
                    read_json(idea.variant.proposer_template),
                    idea.variant.proposer_overrides,
                ),
                source=idea.variant.proposer_template,
            ),
        ),
        reviewer=worker_spec(
            reviewer_worker_payload(idea.variant),
            source=idea.variant.reviewer_template,
        ),
        max_rounds=max_rounds,
        allow_early_stop=enabled,
        on_proposer_failure=ProposerFailurePolicy.FAIL_LOOP,
    )


def worker_spec(
    payload: Mapping[str, Any],
    *,
    source: Path,
) -> WorkerSpec:
    worker_id = required_text(payload, "id", source)
    prompt = payload.get("prompt")
    if not isinstance(prompt, Mapping):
        raise ConfigError(f"{source}.prompt must be an object")
    system = required_text(prompt, "system", source)
    template = required_text(prompt, "template", source)
    output_schema = payload.get("output_schema")
    if not isinstance(output_schema, Mapping):
        raise ConfigError(f"{source}.output_schema must be an object")
    tier = str(payload.get("model_tier", "medium") or "medium").strip().lower()
    provider = str(payload.get("provider", "auto") or "auto").strip()
    model_value = payload.get("model")
    model = None if model_value is None else str(model_value).strip()
    if model == "":
        model = None
    try:
        selection = ModelSelection(
            provider=provider,
            model=model,
            tier=tier,  # type: ignore[arg-type]
        )
    except Exception as exc:
        raise ConfigError(f"{source}.model configuration is invalid: {exc}") from exc
    return WorkerSpec(
        worker_id=worker_id,
        instructions=f"{system}\n\n{template}",
        output_schema=copy.deepcopy(dict(output_schema)),
        model=selection,
    )


def reviewer_worker_payload(variant: VariantConfig) -> dict[str, Any]:
    payload = read_json(variant.reviewer_template)
    payload["output_schema"] = reviewer_payload_schema(variant)
    return payload


def reviewer_payload_schema(variant: VariantConfig) -> dict[str, Any]:
    schema = read_json(variant.reviewer_output_schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict) or "marks" not in properties:
        raise ConfigError(
            f"{variant.reviewer_output_schema} must be a direct reviewer payload schema"
        )
    schema["properties"]["marks"] = marks_schema(
        load_marking_scheme(variant.marking_scheme)
    )
    return schema


def caller_context(
    config: IdeasConfig,
    *,
    variant: VariantConfig,
    idea_id: str,
    idea_index: int,
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    loop_template = read_json(variant.loop_template)
    result = copy.deepcopy(loop_template.get("caller_context", {}))
    if not isinstance(result, dict):
        raise ConfigError(
            f"{variant.loop_template}.caller_context must be an object"
        )
    result = replace_placeholders(
        result,
        {"<user_intent>": config.user_intent},
    )
    result["user_intent"] = config.user_intent
    result["variant_id"] = variant.variant_id
    result["idea_id"] = idea_id
    result["marking_scheme"] = marking_scheme_for_context(
        load_marking_scheme(variant.marking_scheme)
    )
    if variant.research_scope == "cross_domain":
        result["generation_mode"] = "cross_domain"
        cards = domain_cards(config)
        result["domain_cards"] = cards
        result["exploration_profile"] = cross_domain_profile(
            config,
            idea_index=idea_index,
        )
    workspace_input_paths: tuple[Path, ...] = ()
    if variant.context_policy.include_domain_markdown_workspace_input:
        domain_package_dir = (
            config.project_dir / ".arc" / "domain" / "packages"
        )
        markdown_paths = domain_markdown_paths(domain_package_dir)
        if markdown_paths:
            workspace_input_paths = markdown_paths
        else:
            if variant.context_policy.domain_markdown_workspace_input_required:
                raise ConfigError(
                    f"{variant.variant_id} requires domain markdown under "
                    f"{domain_package_dir}"
                )
            result.setdefault("warnings", []).append(
                "domain_markdown_unavailable: Domain markdown was unavailable; "
                "continuing with user intent and ARC paper/tool context only."
            )
    return result, workspace_input_paths


def caller_context_warnings(ideas: list[IdeaPlan]) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for idea in ideas:
        for warning in idea.caller_context.get("warnings", []):
            text = str(warning)
            if text not in seen:
                seen.add(text)
                warnings.append(text)
    return warnings


def domain_cards(config: IdeasConfig) -> list[dict[str, Any]]:
    manifest = config.domain_manifest
    if not isinstance(manifest, Mapping):
        raise ConfigError("cross-domain ideas require a domain manifest")
    groups = manifest.get("field_groups")
    packages = manifest.get("domain_packages")
    if not isinstance(groups, list) or not isinstance(packages, list):
        raise ConfigError(
            f"{config.domain_manifest_path}.field_groups must be an array"
        )
    by_id = {
        str(item.get("domain_package_id", "")): item
        for item in packages
        if isinstance(item, Mapping)
    }
    cards: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        if not isinstance(group, Mapping):
            raise ConfigError(
                f"{config.domain_manifest_path}.field_groups[{index}] must be an object"
            )
        field_id = str(group.get("field_id", "")).strip()
        field_card = group.get("field_card")
        if not field_id or not isinstance(field_card, Mapping):
            raise ConfigError(
                f"{config.domain_manifest_path}.field_groups[{index}] requires "
                "field_id and field_card"
            )
        opportunities: list[Any] = []
        for package_index, package_id in enumerate(
            group.get("domain_package_ids", [])
        ):
            package = by_id.get(str(package_id))
            if not isinstance(package, Mapping):
                raise ConfigError(
                    f"field {field_id!r} references unknown package {package_id!r}"
                )
            summary_path = domain_summary_path(
                config,
                entry=package,
                index=package_index,
            )
            summary = read_json(summary_path)
            version = str(summary.get("schema_version", "")).strip()
            if version != "arc.domain_summary.v5":
                raise ConfigError(
                    f"{summary_path}.schema_version must be "
                    "arc.domain_summary.v5"
                )
            if "domain_id" in summary:
                raise ConfigError(
                    f"{summary_path} arc.domain_summary.v5 must not contain domain_id"
                )
            raw = summary.get("mathematical_opportunities")
            validation_error = mathematical_opportunities_validation_error(raw)
            if validation_error is not None:
                raise ConfigError(
                    f"{summary_path}.mathematical_opportunities is invalid "
                    f"for v5: {validation_error}"
                )
            opportunities.extend(
                copy.deepcopy(raw.get("well_defined_problems", []))
            )
        card = copy.deepcopy(dict(field_card))
        card.update(
            {
                "field_id": field_id,
                "domain_package_ids": list(
                    group.get("domain_package_ids", [])
                ),
                "summary_capabilities": {
                    "mathematical_opportunities": True
                },
                "mathematical_opportunities": {
                    "well_defined_problems": opportunities
                },
            }
        )
        cards.append(card)
    if len(cards) < 2:
        raise ConfigError(
            "cross-domain ideas require at least two distinct field cards"
        )
    return cards


def domain_summary_path(
    config: IdeasConfig,
    *,
    entry: Mapping[str, Any],
    index: int,
) -> Path:
    raw = str(entry.get("summary_json_path") or "").strip()
    if not raw:
        raise ConfigError(
            f"{config.domain_manifest_path}.domains[{index}] requires summary_json_path"
        )
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        raise ConfigError(
            f"{config.domain_manifest_path}.domains[{index}].summary_json_path "
            "must be project-relative"
        )
    project_root = config.project_dir.expanduser().resolve()
    path = (project_root / candidate).resolve()
    try:
        path.relative_to(project_root)
    except ValueError as exc:
        raise ConfigError(
            f"{config.domain_manifest_path}.domains[{index}].summary_json_path "
            "must stay inside project_dir"
        ) from exc
    if not path.is_file():
        raise ConfigError(f"domain summary does not exist: {path}")
    return path


def domain_markdown_paths(domain_dir: Path) -> tuple[Path, ...]:
    if not domain_dir.exists():
        return ()
    return tuple(
        path
        for path in sorted(domain_dir.rglob("*.md"))
        if path.is_file()
    )


def workspace_input_paths(ideas: list[IdeaPlan]) -> tuple[Path, ...]:
    """Return deterministic local sources to materialize before batch execution."""

    return tuple(
        sorted(
            {path.resolve() for idea in ideas for path in idea.workspace_input_paths},
            key=str,
        )
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
