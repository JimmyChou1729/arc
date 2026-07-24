"""Closed prompt contracts for the source-anchored Companion workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


LANGUAGE_PROMPT_VERSION = "arc.companion.language-prompt.v1"
CHAPTER_PLAN_PROMPT_VERSION = "arc.companion.chapter-plan-prompt.v1"
GLOSSARY_PROMPT_VERSION = "arc.companion.glossary-prompt.v2"
TRANSLATION_PROMPT_VERSION = "arc.companion.translation-prompt.v1"
CHAPTER_DRAFT_PROMPT_VERSION = "arc.companion.chapter-draft-prompt.v3"
CHAPTER_REVIEW_PROMPT_VERSION = "arc.companion.chapter-review-prompt.v1"


def _closed(properties: Mapping[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_NONEMPTY = {"type": "string", "minLength": 1}
_BLOCK_IDS = {
    "type": "array",
    "items": _NONEMPTY,
    "minItems": 1,
    "uniqueItems": True,
}

LANGUAGE_SCHEMA = _closed(
    {
        "language_tag": _NONEMPTY,
        "classification": {"enum": ["known", "mixed", "unknown"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    ("language_tag", "classification", "confidence"),
)

_PLANNED_UNIT = _closed(
    {
        "unit_id": _NONEMPTY,
        "kind": {
            "enum": [
                "prerequisite",
                "intuition",
                "derivation",
                "example",
                "misconception",
                "further_reading",
            ]
        },
        "title": _NONEMPTY,
        "anchor_block_ids": _BLOCK_IDS,
        "purpose": _NONEMPTY,
    },
    ("unit_id", "kind", "title", "anchor_block_ids", "purpose"),
)

_GLOSSARY_CANDIDATE = _closed(
    {
        "term": _NONEMPTY,
        "definition": _NONEMPTY,
        "anchor_block_ids": _BLOCK_IDS,
    },
    ("term", "definition", "anchor_block_ids"),
)

_EVIDENCE_REQUEST = _closed(
    {
        "request_id": _NONEMPTY,
        "kind": {"enum": ["paper", "web", "user"]},
        "query": _NONEMPTY,
        "purpose": _NONEMPTY,
        "anchor_block_ids": _BLOCK_IDS,
    },
    ("request_id", "kind", "query", "purpose", "anchor_block_ids"),
)

CHAPTER_PLAN_SCHEMA = _closed(
    {
        "chapter_id": _NONEMPTY,
        "guide": _NONEMPTY,
        "learning_units": {
            "type": "array",
            "items": _PLANNED_UNIT,
        },
        "glossary_candidates": {
            "type": "array",
            "items": _GLOSSARY_CANDIDATE,
        },
        "evidence_requests": {
            "type": "array",
            "items": _EVIDENCE_REQUEST,
        },
    },
    (
        "chapter_id",
        "guide",
        "learning_units",
        "glossary_candidates",
        "evidence_requests",
    ),
)

_GLOSSARY_ENTRY = _closed(
    {
        "term": _NONEMPTY,
        "definition": _NONEMPTY,
        "preferred_translation": {"type": ["string", "null"]},
        "anchor_block_ids": _BLOCK_IDS,
        "citations": {
            "type": "array",
            "items": _NONEMPTY,
            "uniqueItems": True,
        },
    },
    (
        "term",
        "definition",
        "preferred_translation",
        "anchor_block_ids",
        "citations",
    ),
)

GLOSSARY_SCHEMA = _closed(
    {
        "entries": {
            "type": "array",
            "items": _GLOSSARY_ENTRY,
        }
    },
    ("entries",),
)

_SOURCE_IDENTITY = _closed(
    {
        "equations": {
            "type": "array",
            "items": {"type": "string"},
        },
        "code_text": {"type": ["string", "null"]},
        "link_targets": {
            "type": "array",
            "items": {"type": "string"},
        },
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

_WINDOW_TRANSLATED_BLOCK = _closed(
    {
        "block_id": _NONEMPTY,
        "text": {"type": "string"},
        "source_identity": _SOURCE_IDENTITY,
    },
    ("block_id", "text", "source_identity"),
)

TRANSLATION_SCHEMA = _closed(
    {
        "translations": {
            "type": "array",
            "items": _WINDOW_TRANSLATED_BLOCK,
        },
    },
    ("translations",),
)

_LEARNING_UNIT = _closed(
    {
        "unit_id": _NONEMPTY,
        "kind": _NONEMPTY,
        "title": _NONEMPTY,
        "anchor_block_ids": _BLOCK_IDS,
        "content": _NONEMPTY,
        "citations": {
            "type": "array",
            "items": _NONEMPTY,
            "uniqueItems": True,
        },
    },
    ("unit_id", "kind", "title", "anchor_block_ids", "content", "citations"),
)

CHAPTER_DRAFT_SCHEMA = _closed(
    {
        "chapter_id": _NONEMPTY,
        "guide": _NONEMPTY,
        "learning_units": {
            "type": "array",
            "items": _LEARNING_UNIT,
        },
    },
    ("chapter_id", "guide", "learning_units"),
)

_TEXT_PATCH = _closed(
    {
        "id": _NONEMPTY,
        "replacement": {"type": "string"},
    },
    ("id", "replacement"),
)

CHAPTER_REVIEW_SCHEMA = _closed(
    {
        "guide_replacement": {"type": ["string", "null"]},
        "translation_patches": {
            "type": "array",
            "items": _TEXT_PATCH,
        },
        "learning_unit_patches": {
            "type": "array",
            "items": _TEXT_PATCH,
        },
        "summary": _NONEMPTY,
    },
    (
        "guide_replacement",
        "translation_patches",
        "learning_unit_patches",
        "summary",
    ),
)

def language_prompt(samples: Sequence[str]) -> str:
    return _prompt(
        LANGUAGE_PROMPT_VERSION,
        """
