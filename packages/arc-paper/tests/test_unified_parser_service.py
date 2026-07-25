from __future__ import annotations

import subprocess

import pytest

from arc_paper import (
    ArcPaperService,
    MathSpanKind,
    PDFTextLayer,
    PaperInputError,
    PaperParserService,
    PdftotextExtractor,
    ParsedDocument,
    ReconciliationStatus,
    SourceBundle,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
    ValidationPolicy,
    build_visual_page_review_inputs,
    parsed_document_from_document,
    parsed_document_to_document,
)


class FakePDFTextExtractor:
    contract_id = "arc.paper.tests.fake_pdf_text.v1"

    def __init__(self, values: dict[bytes, PDFTextLayer]):
        self.values = values
        self.calls: list[bytes] = []

    def extract(self, payload: bytes) -> PDFTextLayer:
        self.calls.append(payload)
        return self.values[payload]


def _store(
    repository: SourceRepository,
    payload: bytes,
    source_format: SourceFormat,
    *,
    locator: str = "",
):
    return repository.store_bytes(
        payload,
        source_format=source_format,
        origin=SourceOrigin(SourceOriginKind.LOCAL_IMPORT, locator=locator),
    )


@pytest.mark.parametrize(
    ("source_format", "payload", "expected_tex"),
    [
        (
            SourceFormat.HTML,
            b"<article><h1>Intro</h1><p>Before</p>"
            b"<math alttext='x+y' display='block'></math><p>After</p></article>",
            "x+y",
        ),
        (
            SourceFormat.MARKDOWN,
            b"# Intro\nBefore\n\n$$\nx+y\n$$\n\nAfter\n",
            "x+y",
        ),
        (
            SourceFormat.TEX,
            br"\section{Intro}" b"\nBefore\n" br"\[x+y\]" b"\nAfter\n",
            "x+y",
        ),
        (SourceFormat.PDF, b"%PDF fixture", "x + y"),
    ],
)
def test_public_parser_service_reads_all_formats_from_repository(
    tmp_path, source_format, payload, expected_tex
):
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, source_format)
    extractor = FakePDFTextExtractor(
        {
            b"%PDF fixture": PDFTextLayer(
                ("Intro\nBefore\nx + y (1.1)\nAfter",)
            )
        }
    )

    outcome = PaperParserService(
        repository, pdf_text_extractor=extractor
    ).parse(SourceBundle(primary=artifact))

    assert isinstance(outcome.document, ParsedDocument)
    assert outcome.document.source.content_identity == artifact.content_identity
    assert outcome.document.sections[0].title == "Intro"
    assert outcome.document.math_spans[0].normalized_tex == expected_tex
    assert outcome.document.equations
    assert extractor.calls == ([payload] if source_format is SourceFormat.PDF else [])


def test_html_math_context_does_not_cross_explicit_section_boundaries(tmp_path):
    payload = b"""
    <html><body>
      <section id="S1"><h2>Previous</h2><p>Previous section text.</p></section>
      <section id="S2"><h2>Current</h2>
        <table class="ltx_equation" id="E1">
          <tr><td><math alttext="x = y"></math></td></tr>
        </table>
      </section>
      <section id="S3"><h2>Next</h2><p>Next section text.</p></section>
    </body></html>
    """
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, SourceFormat.HTML)

    document = PaperParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    assert len(document.math_spans) == 1
    assert document.math_spans[0].context_before == ""
    assert document.math_spans[0].context_after == ""


def test_html_math_context_is_read_from_the_containing_section(tmp_path):
    payload = b"""
    <html><body>
      <section id="S1"><h2>Previous</h2><p>Other before.</p></section>
      <section id="S2"><h2>Model</h2>
        <p>Model text before equation.</p>
        <table class="ltx_equation" id="E1">
          <tr><td><math alttext="E = mc^2"></math></td></tr>
        </table>
        <p>Model text after equation.</p>
      </section>
      <section id="S3"><h2>Next</h2><p>Other after.</p></section>
    </body></html>
    """
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, SourceFormat.HTML)

    document = PaperParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document
    span = document.math_spans[0]

    assert span.normalized_tex == "E = mc^2"
    assert span.context_before == "Model text before equation."
    assert span.context_after == "Model text after equation."


