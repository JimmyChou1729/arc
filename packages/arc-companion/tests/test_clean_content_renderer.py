from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import zlib

import pytest
from bs4 import BeautifulSoup

from arc_companion.contracts import (
    AcceptedBook,
    AcceptedChapter,
    ChapterPlan,
    CompanionContentCodec,
    ContentCodecError,
    EvidenceRequest,
    EvidenceSource,
    GlossaryEntry,
    LearningUnit,
    PlannedLearningUnit,
    SourceAnchor,
    TranslatedBlock,
)
from arc_companion.renderer import (
    PDF_RENDER_RECIPE,
    WEB_RENDER_RECIPE,
    CompanionRenderer,
    CompanionRenderError,
    _anchor_token,
    _normalize_pdf_search_text,
    _pdf_bibliography_text_contains,
    _pdf_text_contains,
    _render_tex,
    _render_tex_prose,
)
from arc_companion.project import CompanionProjectPaths
from arc_companion.release import CompanionReleasePublisher
from arc_companion.validation import (
    AcceptedBookValidationError,
    require_valid_accepted_book,
    validate_accepted_book,
)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


_PNG = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
    + _png_chunk(b"IDAT", zlib.compress((b"\x00" + b"\x36\x7a\xa4" * 2) * 2))
    + _png_chunk(b"IEND", b"")
)
_PNG_DIGEST = hashlib.sha256(_PNG).hexdigest()
_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="16" '
    b'viewBox="0 0 24 16"><rect width="24" height="16" fill="#367aa4"/></svg>'
)
_SVG_DIGEST = hashlib.sha256(_SVG).hexdigest()
_EPS = b"""%!PS-Adobe-3.0 EPSF-3.0
%%BoundingBox: 0 0 180 90
%%LanguageLevel: 2
%%Pages: 1
%%EndComments
0.15 0.42 0.68 setrgbcolor
10 10 160 70 rectfill
1 1 1 setrgbcolor
/Helvetica findfont 16 scalefont setfont
42 40 moveto (EPS fixture) show
showpage
%%EOF
"""
_EPS_DIGEST = hashlib.sha256(_EPS).hexdigest()
_PDF_SOURCE_EPS = b"""%!PS-Adobe-3.0 EPSF-3.0
%%BoundingBox: 0 0 180 90
%%LanguageLevel: 2
%%Pages: 1
%%EndComments
0.68 0.29 0.18 setrgbcolor
10 10 160 70 rectfill
showpage
%%EOF
"""
_PDF_TOOLS = (
    "latexmk",
    "xelatex",
    "pdfinfo",
    "pdftotext",
    "pdffonts",
    "pdftoppm",
)


def _plain_inline(text: str) -> dict[str, object]:
    return {
        "text": text,
        "inline_spans": (
            {"kind": "text", "start": 0, "end": len(text), "text": text},
        ),
    }


def _pdf_from_eps(path: Path) -> bytes:
    completed = subprocess.run(
        [
            "gs",
            "-q",
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-dEPSCrop",
            "-sDEVICE=pdfwrite",
            f"-sOutputFile={path}",
            "-",
        ],
        input=_PDF_SOURCE_EPS,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8", errors="replace"
    )
    return path.read_bytes()


def _book_with_pdf_and_eps_figures(
    accepted_book: AcceptedBook,
    *,
    pdf_data: bytes,
) -> AcceptedBook:
    chapter = accepted_book.chapters[0]
    existing = chapter.source_anchors[-1]
    pdf_digest = hashlib.sha256(pdf_data).hexdigest()
    pdf_figure = replace(
        existing,
        payload={
            "asset_digest": pdf_digest,
            "alt_text": "A PDF state-space diagram.",
            "caption": "Typed PDF figure",
            "target": "state-space.pdf",
            "media_type": "application/pdf",
            "logical_name": "state-space.pdf",
            "size": len(pdf_data),
        },
    )
    eps_figure = replace(
        existing,
        block_id="b-eps",
        ordinal=existing.ordinal + 1,
        payload={
            "asset_digest": _EPS_DIGEST,
            "alt_text": "An EPS state-space diagram.",
            "caption": "Typed EPS figure",
            "target": "state-space.eps",
            "media_type": "application/postscript",
            "logical_name": "state-space.eps",
            "size": len(_EPS),
        },
    )
    return replace(
        accepted_book,
        chapters=(
            replace(
                chapter,
                source_anchors=chapter.source_anchors[:-1]
                + (pdf_figure, eps_figure),
                translations=chapter.translations
                + (TranslatedBlock(block_id="b-eps", text="Spanish b-eps"),),
            ),
        ),
    )


