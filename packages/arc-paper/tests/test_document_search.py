from __future__ import annotations

import hashlib

import pytest

from arc_paper import (
    DocumentSearchError,
    MathSpan,
    MathSpanKind,
    ParsedDocument,
    ParsedPage,
    ParsedSection,
    SectionSelectionError,
    SourceArtifact,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    TextMatchLocation,
    select_section,
    search_equations,
    search_full_text,
    table_of_contents,
)
from arc_paper.document_search import search_equation_terms


def _source(name: str, source_format: SourceFormat = SourceFormat.MARKDOWN) -> SourceArtifact:
    payload = name.encode()
    media_type = {
        SourceFormat.HTML: "text/html",
        SourceFormat.MARKDOWN: "text/markdown",
        SourceFormat.TEX: "application/x-tex",
        SourceFormat.PDF: "application/pdf",
    }[source_format]
    return SourceArtifact(
        source_format=source_format,
        artifact_digest=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        media_type=media_type,
        origin=SourceOrigin(SourceOriginKind.REPOSITORY, locator=name),
    )


def _document(name: str = "one") -> ParsedDocument:
    return ParsedDocument(
        source=_source(name),
        sections=(
            ParsedSection(
                section_id="intro",
                title="Introduction",
                level=1,
                text="Prior line.\nThe Hamiltonian constraint fixes expansion.\nLater line.",
                ordinal=0,
            ),
            ParsedSection(
                section_id="background",
                title="Background evolution",
                level=1,
                text="The background is slowly varying.",
                ordinal=1,
                page_start=3,
                page_end=4,
            ),
        ),
        math_spans=(
            MathSpan(
                span_id="math-inline",
                kind=MathSpanKind.INLINE,
                source_line_start=4,
                source_column_start=8,
                source_line_end=4,
                source_column_end=11,
                normalized_tex="H^2",
                context_before="The Hubble scale",
                context_after="sets the background.",
            ),
            MathSpan(
                span_id="math-display",
                kind=MathSpanKind.DISPLAY,
                source_line_start=8,
                source_column_start=1,
                source_line_end=8,
                source_column_end=30,
                normalized_tex=r"H^2 = \frac{8\pi G}{3}\rho",
                source_label="eq:friedmann",
                context_before="The Hamiltonian constraint gives",
                context_after="for a flat universe.",
            ),
        ),
    )


def test_full_text_search_returns_typed_stable_section_hits() -> None:
    first = _document()
    second = ParsedDocument(
        source=_source("two"),
        sections=(
            ParsedSection(
                section_id="other",
                title="Other",
                level=1,
                text="Another Hamiltonian constraint.",
                ordinal=0,
            ),
        ),
    )

    result = search_full_text(
        (first, second),
        "hamiltonian   constraint",
        context_lines=1,
        limit=1,
    )

    assert result.searched_documents == 2
    assert result.truncated is True
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.document_digest == first.document_digest
    assert match.location is TextMatchLocation.SECTION
    assert match.location_id == "intro"
    assert match.matched_in == "text"
    assert match.snippet.splitlines() == [
        "Prior line.",
        "The Hamiltonian constraint fixes expansion.",
        "Later line.",
    ]


def test_full_text_search_uses_pages_only_when_sections_are_absent() -> None:
    document = ParsedDocument(
        source=_source("scan", SourceFormat.PDF),
        pages=(
            ParsedPage(1, ""),
            ParsedPage(2, "Observable on a scanned page"),
        ),
    )

    result = search_full_text(document, "observable")

    assert len(result.matches) == 1
    assert result.matches[0].location is TextMatchLocation.PAGE
    assert result.matches[0].page_number == 2


@pytest.mark.parametrize(
    ("query", "matched_in"),
    [
        ("eq:friedmann", "source_label"),
        (r"8\pi G", "math"),
        ("Hamiltonian constraint", "context_before"),
        ("math-inline", "span_id"),
    ],
)
def test_equation_search_covers_all_math_span_fields(
    query: str, matched_in: str
) -> None:
    result = search_equations(_document(), query)

    assert result.matches
    assert result.matches[-1].matched_in == matched_in
    assert {item.kind for item in result.matches} <= {
        MathSpanKind.INLINE,
        MathSpanKind.DISPLAY,
    }