def test_html_nested_math_in_equation_container_is_not_duplicated(tmp_path):
    payload = b"""
    <html><body>
      <section id="S1"><h2>Model</h2>
        <table class="ltx_equation" id="E1">
          <tr><td><math alttext="x = y"><mi>x</mi><mo>=</mo><mi>y</mi></math></td></tr>
        </table>
      </section>
    </body></html>
    """
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, SourceFormat.HTML)

    document = PaperParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    assert len(document.math_spans) == 1
    assert document.math_spans[0].source_label == "E1"
    assert document.math_spans[0].normalized_tex == "x = y"


def test_tex_comment_environment_excludes_sections_and_math_but_keeps_lines(
    tmp_path,
):
    payload = "\n".join(
        [
            r"\section{Active}",
            "Visible text.",
            r"\begin{comment}",
            r"\section{Hidden}",
            r"\begin{equation}",
            r"x = y",
            r"\end{equation}",
            r"\end{comment}",
            "Still visible.",
            r"\begin{equation}",
            r"z = w",
            r"\end{equation}",
        ]
    ).encode()
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, SourceFormat.TEX)

    document = PaperParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    assert [section.title for section in document.sections] == ["Active"]
    assert len(document.math_spans) == 1
    span = document.math_spans[0]
    assert span.normalized_tex == "z = w"
    assert span.context_before == "Still visible."
    assert span.source_line_start == 10


def test_tex_percent_comments_exclude_sections_and_math_but_keep_lines(
    tmp_path,
):
    payload = "\n".join(
        [
            r"\section{Active}",
            "Visible text.",
            r"% \section{Hidden}",
            r"% \begin{equation}",
            r"% x = y",
            r"% \end{equation}",
            r"\begin{equation}",
            r"z = w",
            r"\end{equation}",
        ]
    ).encode()
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, SourceFormat.TEX)

    document = PaperParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    assert [section.title for section in document.sections] == ["Active"]
    assert len(document.math_spans) == 1
    span = document.math_spans[0]
    assert span.normalized_tex == "z = w"
    assert span.source_line_start == 7


def test_markdown_math_manifest_covers_inline_and_display_with_stable_positions(tmp_path):
    payload = (
        b"# Dynamics\n"
        b"The invariant $E = mc^2$ controls the system.\n"
        b"\n"
        b"Before display.\n"
        b"\\[\n"
        b"a^2 + b^2 = c^2\n"
        b"\\]\n"
        b"After display.\n"
        b"`$not_math$`\n"
    )
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, SourceFormat.MARKDOWN)
    service = PaperParserService(repository)

    first = service.parse(SourceBundle(primary=artifact)).document
    second = service.parse(SourceBundle(primary=artifact)).document

    assert [span.kind for span in first.math_spans] == [
        MathSpanKind.INLINE,
        MathSpanKind.DISPLAY,
    ]
    inline, display = first.math_spans
    assert (inline.source_line_start, inline.source_column_start) == (2, 15)
    assert inline.normalized_tex == "E = mc^2"
    assert display.source_line_start == 5
    assert display.source_line_end == 7
    assert display.context_before == "Before display."
    assert display.context_after == "After display."
    assert [span.span_id for span in first.math_spans] == [
        span.span_id for span in second.math_spans
    ]
    assert [item["id"] for item in first.equations] == [display.span_id]


def test_markdown_indented_code_blocks_are_excluded_from_math_manifest(tmp_path):
    payload = (
        b"# Example\n"
        b"\n"
        b"    $not_inline_math$\n"
        b"    $$not_display_math$$\n"
        b"\t\\(also_not_math\\)\n"
        b"\n"
        b"- A list item\n"
        b"\n"
        b"    whose continuation contains $list_math$.\n"
        b"\n"
        b"$$\n"
        b"\n"
        b"    display_math\n"
        b"$$\n"
        b"\n"
        b"The real expression is $x+y$.\n"
    )
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, SourceFormat.MARKDOWN)

    document = PaperParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    assert [span.normalized_tex for span in document.math_spans] == [
        "list_math",
        "display_math",
        "x+y",
    ]


