from __future__ import annotations

import hashlib

import pytest
from arc_paper import (
    RichBlock,
    RichBlockKind,
    SourceFormat,
    SourceLocator,
)
from arc_render import (
    FragmentAnchor,
    FragmentRevision,
    AnchorBlock,
    SourceIdentity,
    block_text_to_markdown,
)


def _block(
    kind: RichBlockKind,
    payload: dict[str, object],
) -> RichBlock:
    return RichBlock(
        block_id=f"block-{kind.value}",
        ordinal=0,
        kind=kind,
        section_path=(),
        locator=SourceLocator(SourceFormat.MARKDOWN, 1, 1, 1, 1),
        payload=payload,
    )


def test_block_text_to_markdown_supplies_source_owned_structure() -> None:
    heading = _block(
        RichBlockKind.HEADING,
        {"text": "Title", "level": 2},
    )
    unordered = _block(
        RichBlockKind.LIST,
        {
            "ordered": False,
                "items": [
                    {
                        "text": "one",
                        "inline_spans": [
                            {
                                "kind": "text",
                                "start": 0,
                                "end": 3,
                                "text": "one",
                            }
                        ],
                    },
                    {
                        "text": "two",
                        "inline_spans": [
                            {
                                "kind": "text",
                                "start": 0,
                                "end": 3,
                                "text": "two",
                            }
                        ],
                    },
            ],
        },
    )
    ordered = _block(
        RichBlockKind.LIST,
        {
            "ordered": True,
                "items": [
                    {
                        "text": "one",
                        "inline_spans": [
                            {
                                "kind": "text",
                                "start": 0,
                                "end": 3,
                                "text": "one",
                            }
                        ],
                    },
                    {
                        "text": "two",
                        "inline_spans": [
                            {
                                "kind": "text",
                                "start": 0,
                                "end": 3,
                                "text": "two",
                            }
                        ],
                    },
            ],
        },
    )
    equation = _block(
        RichBlockKind.EQUATION,
        {"tex": "x=1", "display": True, "label": ""},
    )
    code = _block(
        RichBlockKind.CODE,
        {"text": "value = ```", "language": ""},
    )

    assert block_text_to_markdown(heading, "译名") == "## 译名\n"
    assert block_text_to_markdown(unordered, "一\n二") == "- 一\n- 二\n"
    assert block_text_to_markdown(ordered, "一\n二") == "1. 一\n2. 二\n"
    assert block_text_to_markdown(equation, r"x \otimes y") == (
        "$$\nx \\otimes y\n$$\n"
    )
    assert block_text_to_markdown(code, "value = ```") == (
        "````\nvalue = ```\n````\n"
    )


def test_block_text_to_markdown_preserves_existing_list_and_math_markup() -> None:
    listing = _block(
        RichBlockKind.LIST,
        {
            "ordered": False,
            "items": [
                {
                    "text": "one",
                    "inline_spans": [
                        {
                            "kind": "text",
                            "start": 0,
                            "end": 3,
                            "text": "one",
                        }
                    ],
                }
            ],
        },
    )
    equation = _block(
        RichBlockKind.EQUATION,
        {"tex": "x=1", "display": True, "label": ""},
    )
    assert block_text_to_markdown(listing, "* 已有") == "* 已有\n"
    assert block_text_to_markdown(equation, "$$\nx=1\n$$") == "$$\nx=1\n$$\n"


def test_fragment_metadata_rejects_float_spelling() -> None:
    anchor = FragmentAnchor(
        "block",
        "block-1",
        (
            AnchorBlock(
                "block-1",
                "paragraph",
                0,
                {"source_format": "markdown", "line_start": 1},
                hashlib.sha256(b"block").hexdigest(),
            ),
        ),
    )
    with pytest.raises(ValueError, match="non-integer number"):
        FragmentRevision(
            source=SourceIdentity(
                "markdown",
                "text/markdown",
                hashlib.sha256(b"source").hexdigest(),
                6,
                hashlib.sha256(b"rich").hexdigest(),
            ),
            fragment_id="fragment-1",
            revision=1,
            parent_semantic_digest=None,
            anchor=anchor,
            priority=10,
            role="translation",
            language="zh-CN",
            title=None,
            citation_ids=(),
            provenance={"confidence": 1.0},
            markdown_body="译文\n",
        )