@pytest.fixture
def accepted_book() -> AcceptedBook:
    intro_text = r"A state follows source note with $\sum_i p_i=1$ normalized distribution."
    link_start = intro_text.index("source note")
    link_end = link_start + len("source note")
    math_source = r"$\sum_i p_i=1$"
    math_start = intro_text.index(math_source)
    math_end = math_start + len(math_source)
    anchors = (
        SourceAnchor(
            block_id="b-intro",
            ordinal=0,
            kind="paragraph",
            section_path=("intro",),
            locator={
                "source_format": "markdown",
                "line_start": 3,
                "column_start": 1,
                "line_end": 3,
                "column_end": 58,
                "selector": "",
                "source_id": "",
            },
            payload={
                "text": intro_text,
                "inline_spans": (
                    {
                        "kind": "text",
                        "start": 0,
                        "end": link_start,
                        "text": intro_text[:link_start],
                    },
                    {
                        "kind": "link",
                        "start": link_start,
                        "end": link_end,
                        "text": "source note",
                        "target": "https://example.test/note",
                    },
                    {
                        "kind": "text",
                        "start": link_end,
                        "end": math_start,
                        "text": intro_text[link_end:math_start],
                    },
                    {
                        "kind": "math",
                        "start": math_start,
                        "end": math_end,
                        "text": math_source,
                        "tex": r"\sum_i p_i=1",
                        "source": math_source,
                    },
                    {
                        "kind": "text",
                        "start": math_end,
                        "end": len(intro_text),
                        "text": intro_text[math_end:],
                    },
                ),
            },
            page_number=2,
        ),
        SourceAnchor(
            block_id="b-equation",
            ordinal=1,
            kind="equation",
            section_path=("intro",),
            locator={},
            payload={"tex": r"S=-\sum_i p_i\log p_i", "display": True, "label": "1"},
            page_number=2,
        ),
        SourceAnchor(
            block_id="b-table",
            ordinal=2,
            kind="table",
            section_path=("intro",),
            locator={},
            payload={
                "headers": ("State", "Weight"),
                "rows": (("ground", "p_0"), ("excited", "p_1")),
                "caption": "Two-state example",
            },
            page_number=3,
        ),
        SourceAnchor(
            block_id="b-list",
            ordinal=3,
            kind="list",
            section_path=("intro",),
            locator={},
            payload={
                "ordered": False,
                "items": (
                    _plain_inline("Normalize probabilities"),
                    _plain_inline("Evaluate entropy"),
                ),
            },
            page_number=3,
        ),
        SourceAnchor(
            block_id="b-figure",
            ordinal=4,
            kind="figure",
            section_path=("intro",),
            locator={},
            payload={
                "asset_digest": _PNG_DIGEST,
                "alt_text": "A one-pixel state-space diagram.",
                "caption": "State-space diagram",
                "target": "state-space.png",
                "media_type": "image/png",
                "logical_name": "state-space.png",
                "size": len(_PNG),
            },
            page_number=4,
        ),
    )
    translations = tuple(
        TranslatedBlock(block_id=item.block_id, text=f"Spanish {item.block_id}")
        for item in anchors
    )
    chapter = AcceptedChapter(
        chapter_id="intro",
        title="Probability and entropy",
        guide="Read normalization first, then connect it to entropy.",
        source_anchors=anchors,
        translations=translations,
        learning_units=(
            LearningUnit(
                unit_id="intuition",
                kind="intuition",
                title="Conservation intuition",
                anchor_ids=("b-intro",),
                content="Normalization says the alternatives exhaust the state space.",
            ),
            LearningUnit(
                unit_id="derivation",
                kind="derivation",
                title="Entropy derivation",
                anchor_ids=("b-equation", "b-table"),
                content="Insert the table weights into the displayed sum.",
            ),
            LearningUnit(
                unit_id="reading",
                kind="further_reading",
                title="Further reading",
                anchor_ids=("b-figure",),
                content="Compare the state-space picture with the cited construction.",
                citations=("paper:1234.56789",),
            ),
        ),
    )
    return AcceptedBook(
        document_digest="d" * 64,
        title="A compact fixture companion",
        source_language="en",
        target_language="es",
        translation_mode="enabled",
        chapters=(chapter,),
        glossary=(
            GlossaryEntry(
                entry_id="entropy",
                term="entropy",
                translated_term="entropia",
                definition="A measure formed from weighted logarithms.",
                anchor_ids=("b-equation",),
                citations=("glossary:entropy-reference",),
            ),
        ),
        bibliography=(
            EvidenceSource(
                evidence_id="paper:1234.56789",
                title="A reference paper",
                source="https://example.test/paper",
            ),
            EvidenceSource(
                evidence_id="glossary:entropy-reference",
                title="Entropy reference",
                source="https://example.test/entropy",
            ),
        ),
    )


def test_accepted_book_codec_is_canonical_strict_and_immutable(
    accepted_book: AcceptedBook,
) -> None:
    encoded = CompanionContentCodec.dumps(accepted_book)
    decoded = CompanionContentCodec.loads(encoded)

    assert decoded == accepted_book
    assert CompanionContentCodec.dumps(decoded) == encoded
    assert decoded.content_digest == accepted_book.content_digest
    with pytest.raises(TypeError):
        decoded.chapters[0].source_anchors[0].payload["text"] = "changed"

    document = CompanionContentCodec.to_document(accepted_book)
    document["diagnostics"] = {"provider": "must stay outside content"}
    with pytest.raises(ContentCodecError, match="invalid fields"):
        CompanionContentCodec.from_document(document)
    document = CompanionContentCodec.to_document(accepted_book)
    document["chapters"] = tuple(document["chapters"])
    with pytest.raises(ContentCodecError, match="arrays must be lists"):
        CompanionContentCodec.from_document(document)
    document = CompanionContentCodec.to_document(accepted_book)
    document["chapters"][0]["source_anchors"][0]["payload"] = {
        1: "non-string key"
    }
    with pytest.raises(ContentCodecError, match="keys must be strings"):
        CompanionContentCodec.from_document(document)
    document = CompanionContentCodec.to_document(accepted_book)
    document["chapters"][0]["source_anchors"][0]["payload"] = {
        "invalid": float("nan")
    }
    with pytest.raises(ContentCodecError, match="finite"):
        CompanionContentCodec.from_document(document)


