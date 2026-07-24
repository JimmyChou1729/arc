from __future__ import annotations

from arc_domain.packs import (
    EVIDENCE_PACK_SCHEMA_VERSION,
    PAPER_JSON_PACK_SCHEMA_VERSION,
    build_domain_packs,
)


def _graph() -> dict:
    return {
        "schema_version": "arc.domain_graph.v1",
        "domain_id": "scalar-domain",
        "foundation_paper": "arXiv:2301.00001",
        "created_at": "2026-07-24T12:00:00+00:00",
        "nodes": [
            {
                "paper_id": "arXiv:2401.00003",
                "role": "common_reference",
                "title": "Common reference",
                "abstract": "Common abstract",
                "authors": ["Common author"],
                "year": 2024,
                "citation_count": 3,
                "selection_reason": "shared reference",
            },
            {
                "paper_id": "arXiv:2401.00002",
                "role": "domain_paper",
                "title": "Domain fallback title",
                "abstract": "Domain fallback abstract",
                "authors": ["Domain fallback author"],
                "year": 2024,
                "citation_count": 2,
                "selection_reason": "intent-ranked",
            },
            {
                "paper_id": "arXiv:2301.00001",
                "role": "selected_foundation",
                "title": "Foundation fallback title",
                "abstract": "Foundation fallback abstract",
                "authors": ["Foundation fallback author"],
                "year": 2023,
                "citation_count": 1,
                "selection_reason": "selected as foundation",
            },
            {
                "paper_id": "arXiv:2201.00004",
                "role": "parent_foundation",
                "title": "Parent fallback title",
                "abstract": "Parent fallback abstract",
                "authors": ["Parent fallback author"],
                "year": 2022,
                "citation_count": 4,
                "selection_reason": "parent witness",
            },
        ],
    }


def _acquired() -> dict:
    return {
        "2301.00001": {
            "metadata": {
                "title": "Foundation metadata title",
                "abstract": "Foundation metadata abstract",
                "authors": ["Foundation metadata author"],
                "year": 2023,
                "citation_count": 11,
            },
            "references": [{"paper_id": "arXiv:2101.00001"}],
            "toc": [{"section_id": "1", "title": "Introduction"}],
            "conclusion": {"section_id": "5", "title": "Conclusions", "text": "Foundation result."},
            "warnings": [],
        },
        "arXiv:2201.00004": {
            "metadata": {},
            "references": [],
            "toc": [],
            "conclusion": {"section_id": "4", "title": "Discussion", "text": "Parent result."},
            "warnings": [],
        },
        "arXiv:2401.00002": {
            "metadata": {
                "title": "Domain metadata title",
                "abstract": "Domain metadata abstract",
                "authors": ["Domain metadata author"],
                "year": 2025,
                "citation_count": "17",
            },
            "references": [{"paper_id": "arXiv:2201.00004"}],
            "toc": [],
            "conclusion": None,
            "warnings": [
                {"code": "toc_unavailable", "message": "document parse failed"},
                "references_unavailable:INSPIRE unavailable",
            ],
        },
    }


def test_build_domain_packs_preserves_exact_pack_fields_and_schema_versions():
    packs = build_domain_packs(_graph(), _acquired())

    paper_pack = packs.paper_json_pack
    evidence_pack = packs.evidence_pack
    assert paper_pack["schema_version"] == PAPER_JSON_PACK_SCHEMA_VERSION
    assert evidence_pack["schema_version"] == EVIDENCE_PACK_SCHEMA_VERSION
    assert set(paper_pack) == {
        "schema_version",
        "domain_id",
        "foundation_paper",
        "paper_count",
        "papers",
        "warnings",
        "created_at",
    }
    assert set(evidence_pack) == set(paper_pack)
    assert paper_pack["domain_id"] == "scalar-domain"
    assert evidence_pack["foundation_paper"] == "arXiv:2301.00001"
    assert paper_pack["created_at"] == "2026-07-24T12:00:00+00:00"
    assert paper_pack["paper_count"] == evidence_pack["paper_count"] == 4

    assert set(paper_pack["papers"][0]) == {
        "paper_id", "role", "metadata", "references", "toc", "warnings"
    }
    assert set(evidence_pack["papers"][0]) == {
        "paper_id",
        "role",
        "title",
        "abstract",
        "authors",
        "year",
        "citation_count",
        "selection_reason",
        "conclusion",
        "warnings",
    }
    foundation = evidence_pack["papers"][0]
    assert foundation["title"] == "Foundation metadata title"
    assert foundation["citation_count"] == 11
    assert foundation["conclusion"] == {
        "section_id": "5", "title": "Conclusions", "text": "Foundation result."
    }


