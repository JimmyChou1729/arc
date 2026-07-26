"""Deterministic source chapter planning and exact block coverage."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arc_jobs import canonical_json_bytes
from arc_paper import RichBlock, RichBlockKind, RichDocument, rich_block_to_document

from .source_identity import resolve_document_identity


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


def block_prompt_document(
    block: RichBlock,
    *,
    equation_label_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Source projection used in prompts, with validated PDF label provenance."""

    document = rich_block_to_document(block)
    if block.kind is not RichBlockKind.EQUATION or not equation_label_provenance:
        return document
    effective = equation_label_provenance.get("effective_label")
    source = equation_label_provenance.get("source_label")
    if not isinstance(effective, str) or not isinstance(source, str):
        return document
    document["payload"]["label"] = effective
    document["equation_label_provenance"] = dict(equation_label_provenance)
    return document


def equation_label_provenance(
    document: RichDocument, block_id: str
) -> Mapping[str, Any] | None:
    values = document.metadata.get("equation_label_reconciliation")
    if not isinstance(values, Mapping):
        return None
    value = values.get(block_id)
    return value if isinstance(value, Mapping) else None


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
    return resolve_document_identity(document).title or "Document"


__all__ = [
    "SourceChapter",
    "block_prompt_document",
    "equation_label_provenance",
    "plan_source_chapters",
    "validate_chapter_coverage",
]