def test_tex_prose_renderer_preserves_line_and_paragraph_breaks(
    accepted_book: AcceptedBook,
) -> None:
    assert PDF_RENDER_RECIPE == "arc.companion.pdf.source_anchored.v5"
    assert WEB_RENDER_RECIPE == "arc.companion.web.source_anchored.v4"
    assert (
        _render_tex_prose("first line\r\nsecond line\r\rthird paragraph")
        == r"first line\newline{} second line\par third paragraph"
    )

    chapter = replace(
        accepted_book.chapters[0],
        guide="Guide line one\nGuide line two\n\nGuide paragraph two",
        translations=(
            replace(
                accepted_book.chapters[0].translations[0],
                text=(
                    "Translation line one\nTranslation line two"
                    "\n\nTranslation paragraph two"
                ),
            ),
            *accepted_book.chapters[0].translations[1:],
        ),
        learning_units=(
            replace(
                accepted_book.chapters[0].learning_units[0],
                content=(
                    "Learning line one\nLearning line two"
                    "\n\nLearning paragraph two"
                ),
            ),
            *accepted_book.chapters[0].learning_units[1:],
        ),
    )
    book = replace(
        accepted_book,
        chapters=(chapter,),
        glossary=(
            replace(
                accepted_book.glossary[0],
                definition=(
                    "Definition line one\nDefinition line two"
                    "\n\nDefinition paragraph two"
                ),
            ),
        ),
    )

    tex = _render_tex(
        book,
        source_paths={"b-figure": "source/frozen-fixture.png"},
    )

    assert (
        r"Guide line one\newline{} Guide line two\par Guide paragraph two"
        in tex
    )
    assert (
        r"Translation line one\newline{} Translation line two"
        r"\par Translation paragraph two"
    ) in tex
    assert (
        r"Learning line one\newline{} Learning line two"
        r"\par Learning paragraph two"
    ) in tex
    assert (
        r"Definition line one\newline{} Definition line two"
        r"\par Definition paragraph two"
    ) in tex


def test_renderer_public_import_does_not_load_llm_runtime() -> None:
    package_src = Path(__file__).resolve().parents[1] / "src"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(package_src)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from arc_companion import CompanionRenderer; "
                "assert 'arc_llm' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr

    plan = ChapterPlan(
        chapter_id="intro",
        title="Introduction",
        block_ids=("b-intro",),
        guide="Start from normalization.",
        learning_units=(
            PlannedLearningUnit(
                unit_id="intuition",
                kind="intuition",
                title="Why normalize?",
                anchor_ids=("b-intro",),
                purpose="Connect the equation to exhaustive alternatives.",
            ),
        ),
        glossary_candidates=("entropy",),
        evidence_requests=(
            EvidenceRequest(
                request_id="paper-request",
                kind="paper",
                query="1234.56789",
                purpose="Support further reading.",
                anchor_ids=("b-intro",),
            ),
        ),
    )
    assert CompanionContentCodec.loads_chapter_plan(
        CompanionContentCodec.dumps_chapter_plan(plan)
    ) == plan


def test_business_validation_enforces_coverage_translation_and_evidence(
    accepted_book: AcceptedBook,
) -> None:
    expected = tuple(
        item.block_id
        for item in accepted_book.chapters[0].source_anchors
    )
    require_valid_accepted_book(accepted_book, expected_block_ids=expected)

    chapter = accepted_book.chapters[0]
    incomplete = replace(
        accepted_book,
        chapters=(replace(chapter, translations=chapter.translations[:-1]),),
    )
    codes = {item.code for item in validate_accepted_book(incomplete)}
    assert "translation_coverage" in codes
    with pytest.raises(AcceptedBookValidationError):
        require_valid_accepted_book(incomplete)

    skipped = replace(
        accepted_book,
        translation_mode="skipped",
        chapters=(replace(chapter, translations=()),),
    )
    require_valid_accepted_book(skipped)

    bad_unit = replace(
        chapter.learning_units[-1],
        anchor_ids=("not-a-source-block",),
        citations=(),
    )
    invalid_units = replace(
        accepted_book,
        chapters=(
            replace(
                chapter,
                learning_units=chapter.learning_units[:-1] + (bad_unit,),
            ),
        ),
    )
    codes = {item.code for item in validate_accepted_book(invalid_units)}
    assert {"unknown_learning_anchor", "missing_evidence_citation"} <= codes


