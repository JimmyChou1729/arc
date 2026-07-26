from __future__ import annotations

import pytest

from arc_companion.contracts import (
    AcceptedBook,
    AcceptedChapter,
    EvidenceSource,
    LearningUnit,
    SourceAnchor,
    TranslatedBlock,
)
from arc_companion.reader_labels import ReaderLabelError, resolve_reader_labels
from arc_companion.renderer import _release_links, _render_html, _render_tex
from arc_companion.rich_text import RichTextError, validate_rich_markdown


def _book(*, markdown: str = "正文 [@evidence].") -> AcceptedBook:
    anchor = SourceAnchor(
        block_id="source",
        ordinal=0,
        kind="paragraph",
        section_path=(),
        payload={"inline_spans": [{"kind": "text", "text": "Source text."}]},
        page_number=2,
    )
    return AcceptedBook(
        document_digest="0" * 64,
        title="The Death of the Author",
        authors=("Roland Barthes",),
        source_language="en",
        target_language="zh-CN",
        translation_mode="enabled",
        chapters=(
            AcceptedChapter(
                chapter_id="chapter",
                title="The Death of the Author",
                source_anchors=(anchor,),
                translations=(TranslatedBlock("source", "译文。"),),
                learning_units=(
                    LearningUnit(
                        unit_id="unit",
                        title="一个说明",
                        anchor_ids=("source",),
                        placement="inline",
                        content_markdown=markdown,
                        citations=("evidence",),
                    ),
                ),
            ),
        ),
        bibliography=(
            EvidenceSource("evidence", "A cited work", "https://example.test/work"),
        ),
    )


def test_renderer_uses_localized_chrome_and_compact_markers() -> None:
    book = _book(markdown="**要点**：$x$ [@evidence].")
    links = _release_links(book)

    html = _render_html(book, source_assets={}, release_links=links)
    tex = _render_tex(book, source_paths={}, release_links=links)

    assert "<h1>The Death of the Author</h1>" in html
    assert "<h2>The Death of the Author</h2>" not in html
    assert "作者:</span> Roland Barthes" in html
    assert "原文第 2 页" in html
    assert "术语表" not in html
    assert "参考文献" in html
    assert 'href="#reference-evidence">[1]</a>' in html
    assert "Evidence:" not in html
    assert "Reader question" not in html
    assert ">Source<" not in html
    assert ">Translation<" not in html
    assert "Source-anchored textbook companion" not in html
    assert "pdfauthor={Roland Barthes}" in tex
    assert "\\section{The Death of the Author}" not in tex
    assert "参考文献" in tex
    assert "Evidence:" not in tex
    assert "Reader question" not in tex
    assert "\\textbf{Source}" not in tex
    assert "\\textbf{Translation}" not in tex
    assert "[1] A cited work" not in tex


def test_commonmark_math_and_nearby_citations_share_validation() -> None:
    markdown = "A *point* [@one].\n\n$$\nx^2\n$$\n"

    assert validate_rich_markdown(markdown, allowed_evidence_ids=("one",)) == ("one",)
    with pytest.raises(RichTextError, match="not in bibliography"):
        validate_rich_markdown(markdown, allowed_evidence_ids=())
    with pytest.raises(RichTextError, match="raw HTML"):
        validate_rich_markdown("<mark>no</mark>")


def test_model_markdown_images_are_cross_format_unfrozen_links() -> None:
    book = _book(markdown="![示意图](https://example.test/figure.png)")
    links = _release_links(book)

    html = _render_html(book, source_assets={}, release_links=links)
    tex = _render_tex(book, source_paths={}, release_links=links)

    assert '<img src="https://example.test/figure.png"' not in html
    assert (
        '<a class="unfrozen-image-link" '
        'href="https://example.test/figure.png">示意图</a>'
    ) in html
    assert "\\href{https://example.test/figure.png}{示意图}" in tex


def test_reader_labels_are_complete_and_target_aware() -> None:
    assert resolve_reader_labels("zh-TW")["references"] == "參考文獻"
    with pytest.raises(ReaderLabelError, match="supply a complete"):
        resolve_reader_labels("fr")
    with pytest.raises(ReaderLabelError, match="incomplete"):
        resolve_reader_labels("en", custom_labels={"references": "References"})
