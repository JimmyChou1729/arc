"""Versioned, closed prompt contracts for translation tasks."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


LANGUAGE_PROMPT_VERSION = "arc.translate.language_prompt.v1"
GLOSSARY_PROMPT_VERSION = "arc.translate.glossary_prompt.v1"
TRANSLATION_PROMPT_VERSION = "arc.translate.blocks_prompt.v1"
REVIEW_PROMPT_VERSION = "arc.translate.review_prompt.v1"


def _closed(
    properties: Mapping[str, Any], required: Sequence[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_NONEMPTY = {"type": "string", "minLength": 1}
_SOURCE_IDENTITY = _closed(
    {
        "equations": {"type": "array", "items": {"type": "string"}},
        "code_text": {"type": ["string", "null"]},
        "link_targets": {"type": "array", "items": {"type": "string"}},
        "asset_digest": {"type": ["string", "null"]},
        "asset_target": {"type": ["string", "null"]},
    },
    (
        "equations",
        "code_text",
        "link_targets",
        "asset_digest",
        "asset_target",
    ),
)

LANGUAGE_SCHEMA = _closed(
    {
        "language_tag": _NONEMPTY,
        "classification": {
            "type": "string",
            "enum": ["known", "mixed", "unknown"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    ("language_tag", "classification", "confidence"),
)

GLOSSARY_SCHEMA = _closed(
    {
        "entries": {
            "type": "array",
            "items": _closed(
                {
                    "term_id": _NONEMPTY,
                    "term": _NONEMPTY,
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "occurrence_count": {
                        "type": "integer",
                        "minimum": 0,
                    },
                    "source_refs": {"type": "array", "items": {}},
                    "matched_sentences": {"type": "array", "items": {}},
                    "preferred_translation": _NONEMPTY,
                    "target_definition": _NONEMPTY,
                },
                (
                    "term_id",
                    "term",
                    "aliases",
                    "occurrence_count",
                    "source_refs",
                    "matched_sentences",
                    "preferred_translation",
                    "target_definition",
                ),
            ),
        }
    },
    ("entries",),
)

TRANSLATION_SCHEMA = _closed(
    {
        "translations": {
            "type": "array",
            "items": _closed(
                {
                    "block_id": _NONEMPTY,
                    "text": _NONEMPTY,
                    "source_identity": _SOURCE_IDENTITY,
                },
                ("block_id", "text", "source_identity"),
            ),
        }
    },
    ("translations",),
)

REVIEW_SCHEMA = _closed(
    {
        "translation_patches": {
            "type": "array",
            "items": _closed(
                {"block_id": _NONEMPTY, "replacement": _NONEMPTY},
                ("block_id", "replacement"),
            ),
        },
        "summary": _NONEMPTY,
    },
    ("translation_patches", "summary"),
)


def language_prompt(samples: Sequence[str]) -> str:
    return _prompt(
        LANGUAGE_PROMPT_VERSION,
        """
Detect the primary natural language of the supplied source samples. Return a
BCP-47-compatible primary language tag. Use classification "mixed" when
multiple substantive languages are present and "unknown" when the evidence is
insufficient. Do not infer the target language and do not translate.
""",
        {"samples": list(samples)},
    )


def glossary_prompt(
    *,
    terms: Sequence[Mapping[str, Any]],
    target_language: str,
    window_ordinal: int,
) -> str:
    return _prompt(
        GLOSSARY_PROMPT_VERSION,
        """
Create a target-language glossary entry for every supplied scientific term.
Return every term exactly once and in the supplied order. Copy term_id, term,
aliases, occurrence_count, source_refs, and matched_sentences exactly. Add only
preferred_translation and target_definition. matched_sentences are search
grounding, never definitions; do not label, quote, or treat them as a source
definition. Preserve distinctions between nearby terms and do not merge,
deduplicate, select, drop, pad, or reorder terms. This is one byte-bounded
window; a local caller concatenates windows without a reducer.
""",
        {
            "target_language": target_language,
            "window_ordinal": window_ordinal,
            "terms": list(terms),
        },
    )


def translation_prompt(
    *,
    blocks: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    language_result: Mapping[str, Any],
    window_ordinal: int,
) -> str:
    return _prompt(
        TRANSLATION_PROMPT_VERSION,
        """
Translate every supplied source block into the target language. Return each
block ID exactly once and in source order. Keep each block indivisible. Preserve
formula occurrences, code text, link targets, and asset identity exactly, and
use the supplied preferred glossary translations consistently. Copy each
source_identity object exactly as supplied. Return only the translation layer:
do not add explanations, guides, summaries, or learning material.
""",
        {
            "target_language": target_language,
            "language_result": dict(language_result),
            "window_ordinal": window_ordinal,
            "blocks": list(blocks),
            "glossary": list(glossary),
        },
    )


def review_prompt(
    *,
    blocks: Sequence[Mapping[str, Any]],
    translations: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    window_ordinal: int,
) -> str:
    return _prompt(
        REVIEW_PROMPT_VERSION,
        """
Review only the supplied translation layer for scientific accuracy,
terminology consistency, and fluency. Return text replacement patches only
when needed. Patch block IDs must already exist. Do not change coverage,
ordering, source identity, formulas, code, links, assets, or glossary entries.
Do not add commentary to translated text. An empty patch list is valid.
""",
        {
            "target_language": target_language,
            "window_ordinal": window_ordinal,
            "blocks": list(blocks),
            "translations": list(translations),
            "glossary": list(glossary),
        },
    )


def _prompt(
    version: str, instruction: str, payload: Mapping[str, Any]
) -> str:
    compact_instruction = " ".join(
        line.strip() for line in instruction.strip().splitlines()
    )
    return (
        f"Contract: {version}\n\n{compact_instruction}\n\nInput JSON:\n"
        + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


__all__ = [
    "GLOSSARY_PROMPT_VERSION",
    "GLOSSARY_SCHEMA",
    "LANGUAGE_PROMPT_VERSION",
    "LANGUAGE_SCHEMA",
    "REVIEW_PROMPT_VERSION",
    "REVIEW_SCHEMA",
    "TRANSLATION_PROMPT_VERSION",
    "TRANSLATION_SCHEMA",
    "glossary_prompt",
    "language_prompt",
    "review_prompt",
    "translation_prompt",
]