def test_accepted_content_rejects_empty_guide_and_translation(
    accepted_book: AcceptedBook,
) -> None:
    chapter = accepted_book.chapters[0]

    with pytest.raises(ValueError, match="accepted chapter"):
        replace(chapter, guide=" \n ")
    with pytest.raises(ValueError, match="non-empty string"):
        replace(chapter.translations[0], text="\t")


def test_web_is_responsive_anchor_interleaved_and_deterministic(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    renderer = CompanionRenderer(
        asset_loader=lambda digest: _PNG if digest == _PNG_DIGEST else None
    )
    reader = tmp_path / "reader"
    index = renderer.render_web(accepted_book, reader)
    first = index.read_bytes()
    assert renderer.render_web(accepted_book, reader).read_bytes() == first

    html = first.decode("utf-8")
    css = (reader / "assets" / "reader.css").read_text(encoding="utf-8")
    javascript = (reader / "assets" / "reader.js").read_text(encoding="utf-8")
    assert '@media (min-width: 900px)' in css
    assert '@media (max-width: 899px)' in css
    assert "grid-template-columns: minmax(0,1fr) minmax(0,1fr) minmax(18rem,.85fr)" in css
    assert css.index(".source-layer { order: 1;") < css.index(
        ".translation-layer { order: 2;"
    ) < css.index(".learning-layer { order: 3;")
    assert html.index('data-source-anchor="b-intro"') < html.index(
        'data-source-anchor="b-equation"'
    )
    intro = html.index('data-source-anchor="b-intro"')
    equation = html.index('data-source-anchor="b-equation"')
    assert intro < html.index("Spanish b-intro", intro, equation)
    assert intro < html.index("Conservation intuition", intro, equation)
    assert "Two-state example" in html
    assert "State-space diagram" in html
    assert "https://example.test/note" in html
    assert html.index("A state follows") < html.index(
        'href="https://example.test/note"'
    ) < html.index('data-tex="\\sum_i p_i=1"') < html.index(
        "normalized distribution"
    )
    assert html.count(">source note</a>") == 1
    assert "source-links" not in html
    assert "paper:1234.56789" in html
    assert "entropia" in html
    assert "<h2>References</h2>" in html
    assert "A reference paper" in html
    assert "https://example.test/paper" in html
    assert "glossary:entropy-reference" in html
    assert "window.katex.render" in javascript
    assert "innerHTML" not in javascript
    assert (reader / "assets" / "katex" / "LICENSE").is_file()
    figure_asset = reader / "assets" / "source" / f"{_PNG_DIGEST}.png"
    assert figure_asset.read_bytes() == _PNG
    assert f'src="assets/source/{_PNG_DIGEST}.png"' in html
    assert (
        reader / f"assets/source/{_PNG_DIGEST}.png"
    ).resolve() == figure_asset.resolve()
    for forbidden in ("provider", "cache", "run_id", "schema_version", "warning"):
        assert forbidden not in html.casefold()


def test_source_fragment_links_resolve_to_release_anchors(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    chapter = accepted_book.chapters[0]
    intro = chapter.source_anchors[0]
    equation = chapter.source_anchors[1]
    intro_payload = dict(intro.payload)
    intro_payload["inline_spans"] = tuple(
        (
            {**dict(span), "target": "#equation-source"}
            if span["kind"] == "link"
            else dict(span)
        )
        for span in intro.payload["inline_spans"]
    )
    exact_chapter = replace(
        chapter,
        source_anchors=(
            replace(intro, payload=intro_payload),
            replace(
                equation,
                locator={
                    **dict(equation.locator),
                    "source_id": "equation-source",
                },
            ),
            *chapter.source_anchors[2:],
        ),
    )
    book = replace(accepted_book, chapters=(exact_chapter,))
    renderer = CompanionRenderer(
        asset_loader=lambda digest: _PNG if digest == _PNG_DIGEST else None
    )

    html = renderer.render_web(book, tmp_path / "fragment-reader").read_text(
        encoding="utf-8"
    )
    tex = _render_tex(
        book,
        source_paths={"b-figure": "source/frozen-fixture.png"},
    )
    anchor = _anchor_token("b-equation")

    assert f'href="#anchor-{anchor}">source note</a>' in html
    assert rf"\hyperlink{{anchor-{anchor}}}{{source note}}" in tex
    assert "#equation-source" not in html
    assert "#equation-source" not in tex
    assert _anchor_token("a_b") != _anchor_token("a-b")
    assert re.fullmatch(r"[0-9a-f]{64}", _anchor_token("a_b"))


@pytest.mark.parametrize("target", ["#missing", "appendix.html", "../notes.md"])
def test_unresolved_or_relative_source_links_fail_before_render(
    accepted_book: AcceptedBook, tmp_path: Path, target: str
) -> None:
    chapter = accepted_book.chapters[0]
    intro = chapter.source_anchors[0]
    payload = dict(intro.payload)
    payload["inline_spans"] = tuple(
        (
            {**dict(span), "target": target}
            if span["kind"] == "link"
            else dict(span)
        )
        for span in intro.payload["inline_spans"]
    )
    book = replace(
        accepted_book,
        chapters=(
            replace(
                chapter,
                source_anchors=(
                    replace(intro, payload=payload),
                    *chapter.source_anchors[1:],
                ),
            ),
        ),
    )
    reader = tmp_path / "rejected-reader"

    with pytest.raises(
        CompanionRenderError,
        match="does not resolve|not included in the release",
    ):
        CompanionRenderer().render_web(book, reader)

    assert not reader.exists()


def test_bibliography_uses_canonical_paper_landing_links(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    first = replace(
        accepted_book.bibliography[0],
        source="arXiv:0911.3380v2",
    )
    book = replace(
        accepted_book,
        bibliography=(first, *accepted_book.bibliography[1:]),
    )
    renderer = CompanionRenderer(
        asset_loader=lambda digest: _PNG if digest == _PNG_DIGEST else None
    )

    html = renderer.render_web(book, tmp_path / "bibliography-reader").read_text(
        encoding="utf-8"
    )
    parsed = BeautifulSoup(html, "html.parser")
    tex = _render_tex(
        book,
        source_paths={"b-figure": "source/frozen-fixture.png"},
    )

    canonical = "https://arxiv.org/abs/0911.3380"
    assert f'<a href="{canonical}"><strong>A reference paper</strong></a>' in html
    assert (
        rf"\href{{{canonical}}}{{\textbf{{A reference paper}}}}" in tex
    )
    rows = parsed.select("section.bibliography > ol > li")
    assert len(rows) == len(book.bibliography)
    assert all(row.find("li") is None for row in rows)


def test_pdf_search_normalization_matches_unicode_and_line_wrapping() -> None:
    extracted = (
        "A \N{LATIN SMALL LIGATURE FI}nite decomposition at "
        "https://example.test/very/\nlong/path "
        "with soft\u00adhyphen."
    )

    assert _pdf_text_contains(extracted, "A finite decomposition")
    assert _pdf_text_contains(
        extracted, "https://example.test/very/long/path"
    )
    assert _pdf_text_contains(extracted, "with softhyphen")
    assert not _pdf_text_contains(extracted, "different searchable prose")


def test_pdf_search_normalization_preserves_word_and_hyphen_boundaries() -> None:
    assert _pdf_text_contains("foo\nbar", "foo bar")
    assert not _pdf_text_contains("foo\nbar", "foobar")
    assert not _pdf_text_contains("foobar", "foo bar")
    assert not _pdf_text_contains("foo bar", "foobar")
    assert _pdf_text_contains(
        "https://example.test/foo/\nbar",
        "https://example.test/foo/bar",
    )

    assert _pdf_text_contains("decompo-\nsition", "decomposition")
    assert _pdf_text_contains(
        "pre-check decompo-\nsition",
        "pre-check decomposition",
    )
    assert _pdf_text_contains("equa-\ntion", "equation")
    assert _pdf_text_contains("repre-\nsent", "represent")
    assert _pdf_text_contains("well-\ndefined", "well-defined")
    assert not _pdf_text_contains("welldefined", "well-defined")
    assert not _pdf_text_contains("well defined", "well-defined")


def test_pdf_bibliography_search_tolerates_typesetter_spacing() -> None:
    extracted = (
        "Jean-Pierre Vernant, “Ambiguïté et renversement: sur la structure "
        "énigmatique d’\n    Œdipe-Roi,”Échanges et communications II"
    )
    expected = (
        "Jean-Pierre Vernant, “Ambiguïté et renversement: sur la structure "
        "énigmatique d’Œdipe-Roi,” Échanges et communications II"
    )

    assert not _pdf_text_contains(extracted, expected)
    assert _pdf_bibliography_text_contains(extracted, expected)


def test_headerless_table_uses_row_width_and_omits_empty_web_header(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    chapter = accepted_book.chapters[0]
    table = replace(
        chapter.source_anchors[2],
        payload={
            "headers": (),
            "rows": (("alpha", "1"), ("beta", "2")),
            "caption": "Headerless values",
        },
    )
    table_chapter = replace(
        chapter,
        source_anchors=(table,),
        translations=(
            TranslatedBlock(block_id=table.block_id, text="Translated table."),
        ),
        learning_units=(),
    )
    book = replace(
        accepted_book,
        chapters=(table_chapter,),
        glossary=(),
        bibliography=(),
    )

    require_valid_accepted_book(book)
    html = CompanionRenderer().render_web(
        book, tmp_path / "headerless-reader"
    ).read_text(encoding="utf-8")
    table_html = html[html.index("<table>") : html.index("</table>") + 8]
    assert "<thead>" not in table_html
    assert "<tbody><tr><td>alpha</td><td>1</td></tr>" in table_html

    malformed = replace(
        table,
        payload={
            "headers": (),
            "rows": (("alpha", "1"), ("beta",)),
            "caption": "Broken values",
        },
    )
    invalid = replace(
        book,
        chapters=(replace(table_chapter, source_anchors=(malformed,)),),
    )
    assert "invalid_table_shape" in {
        item.code for item in validate_accepted_book(invalid)
    }


@pytest.mark.skipif(
    any(shutil.which(item) is None for item in _PDF_TOOLS),
    reason="offline PDF toolchain is unavailable",
)
def test_pdf_is_searchable_complete_and_anchor_interleaved(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    renderer = CompanionRenderer(
        asset_loader=lambda digest: _PNG if digest == _PNG_DIGEST else None
    )
    output = renderer.render_pdf(accepted_book, tmp_path / "companion.pdf")
    extracted = subprocess.run(
        ["pdftotext", str(output), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "A compact fixture companion" in extracted
    assert extracted.index("A state follows") < extracted.index(
        "Spanish b-intro"
    ) < extracted.index("Conservation intuition")
    assert "Two-state example" in extracted
    assert "State-space diagram" in extracted
    assert "paper:1234.56789" in extracted
    assert "glossary:entropy-reference" in extracted
    assert "entropia" in extracted
    assert "References" in extracted
    assert "A reference paper" in extracted
    assert "https://example.test/paper" in extracted
    assert not list(tmp_path.glob(".arc-companion-render-*"))
    second = renderer.render_pdf(accepted_book, tmp_path / "companion-copy.pdf")
    assert hashlib.sha256(second.read_bytes()).digest() == hashlib.sha256(
        output.read_bytes()
    ).digest()


@pytest.mark.skipif(
    any(shutil.which(item) is None for item in _PDF_TOOLS),
    reason="offline PDF toolchain is unavailable",
)
def test_pdf_validator_accepts_wrapped_searchable_prose(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    long_title = (
        "A deterministic companion title whose many ordinary words must wrap "
        "across multiple lines while remaining searchable in extracted text"
    )
    long_evidence_title = (
        "A deliberately long bibliography title covering deterministic "
        "normalization source identity and reproducible rendering behavior "
        "across ordinary page boundaries"
    )
    book = replace(
        accepted_book,
        title=long_title,
        bibliography=(
            replace(
                accepted_book.bibliography[0],
                title=long_evidence_title,
                source="doi:10.1234/A%20_&B",
            ),
            *accepted_book.bibliography[1:],
        ),
    )
    renderer = CompanionRenderer(
        asset_loader=lambda digest: _PNG if digest == _PNG_DIGEST else None
    )
    tex = _render_tex(
        book,
        source_paths={"b-figure": "source/frozen-fixture.png"},
    )

    output = renderer.render_pdf(book, tmp_path / "wrapped-searchable.pdf")
    extracted = subprocess.run(
        ["pdftotext", str(output), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert _pdf_text_contains(extracted, long_title)
    assert _pdf_text_contains(extracted, long_evidence_title)
    assert _pdf_text_contains(extracted, "doi:10.1234/A%20_&B")
    assert (
        r"\href{https://doi.org/10.1234/a\%20\_\&b}"
        r"{\textbf{A deliberately long bibliography title"
        in tex
    )
    urls = subprocess.run(
        ["pdfinfo", "-url", str(output)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "https://doi.org/10.1234/a%20_&b" in urls


@pytest.mark.skipif(
    any(shutil.which(item) is None for item in _PDF_TOOLS),
    reason="offline PDF toolchain is unavailable",
)
def test_pdf_preserves_multiline_prose_without_pipeline_diagnostics(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    chapter = replace(
        accepted_book.chapters[0],
        guide=(
            "GUIDE-LINE-ONE\nGUIDE-LINE-TWO"
            "\n\nGUIDE-PARAGRAPH-TWO"
        ),
        translations=(
            replace(
                accepted_book.chapters[0].translations[0],
                text=(
                    "TRANSLATION-LINE-ONE\nTRANSLATION-LINE-TWO"
                    "\n\nTRANSLATION-PARAGRAPH-TWO"
                ),
            ),
            *accepted_book.chapters[0].translations[1:],
        ),
        learning_units=(
            replace(
                accepted_book.chapters[0].learning_units[0],
                content=(
                    "LEARNING-LINE-ONE\nLEARNING-LINE-TWO"
                    "\n\nLEARNING-PARAGRAPH-TWO"
                ),
            ),
            *accepted_book.chapters[0].learning_units[1:],
        ),
    )
    book = replace(
        accepted_book,
        chapters=(chapter,),
        glossary=(
            replace(
                accepted_book.glossary[0],
                definition=(
                    "DEFINITION-LINE-ONE\nDEFINITION-LINE-TWO"
                    "\n\nDEFINITION-PARAGRAPH-TWO"
                ),
            ),
        ),
    )
    renderer = CompanionRenderer(
        asset_loader=lambda digest: _PNG if digest == _PNG_DIGEST else None
    )

    output = renderer.render_pdf(book, tmp_path / "multiline-prose.pdf")
    extracted = subprocess.run(
        ["pdftotext", str(output), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    for prefix in ("GUIDE", "TRANSLATION", "LEARNING", "DEFINITION"):
        first = f"{prefix}-LINE-ONE"
        second = f"{prefix}-LINE-TWO"
        paragraph = f"{prefix}-PARAGRAPH-TWO"
        assert extracted.index(first) < extracted.index(second) < extracted.index(
            paragraph
        )
    assert "arc.companion." not in extracted
    assert "semantic_input" not in extracted
    assert "provider diagnostics" not in extracted


@pytest.mark.skipif(
    any(shutil.which(item) is None for item in _PDF_TOOLS),
    reason="offline PDF toolchain is unavailable",
)
def test_real_renderer_release_manifest_exactly_matches_files(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "release-project")
    renderer = CompanionRenderer(
        asset_loader=lambda digest: _PNG if digest == _PNG_DIGEST else None
    )
    release = CompanionReleasePublisher(project, renderer).publish(
        accepted_book,
        run_id="rendered-release",
    )

    manifest = json.loads(release.manifest.read_text(encoding="utf-8"))
    declared = {item["path"] for item in manifest["files"]}
    actual = {
        path.relative_to(release.directory).as_posix()
        for path in release.directory.rglob("*")
        if path.is_file() and path != release.manifest
    }

    assert declared == actual
    assert project.delivery_pdf.read_bytes() == release.pdf.read_bytes()
    delivered_html = project.delivery_html.read_text(encoding="utf-8")
    assert (
        f'<base href="releases/{release.release_id}/reader/index.html">'
        in delivered_html
    )
    assert 'href="assets/reader.css"' in delivered_html


@pytest.mark.skipif(
    any(shutil.which(item) is None for item in _PDF_TOOLS),
    reason="offline PDF toolchain is unavailable",
)
def test_pdf_longtable_keeps_all_180_rows_and_rasters_every_page(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    rows = tuple(
        (f"record-{index:03d}", f"value-{index:03d}")
        for index in range(180)
    )
    table = replace(
        accepted_book.chapters[0].source_anchors[2],
        payload={
            "headers": (),
            "rows": rows,
            "caption": "All deterministic records",
        },
    )
    chapter = replace(
        accepted_book.chapters[0],
        source_anchors=(table,),
        translations=(
            TranslatedBlock(block_id=table.block_id, text="Translated table."),
        ),
        learning_units=(),
    )
    book = replace(
        accepted_book,
        chapters=(chapter,),
        glossary=(),
        bibliography=(),
    )

    output = CompanionRenderer().render_pdf(book, tmp_path / "longtable.pdf")
    extracted = subprocess.run(
        ["pdftotext", str(output), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    info = subprocess.run(
        ["pdfinfo", str(output)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    page_count = int(
        next(
            line.split(":", 1)[1]
            for line in info.splitlines()
            if line.startswith("Pages:")
        )
    )

    assert page_count >= 3
    assert "All deterministic records" in extracted
    assert "record-000" in extracted
    assert "record-179" in extracted
    assert "value-179" in extracted


def test_unfrozen_remote_figure_is_rendered_as_attributed_link(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    chapter = accepted_book.chapters[0]
    figure = chapter.source_anchors[-1]
    remote = replace(
        figure,
        payload={
            "asset_digest": "",
            "alt_text": "Remote phase portrait",
            "caption": "Externally hosted figure",
            "target": "https://example.test/phase.svg",
            "media_type": "",
            "logical_name": "https://example.test/phase.svg",
            "size": 0,
        },
    )
    book = replace(
        accepted_book,
        chapters=(
            replace(
                chapter,
                source_anchors=chapter.source_anchors[:-1] + (remote,),
            ),
        ),
    )

    index = CompanionRenderer().render_web(book, tmp_path / "reader")
    html = index.read_text(encoding="utf-8")

    figure_start = html.index('data-source-anchor="b-figure"')
    assert "<img" not in html[figure_start:]
    assert "Remote phase portrait" in html[figure_start:]
    assert 'href="https://example.test/phase.svg"' in html[figure_start:]


@pytest.mark.skipif(
    any(shutil.which(item) is None for item in _PDF_TOOLS + ("rsvg-convert",)),
    reason="offline PDF/SVG toolchain is unavailable",
)
def test_pdf_converts_typed_svg_asset(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    chapter = accepted_book.chapters[0]
    figure = chapter.source_anchors[-1]
    svg_figure = replace(
        figure,
        payload={
            "asset_digest": _SVG_DIGEST,
            "alt_text": "A blue rectangle.",
            "caption": "Typed SVG figure",
            "target": "shape.svg",
            "media_type": "image/svg+xml",
            "logical_name": "shape.svg",
            "size": len(_SVG),
        },
    )
    book = replace(
        accepted_book,
        chapters=(
            replace(
                chapter,
                source_anchors=chapter.source_anchors[:-1] + (svg_figure,),
            ),
        ),
    )
    renderer = CompanionRenderer(
        asset_loader=lambda digest: _SVG if digest == _SVG_DIGEST else None
    )

    output = renderer.render_pdf(book, tmp_path / "svg.pdf")
    extracted = subprocess.run(
        ["pdftotext", str(output), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Typed SVG figure" in extracted

    index = renderer.render_web(book, tmp_path / "reader")
    assert (tmp_path / "reader" / "assets" / "source" / f"{_SVG_DIGEST}.svg").read_bytes() == _SVG
    assert f"assets/source/{_SVG_DIGEST}.svg" in index.read_text(encoding="utf-8")


@pytest.mark.skipif(
    any(shutil.which(item) is None for item in ("gs", "pdftocairo")),
    reason="offline PDF/EPS preview toolchain is unavailable",
)
def test_web_previews_pdf_and_eps_and_links_typed_originals(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    pdf_data = _pdf_from_eps(tmp_path / "source-figure.pdf")
    pdf_digest = hashlib.sha256(pdf_data).hexdigest()
    book = _book_with_pdf_and_eps_figures(
        accepted_book,
        pdf_data=pdf_data,
    )
    assets = {
        pdf_digest: pdf_data,
        _EPS_DIGEST: _EPS,
    }
    renderer = CompanionRenderer(asset_loader=assets.get)
    reader = tmp_path / "reader"

    index = renderer.render_web(book, reader)
    html = index.read_text(encoding="utf-8")
    pdf_original = reader / "assets" / "source" / f"{pdf_digest}.pdf"
    pdf_preview = reader / "assets" / "source" / f"{pdf_digest}.preview.png"
    eps_original = reader / "assets" / "source" / f"{_EPS_DIGEST}.eps"
    eps_preview = reader / "assets" / "source" / f"{_EPS_DIGEST}.preview.png"

    assert pdf_original.read_bytes() == pdf_data
    assert eps_original.read_bytes() == _EPS
    assert pdf_preview.stat().st_size > 0
    assert eps_preview.stat().st_size > 0
    assert f'src="assets/source/{pdf_digest}.preview.png"' in html
    assert f'href="assets/source/{pdf_digest}.pdf"' in html
    assert 'type="application/pdf"' in html
    assert f'src="assets/source/{_EPS_DIGEST}.preview.png"' in html
    assert f'href="assets/source/{_EPS_DIGEST}.eps"' in html
    assert 'type="application/postscript"' in html
    assert f'src="assets/source/{pdf_digest}.pdf"' not in html
    assert f'src="assets/source/{_EPS_DIGEST}.eps"' not in html

    preview_digests = (
        hashlib.sha256(pdf_preview.read_bytes()).hexdigest(),
        hashlib.sha256(eps_preview.read_bytes()).hexdigest(),
    )
    renderer.render_web(book, reader)
    assert preview_digests == (
        hashlib.sha256(pdf_preview.read_bytes()).hexdigest(),
        hashlib.sha256(eps_preview.read_bytes()).hexdigest(),
    )


@pytest.mark.skipif(
    any(shutil.which(item) is None for item in _PDF_TOOLS + ("gs",)),
    reason="offline PDF/EPS rendering toolchain is unavailable",
)
def test_pdf_renders_native_pdf_and_converted_eps_figures(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    pdf_data = _pdf_from_eps(tmp_path / "source-figure.pdf")
    pdf_digest = hashlib.sha256(pdf_data).hexdigest()
    book = _book_with_pdf_and_eps_figures(
        accepted_book,
        pdf_data=pdf_data,
    )
    renderer = CompanionRenderer(
        asset_loader={
            pdf_digest: pdf_data,
            _EPS_DIGEST: _EPS,
        }.get
    )

    output = renderer.render_pdf(book, tmp_path / "document-figures.pdf")
    extracted = subprocess.run(
        ["pdftotext", str(output), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "Typed PDF figure" in extracted
    assert "Typed EPS figure" in extracted
    assert output.stat().st_size > 0
    assert not list(tmp_path.glob(".arc-companion-render-*"))


@pytest.mark.skipif(
    any(shutil.which(item) is None for item in _PDF_TOOLS),
    reason="offline PDF toolchain is unavailable",
)
def test_pdf_cards_break_across_pages_for_long_paragraph_and_code(
    accepted_book: AcceptedBook, tmp_path: Path
) -> None:
    long_text = "BEGIN LONG " + " ".join(
        f"anchored sentence {index}." for index in range(900)
    ) + " END LONG"
    paragraph = replace(
        accepted_book.chapters[0].source_anchors[0],
        ordinal=0,
        payload=_plain_inline(long_text),
    )
    code_text = "\n".join(
        ["BEGIN CODE"] + [f"value_{index} = {index}" for index in range(350)] + ["END CODE"]
    )
    code = SourceAnchor(
        block_id="b-code",
        ordinal=1,
        kind="code",
        section_path=("intro",),
        locator={},
        payload={"text": code_text, "language": "python"},
    )
    chapter = replace(
        accepted_book.chapters[0],
        source_anchors=(paragraph, code),
        translations=(
            TranslatedBlock(block_id=paragraph.block_id, text="Translated paragraph."),
            TranslatedBlock(block_id=code.block_id, text="Translated code."),
        ),
        learning_units=(),
    )
    book = replace(accepted_book, chapters=(chapter,), glossary=())

    output = CompanionRenderer().render_pdf(book, tmp_path / "multipage.pdf")
    info = subprocess.run(
        ["pdfinfo", str(output)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    extracted = subprocess.run(
        ["pdftotext", str(output), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    pages = int(next(line.split(":", 1)[1] for line in info.splitlines() if line.startswith("Pages:")))
    assert pages >= 3
    assert "BEGIN LONG" in extracted
    assert "END LONG" in extracted
    assert "BEGIN CODE" in extracted
    assert "END CODE" in extracted
