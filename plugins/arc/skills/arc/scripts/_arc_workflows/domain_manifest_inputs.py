"""Collect and validate copied artifacts for an ARC domain manifest."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _arc_workflows.workflow_io import NonObjectJsonError, read_json_object


SUMMARY_SUFFIX = "_domain_summary.json"
PAPER_PACK_SUFFIX = "_paper_json_pack.json"
SUMMARY_SCHEMA_VERSIONS = {
    "arc.domain_summary.v4",
    "arc.domain_summary.v5",
}
PAPER_PACK_SCHEMA_VERSION = "arc.domain_paper_json_pack.v1"


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


def collect_domain_manifest_inputs(project_dir: Path) -> DomainManifestInputs:
    project_dir = project_dir.expanduser().resolve()
    context_path = project_dir / "context.json"
    domain_dir = project_dir / "domain"
    context = _read_object(context_path)
    seed_by_domain = _seed_by_domain(context)
    if not domain_dir.is_dir():
        raise ManifestError(f"domain directory does not exist: {domain_dir}")

    domains: list[dict[str, Any]] = []
    duplicates: list[dict[str, str]] = []
    seen: dict[str, Path] = {}
    matched_paper_packs: set[Path] = set()
    copied_pack_ids: set[str] = set()
    for summary_path in sorted(domain_dir.glob(f"*{SUMMARY_SUFFIX}")):
        summary = _read_object(summary_path)
        summary_version = _required_string(
            summary, "schema_version", summary_path
        )
        if summary_version not in SUMMARY_SCHEMA_VERSIONS:
            raise ManifestError(
                f"{summary_path} schema_version must be "
                "arc.domain_summary.v4 or arc.domain_summary.v5"
            )
        prefix = summary_path.name[: -len(SUMMARY_SUFFIX)]
        markdown_path = domain_dir / f"{prefix}_domain_summary.md"
        paper_pack_path = domain_dir / f"{prefix}{PAPER_PACK_SUFFIX}"
        for required_path in (markdown_path, paper_pack_path):
            if not required_path.is_file():
                raise ManifestError(
                    f"required domain artifact does not exist: {required_path}"
                )

        paper_pack = _read_object(paper_pack_path)
        domain_id = _paper_pack_domain_id(paper_pack, paper_pack_path)
        matched_paper_packs.add(paper_pack_path.resolve())
        copied_pack_ids.add(domain_id)
        _validate_summary_identity(
            summary,
            summary_version=summary_version,
            summary_path=summary_path,
            authoritative_domain_id=domain_id,
        )
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

        foundation = summary.get("foundation_paper")
        if not isinstance(foundation, dict):
            foundation = {}
        seed_paper = seed_by_domain.get(domain_id, "")
        if not seed_paper:
            seed_paper = str(foundation.get("paper_id", "")).strip()
        if not seed_paper:
            seed_paper = prefix

        papers = paper_pack.get("papers", [])
        paper_ids = (
            sorted(
                {
                    str(item.get("paper_id", "")).strip()
                    for item in papers
                    if isinstance(item, dict) and item.get("paper_id")
                }
            )
            if isinstance(papers, list)
            else []
        )
        citation_edges = (
            sorted(
                {
                    (
                        str(item.get("paper_id", "")).strip(),
                        str(reference.get("paper_id", "")).strip(),
                    )
                    for item in papers
                    if isinstance(item, dict)
                    for reference in item.get("references", [])
                    if isinstance(item.get("references"), list)
                    and isinstance(reference, dict)
                    if item.get("paper_id") and reference.get("paper_id")
                }
            )
            if isinstance(papers, list)
            else []
        )
        domains.append(
            {
                "domain_package_id": domain_id,
                "seed_paper": seed_paper,
                "title": _required_text(
                    summary, "domain_title", summary_path
                ),
                "overview": str(
                    summary.get("overview")
                    or summary.get("brief_introduction")
                    or ""
                ),
                "task_focus": summary.get("task_focus", {}),
                "methodology": summary.get("methodology", []),
                "known_solved_cases": summary.get("known_solved_cases", []),
                "open_axes_for_new_work": summary.get(
                    "open_axes_for_new_work", []
                ),
                "mathematical_opportunities": summary.get(
                    "mathematical_opportunities",
                    {"well_defined_problems": []},
                ),
                "summary_schema_version": summary_version,
                "foundation_paper_ids": sorted(
                    {
                        seed_paper,
                        str(foundation.get("paper_id", "")).strip(),
                    }
                    - {""}
                ),
                "paper_ids": paper_ids,
                "citation_edges": [list(edge) for edge in citation_edges],
                "summary_json_path": _relative(project_dir, summary_path),
                "summary_markdown_path": _relative(
                    project_dir, markdown_path
                ),
                "paper_json_pack_path": _relative(
                    project_dir, paper_pack_path
                ),
            }
        )
        seen[domain_id] = summary_path

    if seed_by_domain:
        orphan_packs: list[Path] = []
        for paper_pack_path in sorted(
            domain_dir.glob(f"*{PAPER_PACK_SUFFIX}")
        ):
            resolved_pack_path = paper_pack_path.resolve()
            if resolved_pack_path in matched_paper_packs:
                continue
            paper_pack = _read_object(paper_pack_path)
            copied_pack_ids.add(
                _paper_pack_domain_id(paper_pack, paper_pack_path)
            )
            orphan_packs.append(paper_pack_path)
        if orphan_packs:
            raise ManifestError(
                "copied paper pack has no matching domain summary: "
                + ", ".join(
                    _relative(project_dir, path) for path in orphan_packs
                )
            )

        missing_record_ids = sorted(copied_pack_ids - set(seed_by_domain))
        if missing_record_ids:
            raise ManifestError(
                "context.json domain_records is missing copied paper-pack "
                "domain IDs: " + ", ".join(missing_record_ids)
            )
        extra_record_ids = sorted(set(seed_by_domain) - copied_pack_ids)
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
        raise ManifestError(f"required JSON file does not exist: {path}")
    try:
        return read_json_object(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read JSON object {path}: {exc}") from exc
    except NonObjectJsonError as exc:
        raise ManifestError(f"JSON root must be an object: {path}") from exc


def _required_text(
    payload: dict[str, Any], key: str, path: Path
) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise ManifestError(f"{path} is missing required field {key}")
    return value


def _required_string(
    payload: dict[str, Any], key: str, path: Path
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(
            f"{path} is missing required string field {key}"
        )
    return value.strip()


def _paper_pack_domain_id(
    paper_pack: dict[str, Any], path: Path
) -> str:
    pack_schema = _required_string(paper_pack, "schema_version", path)
    if pack_schema != PAPER_PACK_SCHEMA_VERSION:
        raise ManifestError(
            f"{path} schema_version must be {PAPER_PACK_SCHEMA_VERSION}"
        )
    return _required_string(paper_pack, "domain_id", path)


def _validate_summary_identity(
    summary: dict[str, Any],
    *,
    summary_version: str,
    summary_path: Path,
    authoritative_domain_id: str,
) -> None:
    if summary_version == "arc.domain_summary.v5":
        if "domain_id" in summary:
            raise ManifestError(
                f"{summary_path} arc.domain_summary.v5 must not contain "
                "domain_id"
            )
        return
    legacy_domain_id = str(summary.get("domain_id", "")).strip()
    if (
        legacy_domain_id
        and legacy_domain_id != authoritative_domain_id
    ):
        raise ManifestError(
            f"{summary_path} legacy domain_id {legacy_domain_id!r} does not "
            f"match paper-pack domain_id {authoritative_domain_id!r}"
        )


def _seed_by_domain(context: dict[str, Any]) -> dict[str, str]:
    raw_records = context.get("domain_records", [])
    if not isinstance(raw_records, list):
        raise ManifestError(
            "context.json domain_records must be an array"
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
                f"context.json domain_records[{index}] requires domain_id "
                "and seed_paper"
            )
        if domain_id in result and result[domain_id] != seed_paper:
            raise ManifestError(
                f"conflicting requested seeds recorded for domain {domain_id}"
            )
        result[domain_id] = seed_paper
    return result


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise ManifestError(
            f"domain artifact must be inside project directory: {path}"
        ) from exc


__all__ = [
    "DomainManifestInputs",
    "ManifestError",
    "PAPER_PACK_SCHEMA_VERSION",
    "PAPER_PACK_SUFFIX",
    "SUMMARY_SCHEMA_VERSIONS",
    "SUMMARY_SUFFIX",
    "_paper_pack_domain_id",
    "_read_object",
    "_relative",
    "_required_string",
    "_required_text",
    "_seed_by_domain",
    "_validate_summary_identity",
    "collect_domain_manifest_inputs",
]
