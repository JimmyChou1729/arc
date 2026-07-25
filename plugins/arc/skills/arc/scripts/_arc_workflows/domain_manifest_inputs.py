"""Collect validated, package-owned artifacts for an ARC domain manifest."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_domain import (
    DomainPackageValidationError,
    decode_domain_package,
)
from arc_domain.summary import DOMAIN_SUMMARY_SCHEMA

from _arc_workflows.workflow_io import (
    NonObjectJsonError,
    read_json_object,
)


SUMMARY_SUFFIX = "_domain_summary.json"
PAPER_PACK_SUFFIX = "_paper_json_pack.json"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class DomainManifestInputs:
    project_dir: Path
    domain_dir: Path
    context: dict[str, Any]
    domains: list[dict[str, Any]]
    duplicates: list[dict[str, str]]
    requested_seed_papers: list[str]


def collect_domain_manifest_inputs(
    project_dir: Path,
) -> DomainManifestInputs:
    project_dir = project_dir.expanduser().resolve()
    context_path = project_dir / "context.json"
    domain_dir = project_dir / "domain"
    context = _read_object(context_path)
    seed_by_domain = _seed_by_domain(context)
    if not domain_dir.is_dir():
        raise ManifestError(
            f"domain directory does not exist: {domain_dir}"
        )

    domains: list[dict[str, Any]] = []
    duplicates: list[dict[str, str]] = []
    seen: dict[str, Path] = {}
    matched_paper_packs: set[Path] = set()
    copied_pack_ids: set[str] = set()
    for summary_path in sorted(
        domain_dir.glob(f"*{SUMMARY_SUFFIX}")
    ):
        prefix = summary_path.name[: -len(SUMMARY_SUFFIX)]
        markdown_path = (
            domain_dir / f"{prefix}_domain_summary.md"
        )
        paper_pack_path = (
            domain_dir / f"{prefix}{PAPER_PACK_SUFFIX}"
        )
        for required_path in (markdown_path, paper_pack_path):
            if not required_path.is_file():
                raise ManifestError(
                    "required domain artifact does not exist: "
                    f"{required_path}"
                )

        summary = _read_object(summary_path)
        paper_pack = _read_object(paper_pack_path)
        try:
            package = decode_domain_package(summary, paper_pack)
        except DomainPackageValidationError as exc:
            raise ManifestError(
                f"invalid domain package {summary_path} and "
                f"{paper_pack_path}: {exc}"
            ) from exc
        current_summary_version = str(
            DOMAIN_SUMMARY_SCHEMA["properties"][
                "schema_version"
            ]["const"]
        )
        if (
            package.summary.schema_version
            != current_summary_version
        ):
            raise ManifestError(
                f"{summary_path} schema_version must be "
                f"{current_summary_version}"
            )

        domain_id = package.domain_id
        matched_paper_packs.add(paper_pack_path.resolve())
        copied_pack_ids.add(domain_id)
        if domain_id in seen:
            duplicates.append(
                {
                    "domain_id": domain_id,
                    "kept_summary_json_path": _relative(
                        project_dir, seen[domain_id]
                    ),
                    "duplicate_summary_json_path": _relative(
                        project_dir, summary_path
                    ),
                }
            )
            continue

        seed_paper = seed_by_domain.get(domain_id, "")
        if not seed_paper:
            raise ManifestError(
                "context.json domain_records is missing copied "
                f"paper-pack domain IDs: {domain_id}"
            )
        summary_view = package.summary
        pack_view = package.paper_pack
        domains.append(
            {
                "domain_package_id": domain_id,
                "seed_paper": seed_paper,
                "title": summary_view.title,
                "overview": summary_view.overview,
                "task_focus": copy.deepcopy(
                    dict(summary_view.task_focus)
                ),
                "methodology": copy.deepcopy(
                    [dict(item) for item in summary_view.methodology]
                ),
                "known_solved_cases": copy.deepcopy(
                    [
                        dict(item)
                        for item in summary_view.known_solved_cases
                    ]
                ),
                "open_axes_for_new_work": copy.deepcopy(
                    [
                        dict(item)
                        for item in summary_view.open_axes_for_new_work
                    ]
                ),
                "mathematical_opportunities": copy.deepcopy(
                    dict(summary_view.mathematical_opportunities)
                ),
                "summary_schema_version": (
                    summary_view.schema_version
                ),
                "foundation_paper_ids": sorted(
                    {
                        seed_paper,
                        summary_view.foundation_paper_id,
                    }
                ),
                "paper_ids": list(pack_view.paper_ids),
                "citation_edges": [
                    list(edge) for edge in pack_view.citation_edges
                ],
                "summary_json_path": _relative(
                    project_dir, summary_path
                ),
                "summary_markdown_path": _relative(
                    project_dir, markdown_path
                ),
                "paper_json_pack_path": _relative(
                    project_dir, paper_pack_path
                ),
            }
        )
        seen[domain_id] = summary_path

    orphan_packs = [
        paper_pack_path
        for paper_pack_path in sorted(
            domain_dir.glob(f"*{PAPER_PACK_SUFFIX}")
        )
        if paper_pack_path.resolve() not in matched_paper_packs
    ]
    if orphan_packs:
        raise ManifestError(
            "copied paper pack has no matching domain summary: "
            + ", ".join(
                _relative(project_dir, path)
                for path in orphan_packs
            )
        )

    missing_record_ids = sorted(
        copied_pack_ids - set(seed_by_domain)
    )
    if missing_record_ids:
        raise ManifestError(
            "context.json domain_records is missing copied paper-pack "
            "domain IDs: " + ", ".join(missing_record_ids)
        )
    extra_record_ids = sorted(
        set(seed_by_domain) - copied_pack_ids
    )
    if extra_record_ids:
        raise ManifestError(
            "context.json domain_records references domain IDs with no "
            "copied paper pack: " + ", ".join(extra_record_ids)
        )

    if not domains:
        raise ManifestError(
            f"no {SUMMARY_SUFFIX} files found in {domain_dir}"
        )

    requested = context.get("seed_paper_list", [])
    if not isinstance(requested, list):
        requested = []
    requested_strings = [str(item) for item in requested]
    requested_order = {
        seed: index for index, seed in enumerate(requested_strings)
    }
    domains.sort(
        key=lambda item: (
            requested_order.get(
                item["seed_paper"], len(requested_order)
            ),
            item["domain_package_id"],
        )
    )
    return DomainManifestInputs(
        project_dir=project_dir,
        domain_dir=domain_dir,
        context=context,
        domains=domains,
        duplicates=duplicates,
        requested_seed_papers=requested_strings,
    )


def _read_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ManifestError(
            f"required JSON file does not exist: {path}"
        )
    try:
        return read_json_object(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(
            f"cannot read JSON object {path}: {exc}"
        ) from exc
    except NonObjectJsonError as exc:
        raise ManifestError(
            f"JSON root must be an object: {path}"
        ) from exc


def _seed_by_domain(context: dict[str, Any]) -> dict[str, str]:
    raw_records = context.get("domain_records")
    if not isinstance(raw_records, list) or not raw_records:
        raise ManifestError(
            "context.json domain_records must be a non-empty array"
        )
    result: dict[str, str] = {}
    for index, record in enumerate(raw_records):
        if not isinstance(record, dict):
            raise ManifestError(
                f"context.json domain_records[{index}] must be an object"
            )
        domain_id = str(record.get("domain_id", "")).strip()
        seed_paper = str(record.get("seed_paper", "")).strip()
        if not domain_id or not seed_paper:
            raise ManifestError(
                f"context.json domain_records[{index}] requires "
                "domain_id and seed_paper"
            )
        if (
            domain_id in result
            and result[domain_id] != seed_paper
        ):
            raise ManifestError(
                "conflicting requested seeds recorded for domain "
                f"{domain_id}"
            )
        result[domain_id] = seed_paper
    return result


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise ManifestError(
            "domain artifact must be inside project directory: "
            f"{path}"
        ) from exc


__all__ = [
    "DomainManifestInputs",
    "ManifestError",
    "PAPER_PACK_SUFFIX",
    "SUMMARY_SUFFIX",
    "_read_object",
    "_relative",
    "_seed_by_domain",
    "collect_domain_manifest_inputs",
]
