"""Build and transactionally publish an ARC domain manifest."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import FileLease, canonical_json_bytes

from _arc_workflows.domain_relationships import (
    RELATIONSHIP_LLM_RUN_DIRNAME,
    DomainRelationshipError,
    RelationshipRunner,
    _default_relationship_runner,
    _llm_relationships,
    normalize_domain_relationship_pairs,
)
from _arc_workflows.domain_manifest_inputs import (
    DomainManifestInputs,
    ManifestError,
    _read_object,
    _relative,
    collect_domain_manifest_inputs,
)
from _arc_workflows.domain_seed_provenance import (
    SEED_PROVENANCE_SCHEMA_VERSION,
)
from _arc_workflows.workflow_io import write_json_object


SCHEMA_VERSION = "arc.workflow.domain_manifest.v4"
SEED_PROVENANCE_DIRECTORY = "seed-provenance"


@dataclass(frozen=True)
class PreparedDomainManifest:
    manifest: dict[str, Any]
    seed_provenance: dict[str, Any]
    seed_provenance_path: Path
    project_dir: Path
    protected_input_paths: tuple[Path, ...]


def build_domain_manifest(
    project_dir: Path,
    *,
    relationship_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate a manifest without publishing any artifact."""

    inputs = collect_domain_manifest_inputs(project_dir)
    return _prepare_domain_manifest(
        inputs,
        relationship_result=relationship_result,
    ).manifest


def write_domain_manifest(
    project_dir: Path,
    output: Path | None = None,
    *,
    relationship_runner: RelationshipRunner | None = None,
) -> Path:
    project_dir = project_dir.expanduser().resolve()
    destination = (
        output.expanduser().resolve()
        if output
        else project_dir / ".arc" / "domain" / "domain-manifest.json"
    )
    lease = FileLease(
        project_dir / ".arc" / "domain" / ".domain-manifest.lock"
    ).acquire(blocking=True)
    try:
        inputs = collect_domain_manifest_inputs(project_dir)
        relationship_result: dict[str, Any] | None
        relationship_warning = ""
        if len(inputs.domains) == 1:
            relationship_result = {"pairs": []}
        else:
            try:
                relationship_result = _llm_relationships(
                    inputs.domains,
                    str(
                        inputs.context.get("user_intent", "")
                    ).strip(),
                    run_root=(
                        inputs.state_dir
                        / RELATIONSHIP_LLM_RUN_DIRNAME
                    ),
                    runner=(
                        relationship_runner
                        or _default_relationship_runner
                    ),
                )
            except Exception as exc:
                relationship_result = None
                relationship_warning = str(exc)
        prepared = _prepare_domain_manifest(
            inputs,
            relationship_result=relationship_result,
            relationship_warning=relationship_warning,
        )
        _publish_prepared(prepared, destination=destination)
        return destination
    finally:
        lease.release()


def _prepare_domain_manifest(
    inputs: DomainManifestInputs,
    *,
    relationship_result: dict[str, Any] | None,
    relationship_warning: str = "",
) -> PreparedDomainManifest:
    context = inputs.context
    domains = inputs.domains
    domain_relationships = _domain_relationships(
        domains,
        relationship_result=relationship_result,
        relationship_warning=relationship_warning,
    )
    seed_provenance_digest = hashlib.sha256(
        canonical_json_bytes(inputs.seed_provenance)
    ).hexdigest()
    seed_provenance_path = (
        inputs.state_dir
        / SEED_PROVENANCE_DIRECTORY
        / f"seed-provenance-{seed_provenance_digest[:24]}.json"
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "user_intent": str(
            context.get("user_intent", "")
        ).strip(),
        "requested_seed_papers": (
            inputs.requested_seed_papers
        ),
        "seed_provenance_artifact": {
            "path": _relative(
                inputs.project_dir, seed_provenance_path
            ),
            "sha256": seed_provenance_digest,
            "schema_version": SEED_PROVENANCE_SCHEMA_VERSION,
        },
        "package_count": len(domains),
        "domain_packages": domains,
        "domain_relationships": domain_relationships,
        "duplicates": inputs.duplicates,
    }
    canonical_json_bytes(manifest)
    return PreparedDomainManifest(
        manifest=manifest,
        seed_provenance=inputs.seed_provenance,
        seed_provenance_path=seed_provenance_path,
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
    seed_provenance_path = prepared.seed_provenance_path
    provenance_exists = _preflight_immutable_artifact(
        seed_provenance_path,
        prepared.seed_provenance,
        label="seed provenance",
    )
    if not provenance_exists:
        write_json_object(
            seed_provenance_path, prepared.seed_provenance
        )
    write_json_object(destination, prepared.manifest)


def _preflight_immutable_artifact(
    path: Path,
    expected: dict[str, Any],
    *,
    label: str,
) -> bool:
    if not path.exists():
        return False
    try:
        existing = _read_object(path)
    except ManifestError as exc:
        raise ManifestError(
            f"immutable {label} is unreadable: {path}"
        ) from exc
    if existing != expected:
        raise ManifestError(
            f"immutable {label} conflicts with its content "
            f"identity: {path}"
        )
    return True


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
    seed_provenance_path = (
        prepared.seed_provenance_path.resolve()
    )
    project_dir = prepared.project_dir.resolve()
    if destination == seed_provenance_path:
        raise ManifestError(
            "manifest output must not be the immutable seed "
            f"provenance artifact: {destination}"
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
        ("immutable seed provenance artifact", seed_provenance_path),
    ):
        if path in protected:
            raise ManifestError(
                f"{label} must not overwrite a referenced input "
                f"artifact: {path}"
            )


def _domain_relationships(
    domains: list[dict[str, Any]],
    *,
    relationship_result: dict[str, Any] | None,
    relationship_warning: str,
) -> dict[str, Any]:
    if len(domains) == 1:
        return {
            "status": "not_applicable",
            "method": "not_applicable",
            "pair_classifications": [],
            "warnings": [],
        }
    try:
        if relationship_warning:
            raise DomainRelationshipError(relationship_warning)
        pairs = normalize_domain_relationship_pairs(
            relationship_result, domains
        )
    except (DomainRelationshipError, ManifestError) as exc:
        return {
            "status": "unavailable",
            "method": "llm_semantic_pair_classification",
            "pair_classifications": [],
            "warnings": [
                "domain_relationships_unavailable: "
                f"{exc}; package cards remain usable and scientific "
                "route selection remains model-led"
            ],
        }
    return {
        "status": "available",
        "method": "llm_semantic_pair_classification",
        "pair_classifications": pairs,
        "warnings": [],
    }


__all__ = [
    "SEED_PROVENANCE_DIRECTORY",
    "PreparedDomainManifest",
    "SCHEMA_VERSION",
    "build_domain_manifest",
    "write_domain_manifest",
]