def test_markdown_block_quotes_apply_indented_code_rules_per_container(tmp_path):
    payload = (
        b">     $quoted_code$\n"
        b">\n"
        b"> >     $$nested_quoted_code$$\n"
        b"> >\n"
        b"> > - A nested list item\n"
        b"> >\n"
        b"> >     whose continuation contains $nested_list_math$.\n"
        b"\n"
        b"> Quoted prose contains $quoted_math$.\n"
    )
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, SourceFormat.MARKDOWN)

    document = PaperParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    assert [span.normalized_tex for span in document.math_spans] == [
        "nested_list_math",
        "quoted_math",
    ]


def test_markdown_block_quote_fences_use_container_relative_content(tmp_path):
    payload = (
        b"> ```text\n"
        b"> $quoted_fenced_code$\n"
        b"> ```\n"
        b">\n"
        b"> > ~~~\n"
        b"> > $$nested_quoted_fenced_code$$\n"
        b"> > ~~~\n"
        b"\n"
        b"Outside the fences is $real_math$.\n"
    )
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, SourceFormat.MARKDOWN)

    document = PaperParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    assert [span.normalized_tex for span in document.math_spans] == ["real_math"]


def test_outer_quote_fence_contains_nested_quote_content(tmp_path):
    payload = (
        b"> ```text\n"
        b"> > Nested quote code contains $not_math$.\n"
        b"> ```\n"
        b"\n"
        b"Outside is $real_math$.\n"
    )
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, SourceFormat.MARKDOWN)

    document = PaperParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    assert [span.normalized_tex for span in document.math_spans] == ["real_math"]


def test_unclosed_quote_fence_does_not_leak_into_later_quote(tmp_path):
    payload = (
        b"> ```text\n"
        b"> $not_math$\n"
        b"\n"
        b"> A new quote contains $real_math$.\n"
    )
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(repository, payload, SourceFormat.MARKDOWN)

    document = PaperParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    assert [span.normalized_tex for span in document.math_spans] == ["real_math"]


