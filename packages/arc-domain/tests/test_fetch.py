from __future__ import annotations

from types import SimpleNamespace

import pytest

from arc_domain.fetch import CONCLUSION_TEXT_LIMIT, DomainPaperAccess


PAPER_ID = "arXiv:2401.00001"


class FakePaperService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_metadata = False
        self.fail_references = False
        self.fail_parse = False
        self.parse_warnings: tuple[str, ...] = ()
        self.document = SimpleNamespace(
            sections=(
                SimpleNamespace(
                    section_id="intro",
                    title="Introduction",
                    level=1,
                    ordinal=0,
                    page_start=1,
                    page_end=2,
                    text="Introduction.",
                ),
                SimpleNamespace(
                    section_id="discussion",
                    title="Discussion",
                    level=1,
                    ordinal=1,
                    page_start=3,
                    page_end=4,
                    text="Discussion.",
                ),
                SimpleNamespace(
                    section_id="conclusions",
                    title="Conclusions",
                    level=1,
                    ordinal=2,
                    page_start=5,
                    page_end=6,
                    text="C" * (CONCLUSION_TEXT_LIMIT + 10),
                ),
            )
        )

    def get_metadata(self, paper_id: str, **kwargs):
        self.calls.append(("metadata", {"paper_id": paper_id, **kwargs}))
        if self.fail_metadata:
            raise RuntimeError("metadata unavailable")
        return {"paper_id": paper_id, "title": "Example"}

    def get_references(self, paper_id: str, **kwargs):
        self.calls.append(("references", {"paper_id": paper_id, **kwargs}))
        if self.fail_references:
            raise RuntimeError("references unavailable")
        return [{"paper_id": "arXiv:2301.00001"}]

    def get_citers(self, paper_id: str, **kwargs):
        self.calls.append(("citers", {"paper_id": paper_id, **kwargs}))
        return [{"paper_id": "arXiv:2501.00001"}]

    def parse_arxiv_auto(self, paper_id: str, **kwargs):
        self.calls.append(("parse", {"paper_id": paper_id, **kwargs}))
        if self.fail_parse:
            raise RuntimeError("cached source unavailable")
        return SimpleNamespace(document=self.document, warnings=self.parse_warnings)

    def table_of_contents(self, document):
        self.calls.append(("toc", {"document": document}))
        return tuple(document.sections)

    def select_section(self, document, selector):
        self.calls.append(("select", {"document": document, "selector": selector}))
        return next(section for section in document.sections if section.section_id == selector)


def test_acquire_pack_record_separates_metadata_references_and_parses_once():
    service = FakePaperService()
    access = DomainPaperAccess(service)

    record = access.acquire_pack_record(PAPER_ID)

    assert [name for name, _ in service.calls] == [
        "metadata",
        "references",
        "parse",
        "toc",
        "select",
    ]
    assert service.calls[0][1] == {"paper_id": PAPER_ID, "refresh": False}
    assert service.calls[1][1] == {
        "paper_id": PAPER_ID,
        "refresh": False,
        "enrich": False,
    }
    assert service.calls[2][1] == {"paper_id": PAPER_ID, "refresh": False}
    assert service.calls[4][1] == {
        "document": service.document,
        "selector": "conclusions",
    }
    assert len(record["toc"]) == 3
    assert record["conclusion"] == {
        "section_id": "conclusions",
        "title": "Conclusions",
        "text": "C" * CONCLUSION_TEXT_LIMIT,
    }
    assert record["warnings"] == []


def test_direct_essential_calls_are_cache_only_and_propagate_failures():
    service = FakePaperService()
    access = DomainPaperAccess(service)

    assert access.citers(PAPER_ID, limit=7, sort="mostcited") == [{"paper_id": "arXiv:2501.00001"}]
    assert service.calls == [
        ("citers", {"paper_id": PAPER_ID, "refresh": False, "limit": 7, "sort": "mostcited"})
    ]
    service.fail_metadata = True
    with pytest.raises(RuntimeError, match="metadata unavailable"):
        access.metadata(PAPER_ID)


