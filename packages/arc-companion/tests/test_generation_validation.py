from __future__ import annotations

from arc_companion.generation_validation import validate_chapter_guide
from arc_companion.rich_text import parse_markdown


def test_chapter_guide_preserves_literal_latex_commands() -> None:
    markdown = (
        r"第一步为 \(p+p\rightarrow d+e^++\nu_e\)，并产生中微子 \(\nu_e\)。"
    )
    guide = validate_chapter_guide(
        {
            "chapter_guide": {
                "title": "质子—质子链",
                "content_markdown": markdown,
            },
            "section_guides": [],
            "companions": [],
            "references": [],
        },
        chapter_id="chapter-1",
        block_ids=("block-1",),
        chapter_anchor_block_id="block-1",
    )

    assert guide["learning_units"][0]["content_markdown"] == markdown


def test_rich_text_parser_does_not_decode_literal_backslash_n() -> None:
    tokens = parse_markdown(r"before\nafter \(\nu_e\)")

    assert all(
        child.type != "softbreak"
        for token in tokens
        for child in token.children or ()
    )