def test_validators_are_independent_and_conflicts_never_overwrite_primary(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    primary = _store(
        repository,
        b"# Dynamics\nThe equation is $x+y$.\n",
        SourceFormat.MARKDOWN,
    )
    agreeing = _store(
        repository,
        b"<article><h1>Dynamics</h1><p>The equation is "
        b"<math alttext='x+y'></math>.</p></article>",
        SourceFormat.HTML,
    )
    conflicting = _store(
        repository,
        br"\section{Dynamics}" b"\nThe equation is $x-y$.\n",
        SourceFormat.TEX,
    )

    outcome = PaperParserService(repository).parse(
        SourceBundle(primary=primary, validators=(conflicting, agreeing))
    )

    assert outcome.document.math_spans[0].normalized_tex == "x+y"
    by_validator = {
        artifact.artifact_digest: [
            entry
            for entry in outcome.report.entries
            if entry.validator.artifact_digest == artifact.artifact_digest
            and entry.subject_id != "structure"
        ]
        for artifact in (agreeing, conflicting)
    }
    assert any(
        entry.status is ReconciliationStatus.VERIFIED
        for entry in by_validator[agreeing.artifact_digest]
    )
    assert any(
        entry.status is ReconciliationStatus.MISMATCH
        for entry in by_validator[conflicting.artifact_digest]
    )
    assert all(
        entry.provenance.get("observed_tex") != outcome.document.math_spans[0].normalized_tex
        for entry in by_validator[conflicting.artifact_digest]
        if entry.status is ReconciliationStatus.MISMATCH
    )


def test_scanned_pdf_validator_is_successful_partial_and_visual_hook_is_pagewise(
    tmp_path,
):
    repository = SourceRepository(tmp_path / "cache")
    primary = _store(
        repository, b"# Notes\nInline $x+y$.\n", SourceFormat.MARKDOWN
    )
    pdf = _store(repository, b"%PDF scanned", SourceFormat.PDF)
    extractor = FakePDFTextExtractor(
        {
            b"%PDF scanned": PDFTextLayer(
                ("", ""), "PDF contains no extractable text layer; partial parse retained"
            )
        }
    )
    service = PaperParserService(repository, pdf_text_extractor=extractor)

    outcome = service.parse(
        SourceBundle(primary=primary, validators=(pdf,)),
        policy=ValidationPolicy.VISUAL_ALL_PAGES,
    )
    parsed_pdf = service.parse_source(pdf)  # noqa: SLF001 - visual handoff fixture
    requests = build_visual_page_review_inputs(outcome.document, parsed_pdf)

    assert outcome.document.math_spans[0].normalized_tex == "x+y"
    assert any("no extractable text layer" in item for item in outcome.warnings)
    assert outcome.report.entries[0].status is ReconciliationStatus.UNREVIEWED
    assert [request.page_number for request in requests] == [1, 2]
    assert all(
        request.math_span_ids == (outcome.document.math_spans[0].span_id,)
        for request in requests
    )


def test_scanned_pdf_primary_returns_partial_document_instead_of_failing(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    pdf = _store(repository, b"%PDF scanned primary", SourceFormat.PDF)
    extractor = FakePDFTextExtractor(
        {
            b"%PDF scanned primary": PDFTextLayer(
                ("",), "PDF contains no extractable text layer; partial parse retained"
            )
        }
    )

    outcome = PaperParserService(
        repository, pdf_text_extractor=extractor
    ).parse(SourceBundle(primary=pdf))

    assert outcome.document.source_format is SourceFormat.PDF
    assert len(outcome.document.pages) == 1
    assert outcome.document.math_spans == ()
    assert outcome.document.metadata["text_layer"] is False
    assert "partial parse retained" in outcome.warnings[0]


@pytest.mark.parametrize(
    ("source_format", "source_payload"),
    [
        (
            SourceFormat.MARKDOWN,
            b"# Dynamics\n\n$$x+y \\tag {2.1}$$\n",
        ),
        (
            SourceFormat.TEX,
            b"\\section{Dynamics}\n\\[x+y \\tag {2.1}\\]\n",
        ),
    ],
)
def test_pdf_validator_records_deterministic_page_and_printed_number_evidence(
    tmp_path,
    source_format,
    source_payload,
):
    repository = SourceRepository(tmp_path / "cache")
    primary = _store(
        repository,
        source_payload,
        source_format,
    )
    pdf = _store(repository, b"%PDF deterministic", SourceFormat.PDF)
    extractor = FakePDFTextExtractor(
        {
            b"%PDF deterministic": PDFTextLayer(
                ("Front matter", "Dynamics\nx + y (2.1)")
            )
        }
    )

    outcome = PaperParserService(
        repository, pdf_text_extractor=extractor
    ).parse(
        SourceBundle(primary=primary, validators=(pdf,)),
        policy=ValidationPolicy.DETERMINISTIC_ONLY,
    )
    span = outcome.document.math_spans[0]
    entry = next(item for item in outcome.report.entries if item.subject_id == span.span_id)

    assert span.normalized_tex == "x+y"
    assert span.source_label == "2.1"
    assert entry.status is ReconciliationStatus.VERIFIED
    assert entry.provenance["page_candidates"] == [2]
    assert entry.provenance["printed_equation_number"] == "2.1"


def test_non_pdf_primary_fails_before_text_extraction(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    payload = b"this is not a PDF"
    artifact = _store(repository, payload, SourceFormat.PDF)
    extractor = FakePDFTextExtractor(
        {payload: PDFTextLayer(("",), "no extractable text layer")}
    )

    with pytest.raises(Exception) as error:
        PaperParserService(
            repository, pdf_text_extractor=extractor
        ).parse(SourceBundle(primary=artifact))

    assert getattr(error.value, "code", "") == "pdf_invalid"
    assert extractor.calls == []


def test_pdftotext_rejection_is_a_parse_failure_not_a_missing_text_layer(
    tmp_path, monkeypatch
):
    repository = SourceRepository(tmp_path / "cache")
    payload = b"%PDF-1.7\nmalformed body"
    artifact = _store(repository, payload, SourceFormat.PDF)

    def reject(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="invalid PDF")

    monkeypatch.setattr(subprocess, "run", reject)

    with pytest.raises(Exception) as error:
        PaperParserService(
            repository,
            pdf_text_extractor=PdftotextExtractor(),
        ).parse(SourceBundle(primary=artifact))

    assert getattr(error.value, "code", "") == "pdf_invalid"


def test_equal_count_rich_validator_uses_sequence_for_every_conflict(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    primary = _store(
        repository,
        b"# Equations\n$x$\n$y$\n$z$\n",
        SourceFormat.MARKDOWN,
    )
    validator = _store(
        repository,
        b"# Equations\n$a$\n$b$\n$c$\n",
        SourceFormat.MARKDOWN,
    )

    outcome = PaperParserService(repository).parse(
        SourceBundle(primary=primary, validators=(validator,))
    )
    math_entries = [
        entry
        for entry in outcome.report.entries
        if entry.subject_id != "structure"
    ]

    assert [entry.status for entry in math_entries] == [
        ReconciliationStatus.MISMATCH,
        ReconciliationStatus.MISMATCH,
        ReconciliationStatus.MISMATCH,
    ]
    assert [
        entry.provenance.get("matching_method") for entry in math_entries
    ] == ["sequence", "sequence", "sequence"]
    assert not any(entry.subject_id.startswith("validator:") for entry in math_entries)


def test_injected_repository_is_the_default_request_cache_root(tmp_path):
    repository = SourceRepository(tmp_path / "sources")

    service = ArcPaperService(repository=repository)

    assert service.repository is repository
    assert service.ar5iv.cache.root == repository.root
    assert service.arxiv_pdf.cache.root == repository.root
    assert service.inspire.cache.root == repository.root


def test_injected_repository_rejects_a_different_explicit_cache_root(tmp_path):
    repository = SourceRepository(tmp_path / "sources")

    with pytest.raises(PaperInputError, match="must match"):
        ArcPaperService(
            repository=repository,
            cache_root=tmp_path / "request-cache",
        )


def test_same_bytes_different_paths_have_same_document_and_span_identity(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    first_path = tmp_path / "one.md"
    second_path = tmp_path / "nested" / "two.md"
    second_path.parent.mkdir()
    payload = b"# Identity\nInline $x+y$.\n"
    first_path.write_bytes(payload)
    second_path.write_bytes(payload)
    first_artifact = repository.import_path(first_path)
    second_artifact = repository.import_path(second_path)
    service = PaperParserService(repository)

    first = service.parse(SourceBundle(primary=first_artifact)).document
    second = service.parse(SourceBundle(primary=second_artifact)).document

    assert first_artifact.origin.locator != second_artifact.origin.locator
    assert first_artifact.content_identity == second_artifact.content_identity
    assert first.document_digest == second.document_digest
    assert first.math_spans[0].span_id == second.math_spans[0].span_id


def test_parsed_document_codec_round_trips_and_rejects_unknown_fields(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(
        repository, b"# Codec\nInline $x+y$.\n", SourceFormat.MARKDOWN
    )
    parsed = PaperParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    encoded = parsed_document_to_document(parsed)
    decoded = parsed_document_from_document(encoded)

    assert decoded.document_digest == parsed.document_digest
    assert decoded.source.content_identity == parsed.source.content_identity
    invalid = {**encoded, "unknown": True}
    with pytest.raises(ValueError, match="invalid fields"):
        parsed_document_from_document(invalid)
    corrupt = {
        **encoded,
        "math_spans": [
            {**encoded["math_spans"][0], "normalized_tex": "changed"}
        ],
    }
    with pytest.raises(ValueError, match="digest"):
        parsed_document_from_document(corrupt)


def test_single_file_tex_rejects_input_include(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(
        repository,
        br"\section{Main}" b"\n" br"\input{chapter}",
        SourceFormat.TEX,
    )

    with pytest.raises(Exception) as error:
        PaperParserService(repository).parse(SourceBundle(primary=artifact))

    assert getattr(error.value, "code", "") == "unsupported_tex_project"
