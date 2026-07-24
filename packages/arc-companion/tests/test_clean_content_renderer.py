from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zlib

import pytest

from arc_companion.contracts import (
    AcceptedBook,
    AcceptedChapter,
    ChapterPlan,
    CompanionContentCodec,
    ContentCodecError,
    GlossaryEntry,
    LearningUnit,
    PlannedLearningUnit,
    SourceAnchor,
    TranslatedBlock,
)
from arc_companion.renderer import CompanionRenderer
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


@pytest.fixture
def accepted_book() -> AcceptedBook:
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
                "text": "A state follows the normalized distribution.",
                "links": (
                    {"text": "source note", "target": "https://example.test/note"},
                ),
                "inline_math": ({"tex": r"\sum_i p_i=1", "source": "sum p_i = 1"},),
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
                    {"text": "Normalize probabilities", "links": ()},
                    {"text": "Evaluate entropy", "links": ()},
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
        evidence_requests=("paper:1234.56789",),
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
    assert "paper:1234.56789" in html
    assert "entropia" in html
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


@pytest.mark.skipif(
    any(shutil.which(item) is None for item in ("latexmk", "xelatex", "pdfinfo", "pdftotext", "pdffonts", "pdftoppm")),
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
    assert not list(tmp_path.glob(".arc-companion-render-*"))
    second = renderer.render_pdf(accepted_book, tmp_path / "companion-copy.pdf")
    assert hashlib.sha256(second.read_bytes()).digest() == hashlib.sha256(
        output.read_bytes()
    ).digest()
