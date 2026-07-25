"""Guide-only prompt contracts for Companion's split v2 workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


CHAPTER_PLAN_PROMPT_VERSION = "arc.companion.chapter-plan-prompt.v2"
CHAPTER_GUIDE_PROMPT_VERSION = "arc.companion.chapter-guide-prompt.v1"
CHAPTER_GUIDE_REVIEW_PROMPT_VERSION = (
    "arc.companion.chapter-guide-review-prompt.v1"
)


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
_BLOCK_IDS = {
    "type": "array",
    "items": _NONEMPTY,
    "minItems": 1,
    "uniqueItems": True,
}
_PLANNED_UNIT = _closed(
    {
        "unit_id": _NONEMPTY,
        "kind": {
            "type": "string",
            "enum": [
                "prerequisite",
                "intuition",
                "derivation",
                "example",
                "misconception",
                "further_reading",
            ],
        },
        "title": _NONEMPTY,
        "anchor_block_ids": _BLOCK_IDS,
        "purpose": _NONEMPTY,
    },
    ("unit_id", "kind", "title", "anchor_block_ids", "purpose"),
)
_EVIDENCE_REQUEST = _closed(
    {
        "request_id": _NONEMPTY,
        "kind": {
            "type": "string",
            "enum": ["paper", "web", "user"],
        },
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
        "learning_units": {"type": "array", "items": _PLANNED_UNIT},
        "evidence_requests": {
            "type": "array",
            "items": _EVIDENCE_REQUEST,
        },
    },
    ("chapter_id", "guide", "learning_units", "evidence_requests"),
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
CHAPTER_GUIDE_SCHEMA = _closed(
    {
        "chapter_id": _NONEMPTY,
        "guide": _NONEMPTY,
        "learning_units": {"type": "array", "items": _LEARNING_UNIT},
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
CHAPTER_GUIDE_REVIEW_SCHEMA = _closed(
    {
        "guide_replacement": {"type": ["string", "null"]},
        "learning_unit_patches": {
            "type": "array",
            "items": _TEXT_PATCH,
        },
        "summary": _NONEMPTY,
    },
    ("guide_replacement", "learning_unit_patches", "summary"),
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
        Plan a selective textbook companion for this source chapter. The source
        is authoritative. Write the guide in the target language, select only
        genuinely useful learning units, and anchor every proposed unit and
        evidence request to existing block IDs. Terminology and translation are
        owned by a separate workflow: do not propose glossary candidates and do
        not translate. Never invent or modify source block IDs.
        """,
        {
            "chapter_id": chapter_id,
            "title": title,
            "target_language": target_language,
            "intent": intent,
            "blocks": list(blocks),
        },
    )


def chapter_guide_prompt(
    *,
    plan: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    language_result: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> str:
    return _prompt(
        CHAPTER_GUIDE_PROMPT_VERSION,
        """
        Produce the chapter guide and only the planned learning units. A
        translation lane, when needed, runs independently and will be joined
        locally; do not translate or return translations. Learning-unit IDs and
        anchors must exactly match the plan. The supplied glossary contains only
        source terms that occur literally in this chapter. Use those entries
        consistently. Cite supplied frozen evidence by evidence_id and do not
        invent evidence identifiers.
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


def chapter_guide_review_prompt(
    *,
    plan: Mapping[str, Any],
    draft: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
) -> str:
    return _prompt(
        CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
        """
        Review only this source-anchored chapter guide and its learning units.
        Return text replacements for the guide or learning-unit content. Patch
        IDs must already exist in the draft. You cannot change source text,
        block IDs, anchors, learning-unit kinds or titles, citations, glossary
        entries, or any translation output. Use null or an empty patch list when
        no change is needed.
        """,
        {
            "plan": dict(plan),
            "draft": dict(draft),
            "source_blocks": list(blocks),
            "glossary": list(glossary),
        },
    )


def _prompt(
    version: str, instruction: str, payload: Mapping[str, Any]
) -> str:
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
    "CHAPTER_GUIDE_PROMPT_VERSION",
    "CHAPTER_GUIDE_REVIEW_PROMPT_VERSION",
    "CHAPTER_GUIDE_REVIEW_SCHEMA",
    "CHAPTER_GUIDE_SCHEMA",
    "CHAPTER_PLAN_PROMPT_VERSION",
    "CHAPTER_PLAN_SCHEMA",
    "chapter_guide_prompt",
    "chapter_guide_review_prompt",
    "chapter_plan_prompt",
]
