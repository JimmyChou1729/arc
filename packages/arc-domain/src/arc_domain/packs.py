"""Pure projections of acquired paper data into the legacy domain packs.

The durable domain build acquires each paper once, then passes the resulting
records here.  This module deliberately neither fetches nor persists anything:
it only preserves the two established pack documents for downstream summary
and export consumers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arc_paper import normalize_paper_id

from ._roles import role_order


PAPER_JSON_PACK_SCHEMA_VERSION = "arc.domain_paper_json_pack.v1"
EVIDENCE_PACK_SCHEMA_VERSION = "arc.domain_evidence_pack.v1"


@dataclass(frozen=True)
class DomainPacks:
    """The paper JSON and evidence documents projected from one graph."""

    paper_json_pack: dict[str, Any]
    evidence_pack: dict[str, Any]


def build_domain_packs(
    graph: Mapping[str, Any], acquired: Mapping[str, Mapping[str, Any]],
) -> DomainPacks:
    """Project acquired paper records into the two version-one pack shapes.

    ``graph`` supplies the membership and role of each paper.  Each acquired
    record is expected to contain ``metadata``, ``references``, ``toc``,
    ``conclusion``, and ``warnings``; a missing record remains visible in both
    packs with deterministic unavailable-data warnings.
    """

    acquired_by_id = _acquired_by_normalized_id(acquired)
    papers = [
        (_paper_id(node), node)
        for node in _graph_nodes(graph)
        if _paper_id(node)
    ]
    papers.sort(key=lambda item: (role_order(item[1].get("role", "")), item[0]))

    paper_json_papers: list[dict[str, Any]] = []
    evidence_papers: list[dict[str, Any]] = []
    for paper_id, node in papers:
        record = acquired_by_id.get(paper_id)
        paper_json_papers.append(_paper_json(paper_id, node, record))
        evidence_papers.append(_paper_evidence(paper_id, node, record))

    common = {
        "domain_id": _string(graph.get("domain_id")),
        "foundation_paper": _string(graph.get("foundation_paper")),
        "created_at": _string(graph.get("created_at")),
    }
    return DomainPacks(
        paper_json_pack={
            "schema_version": PAPER_JSON_PACK_SCHEMA_VERSION,
            "domain_id": common["domain_id"],
            "foundation_paper": common["foundation_paper"],
            "paper_count": len(paper_json_papers),
            "papers": paper_json_papers,
            "warnings": _paper_pack_warnings(paper_json_papers),
            "created_at": common["created_at"],
        },
        evidence_pack={
            "schema_version": EVIDENCE_PACK_SCHEMA_VERSION,
            "domain_id": common["domain_id"],
            "foundation_paper": common["foundation_paper"],
            "paper_count": len(evidence_papers),
            "papers": evidence_papers,
            "warnings": _evidence_pack_warnings(evidence_papers),
            "created_at": common["created_at"],
        },
    )


def _graph_nodes(graph: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, Mapping)]


def _acquired_by_normalized_id(
    acquired: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    records: dict[str, Mapping[str, Any]] = {}
    for raw_paper_id, record in acquired.items():
        paper_id = normalize_paper_id(_string(raw_paper_id))
        if paper_id and isinstance(record, Mapping):
            records[paper_id] = record
    return records


def _paper_id(node: Mapping[str, Any]) -> str:
    return normalize_paper_id(_string(node.get("paper_id") or node.get("id")))


def _paper_json(
    paper_id: str, node: Mapping[str, Any], record: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if record is None:
        metadata: dict[str, Any] = {}
        references: list[Any] = []
        toc: list[Any] = []
        warnings = _missing_acquisition_warnings()[:-1]
    else:
        metadata = _mapping_copy(record.get("metadata"))
        references = _list_copy(record.get("references"))
        toc = _list_copy(record.get("toc"))
        warnings = _warning_strings(record.get("warnings"))
    return {
        "paper_id": paper_id,
        "role": _string(node.get("role")),
        "metadata": metadata,
        "references": references,
        "toc": toc,
        "warnings": warnings,
    }


def _paper_evidence(
    paper_id: str, node: Mapping[str, Any], record: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if record is None:
        metadata: dict[str, Any] = {}
        conclusion: Any = None
        warnings = _missing_acquisition_warnings()
    else:
        metadata = _mapping_copy(record.get("metadata"))
        conclusion = _conclusion(record.get("conclusion"))
        warnings = _warning_strings(record.get("warnings"))
        if conclusion is None and not _has_warning_code(
            warnings, "conclusion_section_unavailable"
        ):
            warnings.append("conclusion_section_unavailable")
    return {
        "paper_id": paper_id,
        "role": _string(node.get("role")),
        "title": _first_string(metadata.get("title"), node.get("title")),
        "abstract": _first_string(metadata.get("abstract"), node.get("abstract")),
        "authors": _first_list(metadata.get("authors"), node.get("authors")),
        "year": metadata.get("year") if metadata.get("year") is not None else node.get("year"),
        "citation_count": _citation_count(
            metadata.get("citation_count"), node.get("citation_count")
        ),
        "selection_reason": _string(node.get("selection_reason")),
        "conclusion": conclusion,
        "warnings": warnings,
    }


def _missing_acquisition_warnings() -> list[str]:
    return [
        "metadata_unavailable:acquisition_missing",
        "references_unavailable:acquisition_missing",
        "toc_unavailable:acquisition_missing",
        "conclusion_section_unavailable",
    ]


def _warning_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    warnings: list[str] = []
    for warning in value:
        normalized = _warning_string(warning)
        if normalized:
            warnings.append(normalized)
    return warnings


def _warning_string(warning: Any) -> str:
    if isinstance(warning, str):
        return warning
    if not isinstance(warning, Mapping):
        return ""
    code = _string(warning.get("code"))
    if not code:
        return ""
    message = _string(warning.get("message"))
    return f"{code}:{message}" if message else code


def _has_warning_code(warnings: list[str], code: str) -> bool:
    return any(warning == code or warning.startswith(f"{code}:") for warning in warnings)


def _paper_pack_warnings(papers: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    missing_toc = sum(
        1
        for paper in papers
        if _has_warning_code(paper["warnings"], "toc_unavailable")
    )
    if missing_toc:
        warnings.append(f"{missing_toc} papers have no cached ar5iv table of contents")
    missing_references = sum(
        1
        for paper in papers
        if _has_warning_code(paper["warnings"], "references_unavailable")
    )
    if missing_references:
        warnings.append(f"{missing_references} papers have no cached reference list")
    return warnings


def _evidence_pack_warnings(papers: list[dict[str, Any]]) -> list[str]:
    missing = sum(1 for paper in papers if paper["conclusion"] is None)
    if not missing:
        return []
    return [f"{missing} papers have no cached conclusion/outlook/discussion section"]


def _mapping_copy(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list_copy(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _conclusion(value: Any) -> Any:
    return dict(value) if isinstance(value, Mapping) else value


def _first_string(preferred: Any, fallback: Any) -> str:
    return _string(preferred) or _string(fallback)


def _first_list(preferred: Any, fallback: Any) -> list[Any]:
    if isinstance(preferred, list) and preferred:
        return list(preferred)
    return _list_copy(fallback)


def _citation_count(preferred: Any, fallback: Any) -> int:
    value = preferred if preferred not in (None, "") else fallback
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


__all__ = [
    "DomainPacks",
    "EVIDENCE_PACK_SCHEMA_VERSION",
    "PAPER_JSON_PACK_SCHEMA_VERSION",
    "build_domain_packs",
]