def test_acquire_pack_record_preserves_structured_warnings_for_missing_operations():
    service = FakePaperService()
    service.fail_metadata = True
    service.fail_references = True
    service.fail_parse = True

    record = DomainPaperAccess(service).acquire_pack_record(PAPER_ID)

    assert record["metadata"] == {}
    assert record["references"] == []
    assert record["toc"] == []
    assert record["conclusion"] is None
    assert record["warnings"] == [
        {
            "code": "metadata_unavailable",
            "message": "metadata unavailable",
            "stage": "paper_acquisition",
            "paper_id": PAPER_ID,
        },
        {
            "code": "references_unavailable",
            "message": "references unavailable",
            "stage": "paper_acquisition",
            "paper_id": PAPER_ID,
        },
        {
            "code": "toc_unavailable",
            "message": "cached source unavailable",
            "stage": "paper_acquisition",
            "paper_id": PAPER_ID,
        },
        {
            "code": "conclusion_section_unavailable",
            "message": "cached source unavailable",
            "stage": "paper_acquisition",
            "paper_id": PAPER_ID,
        },
    ]


def test_acquire_pack_record_preserves_parse_warnings_with_paper_context():
    service = FakePaperService()
    service.parse_warnings = ("validator source unavailable",)

    record = DomainPaperAccess(service).acquire_pack_record(PAPER_ID)

    assert record["warnings"] == [
        {
            "code": "parse_warning",
            "message": "validator source unavailable",
            "stage": "paper_acquisition",
            "paper_id": PAPER_ID,
        }
    ]


def test_conclusion_selection_prefers_later_top_level_candidate_at_same_priority():
    service = FakePaperService()
    service.document.sections = (
        SimpleNamespace(
            section_id="notation",
            title="Summary of notation",
            level=1,
            ordinal=0,
            page_start=1,
            page_end=1,
            text="Notation.",
        ),
        SimpleNamespace(
            section_id="summary",
            title="Summary",
            level=1,
            ordinal=1,
            page_start=2,
            page_end=2,
            text="Summary.",
        ),
        SimpleNamespace(
            section_id="final",
            title="Conclusions and Outlook",
            level=1,
            ordinal=2,
            page_start=3,
            page_end=3,
            text="Final.",
        ),
        SimpleNamespace(
            section_id="nested",
            title="Conclusion",
            level=2,
            ordinal=3,
            page_start=3,
            page_end=3,
            text="Nested.",
        ),
    )

    record = DomainPaperAccess(service).acquire_pack_record(PAPER_ID)

    assert record["conclusion"] == {
        "section_id": "final",
        "title": "Conclusions and Outlook",
        "text": "Final.",
    }
    assert service.calls[-1] == (
        "select",
        {"document": service.document, "selector": "final"},
    )


def test_conclusion_selection_rejects_substrings_and_qualified_summary_headings():
    service = FakePaperService()
    service.document.sections = (
        SimpleNamespace(
            section_id="preconclusion",
            title="Preconclusion notes",
            level=1,
            ordinal=0,
            page_start=1,
            page_end=1,
            text="Notes.",
        ),
        SimpleNamespace(
            section_id="notation",
            title="Summary of notation",
            level=1,
            ordinal=1,
            page_start=2,
            page_end=2,
            text="Notation.",
        ),
    )

    record = DomainPaperAccess(service).acquire_pack_record(PAPER_ID)

    assert record["conclusion"] is None
    assert record["warnings"] == [
        {
            "code": "conclusion_section_unavailable",
            "message": "No conclusion, summary, discussion, or outlook section was found.",
            "stage": "paper_acquisition",
            "paper_id": PAPER_ID,
        }
    ]
    assert [name for name, _ in service.calls] == [
        "metadata",
        "references",
        "parse",
        "toc",
    ]
