"""Domain context and exploration-profile construction for ARC ideas."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from arc_domain import DomainPackageValidationError, decode_domain_summary
from arc_domain.summary import mathematical_opportunities_validation_error

from _arc_workflows.ideas_config import ConfigError, IdeasConfig, VariantConfig
from _arc_workflows.ideas_marking import (
    load_marking_scheme,
    marking_scheme_for_context,
)
from _arc_workflows.ideas_models import IdeaPlan
from _arc_workflows.ideas_policy import cross_domain_profile
from _arc_workflows.ideas_template_io import read_json, replace_placeholders


DEFAULT_SINGLE_DOMAIN_LENSES = [
    {
        "profile_id": "general_controlled_limit",
        "mission": (
            "Explore a controlled limit or expansion in which a bounded first "
            "calculation can expose a scientifically meaningful effect."
        ),
    },
    {
        "profile_id": "general_symmetry_consistency",
        "mission": (
            "Explore a symmetry, consistency condition, analyticity property, "
            "or conservation law that can produce a new discriminating result."
        ),
    },
    {
        "profile_id": "general_observable_discriminator",
        "mission": (
            "Explore a calculable observable or relation among observables that "
            "can distinguish otherwise degenerate physical mechanisms."
        ),
    },
    {
        "profile_id": "general_approximation_boundary",
        "mission": (
            "Explore the boundary of a standard approximation and identify a "
            "controlled calculation that tests where it ceases to be reliable."
        ),
    },
    {
        "profile_id": "general_validation_bridge",
        "mission": (
            "Explore a new validation, matching, or cross-check calculation "
            "that turns an important theoretical uncertainty into a falsifiable result."
        ),
    },
]


def caller_context(
    config: IdeasConfig,
    *,
    variant: VariantConfig,
    idea_id: str,
    idea_index: int,
    single_profiles: list[dict[str, str]] | None = None,
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
    else:
        result["generation_mode"] = "single_domain"
        profiles = (
            config.exploration_profiles
            or (
                single_profiles
                if single_profiles is not None
                else single_domain_profiles(config)
            )
        )
        try:
            result["exploration_profile"] = copy.deepcopy(
                profiles[idea_index - 1]
            )
        except IndexError as exc:
            raise ConfigError(
                "Automatic single-domain exploration profiles are insufficient "
                f"for {config.loops_per_variant} loops; provide exactly "
                "loops_per_variant exploration_profiles"
            ) from exc
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


def single_domain_profiles(config: IdeasConfig) -> list[dict[str, str]]:
    """Derive stable, distinct exploration routes from validated summaries."""

    profiles: list[dict[str, str]] = []
    seen_missions: set[str] = set()
    seen_axes: set[str] = set()
    seen_problems: set[str] = set()
    summaries: list[Any] = []
    packages = config.domain_manifest.get("domain_packages", [])
    if not isinstance(packages, list):
        raise ConfigError(
            f"{config.domain_manifest_path}.domain_packages must be an array"
        )
    for package_index, entry in enumerate(packages):
        if not isinstance(entry, Mapping):
            raise ConfigError(
                f"{config.domain_manifest_path}.domain_packages"
                f"[{package_index}] must be an object"
            )
        summary_path = domain_summary_path(
            config,
            entry=entry,
            index=package_index,
        )
        try:
            summaries.append(decode_domain_summary(read_json(summary_path)))
        except (DomainPackageValidationError, OSError, ValueError) as exc:
            raise ConfigError(
                f"domain summary is invalid: {summary_path}: {exc}"
            ) from exc

    for summary in summaries:
        for axis in summary.open_axes_for_new_work:
            axis_key = _profile_source_key(axis.get("axis"))
            if not axis_key or axis_key in seen_axes:
                continue
            seen_axes.add(axis_key)
            mission = (
                "Explore this domain-summary open axis as a route, while "
                "independently checking novelty and feasibility: "
                + json.dumps(axis, ensure_ascii=False, sort_keys=True)
            )
            _append_distinct_profile(
                profiles,
                seen_missions,
                profile_id=f"domain_axis_{len(seen_axes):03d}",
                mission=mission,
            )
    for summary in summaries:
        opportunities = summary.mathematical_opportunities.get(
            "well_defined_problems", []
        )
        for problem in opportunities:
            problem_key = _profile_source_key(problem.get("problem"))
            if not problem_key or problem_key in seen_problems:
                continue
            seen_problems.add(problem_key)
            mission = (
                "Explore this evidence-grounded mathematical opportunity as "
                "a route, while independently checking novelty and feasibility: "
                + json.dumps(problem, ensure_ascii=False, sort_keys=True)
            )
            _append_distinct_profile(
                profiles,
                seen_missions,
                profile_id=f"domain_problem_{len(seen_problems):03d}",
                mission=mission,
            )
    for lens in DEFAULT_SINGLE_DOMAIN_LENSES:
        _append_distinct_profile(
            profiles,
            seen_missions,
            profile_id=lens["profile_id"],
            mission=lens["mission"],
        )
    if len(profiles) < config.loops_per_variant:
        raise ConfigError(
            "Automatic single-domain exploration profiles are insufficient "
            f"for {config.loops_per_variant} loops; provide exactly "
            "loops_per_variant exploration_profiles"
        )
    return profiles


def _append_distinct_profile(
    profiles: list[dict[str, str]],
    seen_missions: set[str],
    *,
    profile_id: str,
    mission: str,
) -> None:
    normalized = " ".join(mission.split()).casefold()
    if not normalized or normalized in seen_missions:
        return
    seen_missions.add(normalized)
    profiles.append({"profile_id": profile_id, "mission": mission})


def _profile_source_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


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
                    f"{summary_path}.schema_version must be arc.domain_summary.v5"
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
                "domain_package_ids": list(group.get("domain_package_ids", [])),
                "summary_capabilities": {"mathematical_opportunities": True},
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
