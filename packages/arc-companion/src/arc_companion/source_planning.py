"""Deterministic source chapter planning and exact block coverage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from arc_jobs import canonical_json_bytes
from arc_paper import RichBlock, RichBlockKind, RichDocument, rich_block_to_document


@dataclass(frozen=True)
class SourceChapter:
    chapter_id: str
    title: str
    block_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.chapter_id or not self.title or not self.block_ids:
            raise ValueError("source chapter requires identity, title, and blocks")
        if len(set(self.block_ids)) != len(self.block_ids):
            raise ValueError("source chapter contains duplicate blocks")


def plan_source_chapters(document: RichDocument) -> tuple[SourceChapter, ...]:
    """Use the shallowest source headings, preserving exact block coverage."""

    if not document.blocks:
        raise ValueError("Companion source must contain at least one block")
    headings = [
        block
        for block in document.blocks
        if block.kind is RichBlockKind.HEADING
    ]
    if not headings:
        return (
            _chapter(
                document,
                title=_document_title(document),
                blocks=document.blocks,
            ),
        )
    levels = [int(block.payload["level"]) for block in headings]
    chapter_level = min(levels)
    starts = [
        block.ordinal
        for block in headings
        if int(block.payload["level"]) == chapter_level
    ]
    # Front matter belongs to the first chapter so every source block is
    # covered exactly once without inventing a synthetic LLM segment.
    starts[0] = 0
    output: list[SourceChapter] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(document.blocks)
        blocks = document.blocks[start:end]
        heading = next(
            (
                block
                for block in blocks
                if block.kind is RichBlockKind.HEADING
                and int(block.payload["level"]) == chapter_level
            ),
            None,
        )
        title = (
            str(heading.payload["text"]).strip()
            if heading is not None
            else _document_title(document)
        )
        output.append(_chapter(document, title=title, blocks=blocks))
    validate_chapter_coverage(document, tuple(output))
    return tuple(output)


def validate_chapter_coverage(
    document: RichDocument, chapters: tuple[SourceChapter, ...]
) -> None:
    expected = tuple(block.block_id for block in document.blocks)
    actual = tuple(block_id for chapter in chapters for block_id in chapter.block_ids)
    if actual != expected:
        raise ValueError(
            "source chapters must cover every block exactly once in source order"
        )


def block_prompt_document(block: RichBlock) -> dict[str, Any]:
    """Public-codec projection used in all source-anchored prompts."""

    return rich_block_to_document(block)


def _chapter(
    document: RichDocument, *, title: str, blocks: tuple[RichBlock, ...]
) -> SourceChapter:
    material = {
        "document_digest": document.document_digest,
        "block_ids": [block.block_id for block in blocks],
    }
    digest = hashlib.sha256(canonical_json_bytes(material)).hexdigest()[:24]
    return SourceChapter(
        chapter_id=f"chapter-{digest}",
        title=title or "Document",
        block_ids=tuple(block.block_id for block in blocks),
    )


def _document_title(document: RichDocument) -> str:
    title = document.metadata.get("title")
    return str(title).strip() if isinstance(title, str) and title.strip() else "Document"


__all__ = [
    "SourceChapter",
    "block_prompt_document",
    "plan_source_chapters",
    "validate_chapter_coverage",
]
