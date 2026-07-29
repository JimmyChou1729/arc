"""Deterministic source chapter planning and exact block coverage."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arc_jobs import canonical_json_bytes
from arc_paper import (
    DocumentStructureNodeKind,
    DocumentStructureOverlay,
    RichBlock,
    RichBlockKind,
    RichDocument,
    rich_block_to_document,
)

from .source_identity import resolve_document_identity


@dataclass(frozen=True)
class SourceChapter:
    chapter_id: str
    title: str
    block_ids: tuple[str, ...]
    display_anchor_block_id: str
    section_block_ids: tuple[str, ...] = ()
    section_titles: tuple[str, ...] = ()
    section_levels: tuple[int, ...] = ()
    structure_section_id: str | None = None
    generate_guide: bool = True

    def __post_init__(self) -> None:
        if not self.chapter_id or not self.title or not self.block_ids:
            raise ValueError("source chapter requires identity, title, and blocks")
        if len(set(self.block_ids)) != len(self.block_ids):
            raise ValueError("source chapter contains duplicate blocks")
        if self.display_anchor_block_id not in self.block_ids:
            raise ValueError("chapter display anchor is outside its source chapter")
        if not (
            len(self.section_block_ids)
            == len(self.section_titles)
            == len(self.section_levels)
        ):
            raise ValueError("section identities, titles, and levels differ")
        if any(item not in self.block_ids for item in self.section_block_ids):
            raise ValueError("section heading is outside its source chapter")
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 2
            for item in self.section_levels
        ):
            raise ValueError("section outline levels must be at least two")


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


def plan_structured_source_chapters(
    document: RichDocument,
    overlay: DocumentStructureOverlay,
    *,
    companion_section_ids: Sequence[str] | None = None,
) -> tuple[SourceChapter, ...]:
    """Partition a source using an arc-paper structure overlay.

    Structure chooses display and generation boundaries without changing any
    rich block identity. Unselected gaps remain deterministic display chapters
    and never create model loops.
    """

    entries = tuple(overlay.entries)
    by_id = {item.section_id: item for item in entries}
    if companion_section_ids is None:
        selected = [
            item
            for item in entries
            if item.kind is DocumentStructureNodeKind.CONTENT
            and not any(
                ancestor.kind is DocumentStructureNodeKind.CONTENT
                for ancestor in _ancestors(item, by_id)
            )
        ]
    else:
        requested = tuple(companion_section_ids)
        if len(requested) != len(set(requested)):
            raise ValueError("companion section IDs must be unique")
        missing = [item for item in requested if item not in by_id]
        if missing:
            raise ValueError(
                "companion section IDs are absent from the structure overlay"
            )
        selected = [by_id[item] for item in requested]
    selected.sort(key=lambda item: item.source_line_start)
    for left, right in zip(selected, selected[1:]):
        if left.source_line_end >= right.source_line_start:
            raise ValueError("selected Companion structure sections overlap")

    block_ranges: list[tuple[int, int, Any]] = []
    for item in selected:
        indices = [
            index
            for index, block in enumerate(document.blocks)
            if _block_in_lines(
                block,
                item.source_line_start,
                item.source_line_end,
            )
        ]
        if not indices:
            raise ValueError("structure section contains no rich source blocks")
        block_ranges.append((min(indices), max(indices) + 1, item))

    chapters: list[SourceChapter] = []
    cursor = 0
    for start, end, item in block_ranges:
        if start < cursor:
            raise ValueError("structured Companion chapters overlap")
        if start > cursor:
            chapters.append(
                _display_chapter(document, document.blocks[cursor:start])
            )
        chapter_blocks = document.blocks[start:end]
        descendant_headings = [
            candidate
            for candidate in entries
            if candidate.section_id != item.section_id
            and _is_descendant(candidate, item.section_id, by_id)
            and candidate.kind
            in {
                DocumentStructureNodeKind.INTERNAL,
                DocumentStructureNodeKind.CONTENT,
            }
        ]
        section_blocks: list[RichBlock] = []
        section_titles: list[str] = []
        section_levels: list[int] = []
        for section in sorted(
            descendant_headings, key=lambda value: value.source_line_start
        ):
            heading = next(
                (
                    block
                    for block in chapter_blocks
                    if block.kind is RichBlockKind.HEADING
                    and block.locator.line_start == section.heading_line
                ),
                None,
            )
            if heading is not None:
                section_blocks.append(heading)
                section_titles.append(section.title)
                section_levels.append(max(2, section.level - item.level + 1))
        display_anchor = next(
            (
                block
                for block in chapter_blocks
                if block.kind is RichBlockKind.HEADING
                and block.locator.line_start == item.heading_line
            ),
            None,
        )
        chapters.append(
            _chapter(
                document,
                title=item.title,
                blocks=chapter_blocks,
                display_anchor_block=display_anchor,
                section_blocks=tuple(section_blocks),
                section_titles=tuple(section_titles),
                section_levels=tuple(section_levels),
                structure_section_id=item.section_id,
                generate_guide=True,
            )
        )
        cursor = end
    if cursor < len(document.blocks):
        chapters.append(_display_chapter(document, document.blocks[cursor:]))
    validate_chapter_coverage(document, tuple(chapters))
    return tuple(chapters)


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
    document: RichDocument,
    *,
    title: str,
    blocks: tuple[RichBlock, ...],
    display_anchor_block: RichBlock | None = None,
    section_blocks: tuple[RichBlock, ...] = (),
    section_titles: tuple[str, ...] = (),
    section_levels: tuple[int, ...] = (),
    structure_section_id: str | None = None,
    generate_guide: bool = True,
) -> SourceChapter:
    if display_anchor_block is None:
        display_anchor_block = next(
            (
                item
                for item in blocks
                if item.kind is RichBlockKind.HEADING
                and str(item.payload["text"]).strip() == title.strip()
            ),
            None,
        )
    if display_anchor_block is None:
        display_anchor_block = next(
            (item for item in blocks if item.kind is RichBlockKind.HEADING),
            blocks[0],
        )
    if not section_blocks:
        headings = tuple(
            item for item in blocks if item.kind is RichBlockKind.HEADING
        )
        if display_anchor_block in headings:
            headings = tuple(
                item for item in headings if item != display_anchor_block
            )
        section_blocks = headings
        section_titles = tuple(
            str(item.payload["text"]).strip() for item in headings
        )
        anchor_level = (
            int(display_anchor_block.payload["level"])
            if display_anchor_block.kind is RichBlockKind.HEADING
            else 0
        )
        section_levels = tuple(
            max(2, int(item.payload["level"]) - anchor_level + 1)
            for item in headings
        )
    material = {
        "document_digest": document.document_digest,
        "block_ids": [block.block_id for block in blocks],
    }
    digest = hashlib.sha256(canonical_json_bytes(material)).hexdigest()[:24]
    return SourceChapter(
        chapter_id=f"chapter-{digest}",
        title=title or "Document",
        block_ids=tuple(block.block_id for block in blocks),
        display_anchor_block_id=display_anchor_block.block_id,
        section_block_ids=tuple(block.block_id for block in section_blocks),
        section_titles=section_titles,
        section_levels=section_levels,
        structure_section_id=structure_section_id,
        generate_guide=generate_guide,
    )


def _display_chapter(
    document: RichDocument, blocks: tuple[RichBlock, ...]
) -> SourceChapter:
    heading = next(
        (item for item in blocks if item.kind is RichBlockKind.HEADING),
        None,
    )
    title = (
        str(heading.payload["text"]).strip()
        if heading is not None
        else _document_title(document)
    )
    return _chapter(
        document,
        title=title,
        blocks=blocks,
        generate_guide=False,
    )


def _block_in_lines(block: RichBlock, start: int, end: int) -> bool:
    line = block.locator.line_start
    return isinstance(line, int) and start <= line <= end


def _ancestors(item: Any, by_id: Mapping[str, Any]) -> tuple[Any, ...]:
    output = []
    parent_id = item.parent_id
    while parent_id is not None:
        parent = by_id[parent_id]
        output.append(parent)
        parent_id = parent.parent_id
    return tuple(output)


def _is_descendant(
    item: Any, ancestor_id: str, by_id: Mapping[str, Any]
) -> bool:
    return any(
        parent.section_id == ancestor_id
        for parent in _ancestors(item, by_id)
    )


def _document_title(document: RichDocument) -> str:
    return resolve_document_identity(document).title or "Document"


__all__ = [
    "SourceChapter",
    "block_prompt_document",
    "equation_label_provenance",
    "plan_source_chapters",
    "plan_structured_source_chapters",
    "validate_chapter_coverage",
]
