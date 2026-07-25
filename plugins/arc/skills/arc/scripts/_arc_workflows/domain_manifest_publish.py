"""Build and transactionally publish an ARC domain manifest."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import FileLease, canonical_json_bytes

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
    DomainManifestInputs,
    ManifestError,
    _read_object,
    _relative,
    collect_domain_manifest_inputs,
)
from _arc_workflows.workflow_io import write_json_object


SCHEMA_VERSION = "arc.workflow.domain_manifest.v2"
GROUPING_DIRECTORY = "field-groupings"


@dataclass(frozen=True)
class PreparedDomainManifest:
    manifest: dict[str, Any]
    grouping: dict[str, Any]
    grouping_path: Path
    project_dir: Path
    protected_input_paths: tuple[Path, ...]


def build_domain_manifest(
    project_dir: Path,
    *,
    grouping_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate a manifest without publishing any artifact."""

    inputs = collect_domain_manifest_inputs(project_dir)
    return _prepare_domain_manifest(
        inputs,
        grouping_result=grouping_result,
    ).manifest


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
    lease = FileLease(
        project_dir / "domain" / ".domain-manifest.lock"
    ).acquire(blocking=True)
    try:
        inputs = collect_domain_manifest_inputs(project_dir)
        grouping_result: dict[str, Any] | None
        grouping_error: ManifestError | None = None
        if len(inputs.domains) == 1:
            grouping_result = {"pairs": []}
        else:
            try:
                grouping_result = _llm_grouping(
                    inputs.domains,
                    str(
                        inputs.context.get("user_intent", "")
                    ).strip(),
                    run_root=(
                        inputs.domain_dir
                        / GROUPING_LLM_RUN_DIRNAME
                    ),
                    runner=(
                        grouping_runner
                        or _default_grouping_runner
                    ),
                )
            except GroupingLLMRunError:
                raise
            except ManifestError as exc:
                grouping_result = None
                grouping_error = exc
        prepared = _prepare_domain_manifest(
            inputs,
            grouping_result=grouping_result,
            grouping_error=grouping_error,
        )
        _publish_prepared(prepared, destination=destination)
        return destination
    finally:
        lease.release()


def _prepare_domain_manifest(
    inputs: DomainManifestInputs,
    *,
    grouping_result: dict[str, Any] | None,
    grouping_error: ManifestError | None = None,
) -> PreparedDomainManifest:
    context = inputs.context
    domains = inputs.domains
    warning = ""
    try:
        if grouping_error is not None:
            raise grouping_error
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
    grouping_digest = hashlib.sha256(
        canonical_json_bytes(grouping_payload)
    ).hexdigest()[:24]
    grouping_path = (
        inputs.domain_dir
        / GROUPING_DIRECTORY
        / f"field-grouping-{grouping_digest}.json"
    )
    manifest = {
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
            inputs.project_dir, grouping_path
        ),
        "grouping_warnings": grouping_payload["warnings"],
        "duplicates": inputs.duplicates,
    }
    canonical_json_bytes(manifest)
    return PreparedDomainManifest(
        manifest=manifest,
        grouping=grouping_payload,
        grouping_path=grouping_path,
        project_dir=inputs.project_dir,
        protected_input_paths=_protected_input_paths(inputs),
    )


def _publish_prepared(
    prepared: PreparedDomainManifest,
    *,
    destination: Path,
) -> None:
    _validate_publication_paths(
        prepared,
        destination=destination,
    )
    grouping_path = prepared.grouping_path
    if grouping_path.exists():
        try:
            existing = _read_object(grouping_path)
        except ManifestError as exc:
            raise ManifestError(
                "immutable field grouping is unreadable: "
                f"{grouping_path}"
            ) from exc
        if existing != prepared.grouping:
            raise ManifestError(
                "immutable field grouping conflicts with its "
                f"content identity: {grouping_path}"
            )
    else:
        write_json_object(grouping_path, prepared.grouping)
    write_json_object(destination, prepared.manifest)


def _protected_input_paths(
    inputs: DomainManifestInputs,
) -> tuple[Path, ...]:
    paths = {inputs.project_dir / "context.json"}
    for pattern in (
        "*_domain_summary.json",
        "*_domain_summary.md",
        "*_paper_json_pack.json",
    ):
        paths.update(inputs.domain_dir.glob(pattern))
    return tuple(
        sorted(
            (path.resolve() for path in paths),
            key=str,
        )
    )


def _validate_publication_paths(
    prepared: PreparedDomainManifest,
    *,
    destination: Path,
) -> None:
    destination = destination.resolve()
    grouping_path = prepared.grouping_path.resolve()
    project_dir = prepared.project_dir.resolve()
    if destination == grouping_path:
        raise ManifestError(
            "manifest output must not be the immutable grouping "
            f"artifact: {destination}"
        )
    try:
        destination.relative_to(project_dir)
    except ValueError as exc:
        raise ManifestError(
            "manifest output must be inside the project directory: "
            f"{destination}"
        ) from exc
    protected = set(prepared.protected_input_paths)
    for label, path in (
        ("manifest output", destination),
        ("immutable grouping artifact", grouping_path),
    ):
        if path in protected:
            raise ManifestError(
                f"{label} must not overwrite a referenced input "
                f"artifact: {path}"
            )


__all__ = [
    "GROUPING_DIRECTORY",
    "PreparedDomainManifest",
    "SCHEMA_VERSION",
    "build_domain_manifest",
    "write_domain_manifest",
]