def test_search_rejects_invalid_requests_and_duplicate_documents() -> None:
    document = _document()

    with pytest.raises(DocumentSearchError, match="query is required") as error:
        search_full_text(document, " ")
    assert error.value.code == "invalid_search_request"

    with pytest.raises(DocumentSearchError, match="duplicate content"):
        search_equations((document, document), "H")

    with pytest.raises(DocumentSearchError, match="between 1 and 200"):
        search_full_text(document, "H", limit=201)


def test_equation_search_case_sensitivity_is_explicit() -> None:
    document = _document()

    assert search_equations(document, "hubble").matches
    assert not search_equations(document, "hubble", case_sensitive=True).matches


def test_table_of_contents_is_a_typed_projection_of_sections() -> None:
    document = _document()

    toc = table_of_contents(document)

    assert [item.section_id for item in toc] == ["intro", "background"]
    assert toc[1].title == "Background evolution"
    assert toc[1].page_start == 3
    assert toc[1].page_end == 4


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        (0, "intro"),
        ("INTRO", "intro"),
        ("background evolution", "background"),
        ("evolution", "background"),
    ],
)
def test_select_section_supports_typed_stable_selectors(
    selector: str | int, expected: str
) -> None:
    assert select_section(_document(), selector).section_id == expected


def test_select_section_reports_typed_not_found_and_ambiguity() -> None:
    document = ParsedDocument(
        source=_source("ambiguous"),
        sections=(
            ParsedSection("one", "First result", 1, "", 0),
            ParsedSection("two", "Second result", 1, "", 1),
        ),
    )

    with pytest.raises(SectionSelectionError) as missing:
        select_section(document, "absent")
    assert missing.value.code == "section_not_found"

    with pytest.raises(SectionSelectionError) as ambiguous:
        select_section(document, "result")
    assert ambiguous.value.code == "section_ambiguous"


def test_equation_search_replaces_equation_context_lookup() -> None:
    match = search_equations(_document(), "eq:friedmann").matches[0]

    assert match.normalized_tex == r"H^2 = \frac{8\pi G}{3}\rho"
    assert match.context_before == "The Hamiltonian constraint gives"
    assert match.context_after == "for a flat universe."


def test_equation_term_search_ranks_labels_and_returns_pdf_layout_evidence() -> None:
    source = SourceArtifact(
        source_format=SourceFormat.PDF,
        artifact_digest=hashlib.sha256(b"pdf").hexdigest(),
        size=3,
        media_type="application/pdf",
        origin=SourceOrigin(SourceOriginKind.REPOSITORY),
    )
    document = ParsedDocument(
        source=source,
        pages=(
            ParsedPage(
                1,
                "Lead\nfirst equation (2.3)\nMiddle\nfull multiline tail (2.30)\nEnd",
            ),
        ),
        math_spans=(
            MathSpan(
                span_id="math-30-hash-fragment",
                kind=MathSpanKind.DISPLAY,
                source_line_start=2,
                source_column_start=1,
                source_line_end=2,
                source_column_end=20,
                normalized_tex="x",
                source_label="2.3",
            ),
            MathSpan(
                span_id="math-exact-equation",
                kind=MathSpanKind.DISPLAY,
                source_line_start=4,
                source_column_start=1,
                source_line_end=4,
                source_column_end=30,
                normalized_tex="x+y+z",
                source_label="2.30",
            ),
        ),
    )

    result = search_equation_terms(document, ("2.30",), context_lines=2)

    assert [item.source_label for item in result.matches] == ["2.30"]
    assert result.matches[0].matched_fields == ("source_label",)
    assert result.matches[0].page_candidates == (1,)
    assert "full multiline tail (2.30)" in result.matches[0].source_excerpt

    short = search_equation_terms(document, ("30",), context_lines=0)
    assert [item.source_label for item in short.matches] == ["2.30"]


def test_equation_search_does_not_substring_match_opaque_span_ids() -> None:
    result = search_equations(_document(), "inline")
    assert result.matches == ()
