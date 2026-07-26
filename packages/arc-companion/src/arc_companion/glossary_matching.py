"""Deterministic, layer-aware glossary surface matching."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import unicodedata

from .contracts import GlossaryEntry


@dataclass(frozen=True)
class GlossaryMatch:
    start: int
    end: int
    entries: tuple[GlossaryEntry, ...]


@dataclass(frozen=True)
class _Surface:
    text: str
    entries: tuple[GlossaryEntry, ...]
    order: int
    word_boundaries: bool


class GlossaryMatcher:
    """Find stable, longest, non-overlapping glossary surfaces."""

    def __init__(
        self,
        entries: Sequence[GlossaryEntry],
        *,
        translated: bool,
    ) -> None:
        grouped: dict[str, list[GlossaryEntry]] = {}
        display: dict[str, str] = {}
        order: dict[str, int] = {}
        for index, entry in enumerate(entries):
            surface = (
                entry.translated_term if translated else entry.term
            ).strip()
            if not surface:
                continue
            key = surface.casefold()
            grouped.setdefault(key, []).append(entry)
            display.setdefault(key, surface)
            order.setdefault(key, index)
        self._surfaces = tuple(
            sorted(
                (
                    _Surface(
                        display[key],
                        tuple(values),
                        order[key],
                        _requires_word_boundaries(display[key]),
                    )
                    for key, values in grouped.items()
                ),
                key=lambda item: (-len(item.text), item.order),
            )
        )

    def find(self, text: str) -> tuple[GlossaryMatch, ...]:
        candidates: list[tuple[int, int, _Surface]] = []
        folded_text, offsets = _casefold_with_offsets(text)
        for surface in self._surfaces:
            folded_surface = surface.text.casefold()
            cursor = 0
            while folded_surface:
                folded_start = folded_text.find(folded_surface, cursor)
                if folded_start < 0:
                    break
                folded_end = folded_start + len(folded_surface)
                cursor = folded_start + 1
                if not _is_original_character_span(
                    offsets, folded_start, folded_end
                ):
                    continue
                start = offsets[folded_start]
                end = offsets[folded_end - 1] + 1
                if surface.word_boundaries and not _has_word_boundaries(
                    text, start, end
                ):
                    continue
                candidates.append((start, end, surface))
        candidates.sort(
            key=lambda item: (
                item[0],
                -(item[1] - item[0]),
                item[2].order,
            )
        )
        output: list[GlossaryMatch] = []
        cursor = 0
        index = 0
        while index < len(candidates):
            start = candidates[index][0]
            at_start: list[tuple[int, int, _Surface]] = []
            while index < len(candidates) and candidates[index][0] == start:
                at_start.append(candidates[index])
                index += 1
            if start < cursor:
                continue
            chosen = at_start[0]
            output.append(
                GlossaryMatch(
                    chosen[0],
                    chosen[1],
                    chosen[2].entries,
                )
            )
            cursor = chosen[1]
        return tuple(output)

    def segments(
        self, text: str
    ) -> tuple[tuple[str, tuple[GlossaryEntry, ...]], ...]:
        values: list[tuple[str, tuple[GlossaryEntry, ...]]] = []
        cursor = 0
        for match in self.find(text):
            if match.start > cursor:
                values.append((text[cursor : match.start], ()))
            values.append((text[match.start : match.end], match.entries))
            cursor = match.end
        if cursor < len(text):
            values.append((text[cursor:], ()))
        if not values:
            values.append((text, ()))
        return tuple(values)


def glossary_tooltip(
    entries: Sequence[GlossaryEntry],
    *,
    labels: Mapping[str, str],
    separator: str = "\n",
) -> str:
    rows = []
    for entry in entries:
        rows.append(
            f"{labels['source_term']}: {entry.term}; "
            f"{labels['translation']}: {entry.translated_term}; "
            f"{labels['definition']}: {entry.definition}"
        )
    return separator.join(rows)


def _requires_word_boundaries(value: str) -> bool:
    return any(_is_latin(char) for char in value) and not any(
        _is_cjk(char) for char in value
    )


def _is_latin(char: str) -> bool:
    return "LATIN" in unicodedata.name(char, "")


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _has_word_boundaries(text: str, start: int, end: int) -> bool:
    return (
        start == 0 or not _is_word_character(text[start - 1])
    ) and (
        end == len(text) or not _is_word_character(text[end])
    )


def _is_word_character(char: str) -> bool:
    return char == "_" or char.isalnum()


def _casefold_with_offsets(value: str) -> tuple[str, tuple[int, ...]]:
    folded: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(value):
        expansion = character.casefold()
        folded.append(expansion)
        offsets.extend(index for _ in expansion)
    return "".join(folded), tuple(offsets)


def _is_original_character_span(
    offsets: Sequence[int], start: int, end: int
) -> bool:
    if start >= end or end > len(offsets):
        return False
    starts_at_boundary = start == 0 or offsets[start - 1] != offsets[start]
    ends_at_boundary = end == len(offsets) or offsets[end - 1] != offsets[end]
    return starts_at_boundary and ends_at_boundary


__all__ = [
    "GlossaryMatch",
    "GlossaryMatcher",
    "glossary_tooltip",
]
