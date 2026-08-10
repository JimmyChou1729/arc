from __future__ import annotations

from arc_companion.generation_validation import (
    validate_chapter_guide,
    validate_chapter_guide_review_audit,
)
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


def test_review_audit_normalizes_repeated_valid_locations() -> None:
    audit = validate_chapter_guide_review_audit(
        {
            "payload": {
                "checked_complete_chapter": True,
                "checked_part_numbers": [3, 1, 3],
                "checked_section_numbers": [2, 2],
            }
        },
        proposal={
            "companions": [{"after_part": 3}],
            "section_guides": [{"section_number": 2}],
        },
        part_count=3,
        section_count=2,
    )

    assert audit == {
        "checked_complete_chapter": True,
        "checked_part_numbers": [1, 3],
        "checked_section_numbers": [2],
    }


def test_review_audit_ignores_unneeded_out_of_range_locations() -> None:
    audit = validate_chapter_guide_review_audit(
        {
            "payload": {
                "checked_complete_chapter": True,
                "checked_part_numbers": [1, 99],
                "checked_section_numbers": [3],
            }
        },
        proposal={"companions": [], "section_guides": []},
        part_count=2,
        section_count=0,
    )

    assert audit["checked_part_numbers"] == [1]
    assert audit["checked_section_numbers"] == []
