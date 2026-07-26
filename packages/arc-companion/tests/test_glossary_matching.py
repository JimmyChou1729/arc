from __future__ import annotations

from arc_companion.contracts import GlossaryEntry
from arc_companion.glossary_matching import (
    GlossaryMatcher,
    glossary_tooltip,
)


def _entry(
    entry_id: str,
    term: str,
    translated: str,
    definition: str,
) -> GlossaryEntry:
    return GlossaryEntry(
        entry_id,
        term,
        translated,
        definition,
        ("block",),
    )


def test_latin_matching_is_longest_case_insensitive_and_word_bounded() -> None:
    matcher = GlossaryMatcher(
        (
            _entry("state", "state", "态", "A state."),
            _entry(
                "state-space",
                "state space",
                "状态空间",
                "A state space.",
            ),
            _entry("entropy", "entropy", "熵", "Entropy."),
        ),
        translated=False,
    )

    matches = matcher.find(
        "STATE SPACE and Entropy, but entropyProduction is distinct."
    )

    assert [
        (match.start, match.end, match.entries[0].entry_id)
        for match in matches
    ] == [
        (0, 11, "state-space"),
        (16, 23, "entropy"),
    ]


def test_latin_matching_uses_unicode_casefold_with_original_offsets() -> None:
    matcher = GlossaryMatcher(
        (_entry("street", "Straße", "街道", "A street."),),
        translated=False,
    )

    matches = matcher.find("STRASSE und Straße, nicht Grossstrasse.")

    assert [(match.start, match.end) for match in matches] == [
        (0, 7),
        (12, 18),
    ]


def test_cjk_surface_matches_inside_compounds() -> None:
    matcher = GlossaryMatcher(
        (_entry("entropy", "entropy", "熵", "熵的定义。"),),
        translated=True,
    )

    matches = matcher.find("信息熵增大")

    assert len(matches) == 1
    assert matches[0].start == 2
    assert matches[0].end == 3


def test_ambiguous_surface_keeps_all_entries_in_stable_order() -> None:
    entries = (
        _entry("first", "first term", "同名", "第一义。"),
        _entry("second", "second term", "同名", "第二义。"),
    )
    matcher = GlossaryMatcher(entries, translated=True)

    match = matcher.find("同名")[0]
    tooltip = glossary_tooltip(
        match.entries,
        labels={
            "source_term": "原文术语",
            "translation": "译名",
            "definition": "释义",
        },
    )

    assert [item.entry_id for item in match.entries] == [
        "first",
        "second",
    ]
    assert tooltip.splitlines() == [
        "原文术语: first term; 译名: 同名; 释义: 第一义。",
        "原文术语: second term; 译名: 同名; 释义: 第二义。",
    ]