def test_build_domain_packs_orders_roles_and_marks_missing_acquisition():
    packs = build_domain_packs(_graph(), _acquired())

    assert [paper["paper_id"] for paper in packs.paper_json_pack["papers"]] == [
        "arXiv:2301.00001",
        "arXiv:2201.00004",
        "arXiv:2401.00002",
        "arXiv:2401.00003",
    ]
    assert [paper["role"] for paper in packs.evidence_pack["papers"]] == [
        "selected_foundation",
        "parent_foundation",
        "domain_paper",
        "common_reference",
    ]

    missing_paper = packs.paper_json_pack["papers"][-1]
    missing_evidence = packs.evidence_pack["papers"][-1]
    assert missing_paper["metadata"] == {}
    assert missing_paper["references"] == []
    assert missing_paper["toc"] == []
    assert missing_paper["warnings"] == [
        "metadata_unavailable:acquisition_missing",
        "references_unavailable:acquisition_missing",
        "toc_unavailable:acquisition_missing",
    ]
    assert missing_evidence["conclusion"] is None
    assert missing_evidence["warnings"] == [
        "metadata_unavailable:acquisition_missing",
        "references_unavailable:acquisition_missing",
        "toc_unavailable:acquisition_missing",
        "conclusion_section_unavailable",
    ]
    assert packs.paper_json_pack["warnings"] == [
        "2 papers have no cached ar5iv table of contents",
        "2 papers have no cached reference list",
    ]
    assert packs.evidence_pack["warnings"] == [
        "2 papers have no cached conclusion/outlook/discussion section"
    ]


def test_pack_warnings_recognize_codes_with_or_without_messages() -> None:
    acquired = _acquired()
    acquired["2301.00001"]["conclusion"] = None
    acquired["2301.00001"]["warnings"] = [
        {"code": "toc_unavailable"},
        {"code": "references_unavailable"},
        {"code": "conclusion_section_unavailable"},
    ]
    acquired["arXiv:2401.00002"]["warnings"] = [
        {"code": "toc_unavailable", "message": "document parse failed"},
        {"code": "references_unavailable", "message": "source unavailable"},
        {
            "code": "conclusion_section_unavailable",
            "message": "section not present",
        },
    ]

    packs = build_domain_packs(_graph(), acquired)
    paper_by_id = {paper["paper_id"]: paper for paper in packs.paper_json_pack["papers"]}
    evidence_by_id = {paper["paper_id"]: paper for paper in packs.evidence_pack["papers"]}

    assert paper_by_id["arXiv:2301.00001"]["warnings"] == [
        "toc_unavailable",
        "references_unavailable",
        "conclusion_section_unavailable",
    ]
    assert evidence_by_id["arXiv:2301.00001"]["warnings"] == [
        "toc_unavailable",
        "references_unavailable",
        "conclusion_section_unavailable",
    ]
    assert evidence_by_id["arXiv:2401.00002"]["warnings"] == [
        "toc_unavailable:document parse failed",
        "references_unavailable:source unavailable",
        "conclusion_section_unavailable:section not present",
    ]
    assert packs.paper_json_pack["warnings"] == [
        "3 papers have no cached ar5iv table of contents",
        "3 papers have no cached reference list",
    ]