Detect the primary natural language of the supplied source samples exactly
once for a book build. Return a BCP-47-compatible primary language tag.
Use classification "mixed" when multiple substantive languages are present
and "unknown" when the evidence is insufficient. Do not infer the target
language and do not translate.
""",
        {"samples": list(samples)},
    )


def chapter_plan_prompt(
    *,
    chapter_id: str,
    title: str,
    blocks: Sequence[Mapping[str, Any]],
    target_language: str,
    intent: str,
) -> str:
    return _prompt(
        CHAPTER_PLAN_PROMPT_VERSION,
        """
Plan a selective textbook companion for this source chapter. The source is
authoritative. Write the guide in the target language, select only genuinely
useful learning units, and anchor every proposed unit, glossary candidate, and
evidence request to existing block IDs. Do not mechanically expand every
paragraph. Never invent or modify source block IDs.
""",
        {
            "chapter_id": chapter_id,
            "title": title,
            "target_language": target_language,
            "intent": intent,
            "blocks": list(blocks),
        },
    )


def glossary_prompt(
    *,
    candidates: Sequence[Mapping[str, Any]],
    target_language: str,
    evidence: Sequence[Mapping[str, Any]],
) -> str:
    return _prompt(
        GLOSSARY_PROMPT_VERSION,
        """
Create one concise book-wide glossary. Merge duplicate terms
case-insensitively, preserve all valid source anchors, and choose one preferred
translation per term when translation is useful. Definitions and preferred
translations must be consistent throughout the book.
Use citations only when frozen evidence supports a definition, and cite it by
evidence_id. An empty citations list is valid.
""",
        {
            "target_language": target_language,
            "candidates": list(candidates),
            "frozen_evidence": list(evidence),
        },
    )


def translation_prompt(
    *,
    chapter_id: str,
    window_ordinal: int,
    blocks: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    language_result: Mapping[str, Any],
) -> str:
    return _prompt(
        TRANSLATION_PROMPT_VERSION,
        """
Translate every supplied source block into the target language. Return each
supplied block ID exactly once and in source order. Preserve formulas, links,
citations, code, tables, and asset references faithfully. Use the supplied
book-wide glossary consistently. This request is one deterministic block-boundary
window; do not add a chapter guide or learning material. For every result,
copy the source block's equations, code text, link targets, asset digest, and
asset target into source_identity exactly as supplied; use empty arrays/null
for identity fields that do not apply.
""",
        {
            "chapter_id": chapter_id,
            "window_ordinal": window_ordinal,
            "target_language": target_language,
            "language_result": dict(language_result),
            "blocks": list(blocks),
            "glossary": list(glossary),
        },
    )


def chapter_draft_prompt(
    *,
    plan: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    language_result: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> str:
    return _prompt(
        CHAPTER_DRAFT_PROMPT_VERSION,
        """
Produce the chapter guide and only the planned learning units. A complete
translation layer has already been frozen separately and will be joined
locally; do not translate, rewrite, or return translations. Learning-unit IDs
and anchors must exactly match the plan. Use the book glossary consistently.
Cite supplied frozen evidence by evidence_id. All planned evidence has already
been resolved by the workflow; do not invent evidence identifiers.
""",
        {
            "target_language": target_language,
            "language_result": dict(language_result),
            "plan": dict(plan),
            "blocks": list(blocks),
            "glossary": list(glossary),
            "frozen_evidence": list(evidence),
        },
    )


def chapter_review_prompt(
    *,
    plan: Mapping[str, Any],
    draft: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
) -> str:
    return _prompt(
        CHAPTER_REVIEW_PROMPT_VERSION,
        """
Review this source-anchored chapter once. Return only text replacement patches
for the guide, translations, or learning-unit content. Patch IDs must already
exist in the draft. You cannot change source text, block IDs, source anchors,
learning-unit kinds or titles, citations, or glossary entries. Use null/empty
patch lists when no change is needed.
""",
        {
            "plan": dict(plan),
            "draft": dict(draft),
            "source_blocks": list(blocks),
            "glossary": list(glossary),
        },
    )


def _prompt(version: str, instruction: str, payload: Mapping[str, Any]) -> str:
    return (
        f"Contract: {version}\n\n"
        + " ".join(line.strip() for line in instruction.strip().splitlines())
        + "\n\nInput JSON:\n"
        + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


__all__ = [
    "CHAPTER_DRAFT_PROMPT_VERSION",
    "CHAPTER_DRAFT_SCHEMA",
    "CHAPTER_PLAN_PROMPT_VERSION",
    "CHAPTER_PLAN_SCHEMA",
    "CHAPTER_REVIEW_PROMPT_VERSION",
    "CHAPTER_REVIEW_SCHEMA",
    "GLOSSARY_PROMPT_VERSION",
    "GLOSSARY_SCHEMA",
    "LANGUAGE_PROMPT_VERSION",
    "LANGUAGE_SCHEMA",
    "TRANSLATION_PROMPT_VERSION",
    "TRANSLATION_SCHEMA",
    "chapter_draft_prompt",
    "chapter_plan_prompt",
    "chapter_review_prompt",
    "glossary_prompt",
    "language_prompt",
    "translation_prompt",
]
