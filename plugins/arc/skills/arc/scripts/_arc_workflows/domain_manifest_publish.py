"""Build and publish the project-local ARC domain manifest."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _arc_workflows.domain_field_grouping import (
    GROUPING_LLM_RUN_DIRNAME,
    GROUPING_SCHEMA_VERSION,
    HARD_SEPARATION_CONFIDENCE,
    GroupingLLMRunError,
    GroupingRunner,
    _build_field_groups,
    _default_grouping_runner,
    _llm_grouping,
    _validate_grouping,
)
from _arc_workflows.domain_manifest_inputs import (
    ManifestError,
    _relative,
    collect_domain_manifest_inputs,
)
from _arc_workflows.workflow_io import write_json_object


SCHEMA_VERSION = "arc.workflow.domain_manifest.v2"


def build_domain_manifest(
    project_dir: Path,
    *,
    grouping_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inputs = collect_domain_manifest_inputs(project_dir)
    project_dir = inputs.project_dir
    context = inputs.context
    domains = inputs.domains
    warning = ""
    try:
        pairs = _validate_grouping(grouping_result, domains)
        grouping_method = (
            "llm_semantic_pair_classification"
        )
    except ManifestError as exc:
        pairs = []
        grouping_method = "conservative_fallback"
        warning = (
            f"field_grouping_degraded: {exc}; merged all domain "
            "packages into one field"
        )
    field_groups = _build_field_groups(
        domains,
        pairs,
        intent=str(context.get("user_intent", "")),
        force_single=bool(warning),
    )
    grouping_payload = {
        "schema_version": GROUPING_SCHEMA_VERSION,
        "grouping_method": grouping_method,
        "hard_separation_confidence": (
            HARD_SEPARATION_CONFIDENCE
        ),
        "pair_classifications": pairs,
        "field_groups": [
            {
                key: item[key]
                for key in (
                    "field_id",
                    "domain_package_ids",
                    "confidence",
                    "reason",
                    "evidence",
                )
            }
            for item in field_groups
        ],
        "warnings": [warning] if warning else [],
    }
    grouping_path = (
        inputs.domain_dir / "field-grouping.json"
    )
    write_json_object(grouping_path, grouping_payload)
    return {
        "schema_version": SCHEMA_VERSION,
        "user_intent": str(
            context.get("user_intent", "")
        ).strip(),
        "research_scope": (
            "single_domain"
            if len(field_groups) == 1
            else "cross_domain"
        ),
        "requested_seed_papers": (
            inputs.requested_seed_papers
        ),
        "package_count": len(domains),
        "domain_packages": domains,
        "field_count": len(field_groups),
        "field_groups": field_groups,
        "grouping_method": grouping_method,
        "grouping_artifact": _relative(
            project_dir, grouping_path
        ),
        "grouping_warnings": grouping_payload["warnings"],
        "duplicates": inputs.duplicates,
    }


def write_domain_manifest(
    project_dir: Path,
    output: Path | None = None,
    *,
    grouping_runner: GroupingRunner | None = None,
) -> Path:
    project_dir = project_dir.expanduser().resolve()
    destination = (
        output.expanduser().resolve()
        if output
        else project_dir / "domain" / "domain-manifest.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        preliminary = build_domain_manifest(project_dir)
        if preliminary["package_count"] == 1:
            payload = build_domain_manifest(
                project_dir, grouping_result={"pairs": []}
            )
        else:
            grouping_result = _llm_grouping(
                preliminary["domain_packages"],
                preliminary["user_intent"],
                run_root=(
                    project_dir
                    / "domain"
                    / GROUPING_LLM_RUN_DIRNAME
                ),
                runner=(
                    grouping_runner
                    or _default_grouping_runner
                ),
            )
            payload = build_domain_manifest(
                project_dir,
                grouping_result=grouping_result,
            )
    except GroupingLLMRunError:
        raise
    except Exception:
        # Invalid model output and local grouping checks degrade safely to one
        # field. Typed non-terminal LLM outcomes are handled above and stop.
        payload = build_domain_manifest(project_dir)
    write_json_object(destination, payload)
    return destination


__all__ = [
    "SCHEMA_VERSION",
    "build_domain_manifest",
    "write_domain_manifest",
]
