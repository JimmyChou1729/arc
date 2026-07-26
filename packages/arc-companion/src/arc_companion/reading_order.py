"""Deterministic reader-visible ordering shared by build and render paths."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence

from .contracts import AcceptedChapter, GlossaryEntry, LearningUnit


def iter_visible_learning_units(
    chapters: Sequence[AcceptedChapter],
) -> Iterator[LearningUnit]:
    """Yield units in the same order a reader encounters them."""

    for chapter in chapters:
        for unit in chapter.learning_units:
            if unit.placement == "chapter":
                yield unit
        inline = tuple(
            unit
            for unit in chapter.learning_units
            if unit.placement == "inline"
        )
        for anchor in chapter.source_anchors:
            for unit in inline:
                if unit.anchor_ids[0] == anchor.block_id:
                    yield unit


def first_visible_citation_ids(
    chapters: Sequence[AcceptedChapter],
    glossary: Sequence[GlossaryEntry] = (),
) -> tuple[str, ...]:
    """Stable-dedupe citations by their first visible marker."""

    return _stable_unique(
        citation
        for unit in iter_visible_learning_units(chapters)
        for citation in unit.citations
    ) + _stable_unique(
        citation
        for entry in glossary
        for citation in entry.citations
    )


def _stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


__all__ = [
    "first_visible_citation_ids",
    "iter_visible_learning_units",
]
