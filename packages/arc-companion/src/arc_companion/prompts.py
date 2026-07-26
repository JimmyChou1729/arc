"""Evidence-first prompt contracts for the current Companion workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


LITERATURE_REQUEST_PROMPT_VERSION = (
    "arc.companion.literature-request-prompt.v1"
)
LITERATURE_SURVEY_PROMPT_VERSION = (
    "arc.companion.literature-survey-prompt.v1"
)
CHAPTER_PLAN_PROMPT_VERSION = "arc.companion.chapter-plan-prompt.v3"
CHAPTER_GUIDE_PROMPT_VERSION = "arc.companion.chapter-learning-prompt.v2"
CHAPTER_GUIDE_REVIEW_PROMPT_VERSION = (
    "arc.companion.chapter-learning-review-prompt.v2"
)
VALUE_DIMENSIONS = (
    "motivation_or_argument_role",
    "genuinely_different_presentation",
    "deeper_or_nonconventional_implication",
    "omitted_intermediate_reasoning",
    "substantive_connection",
    "reliable_history_or_fact",
    "materially_useful_later_development",
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
_STRING_IDS = {
    "type": "array",
    "items": _NONEMPTY,
    "uniqueItems": True,
}
_LITERATURE_REQUEST = _closed(
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
LITERATURE_REQUEST_PLAN_SCHEMA = _closed(
    {
        "requests": {
            "type": "array",
            "items": _LITERATURE_REQUEST,
            "minItems": 1,
        }
    },
    ("requests",),
)
_SURVEY_THEME = _closed(
    {
        "theme_id": _NONEMPTY,
        "title": _NONEMPTY,
        "synthesis": _NONEMPTY,
        "anchor_block_ids": _BLOCK_IDS,
        "evidence_ids": {
            **_STRING_IDS,
            "minItems": 1,
        },
    },
    (
        "theme_id",
        "title",
        "synthesis",
        "anchor_block_ids",
        "evidence_ids",
    ),
)
LITERATURE_SURVEY_SCHEMA = _closed(
    {
        "themes": {"type": "array", "items": _SURVEY_THEME},
        "limitations": {
            "type": "array",
            "items": _NONEMPTY,
            "uniqueItems": True,
        },
    },
    ("themes", "limitations"),
)
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
        "placement": {
            "type": "string",
            "enum": ["inline", "chapter"],
        },
        "reader_question": _NONEMPTY,
        "added_value": _NONEMPTY,
        "value_dimensions": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": list(VALUE_DIMENSIONS),
            },
            "uniqueItems": True,
            "minItems": 1,
        },
        "evidence_ids": _STRING_IDS,
    },
    (
        "unit_id",
        "kind",
        "title",
        "anchor_block_ids",
        "placement",
        "reader_question",
        "added_value",
        "value_dimensions",
        "evidence_ids",
    ),
)
CHAPTER_PLAN_SCHEMA = _closed(
    {
        "chapter_id": _NONEMPTY,
        "learning_units": {"type": "array", "items": _PLANNED_UNIT},
    },
    ("chapter_id", "learning_units"),
)
_LEARNING_UNIT = _closed(
    {
        **_PLANNED_UNIT["properties"],
        "content": _NONEMPTY,
    },
    (*_PLANNED_UNIT["required"], "content"),
)
CHAPTER_GUIDE_SCHEMA = _closed(
    {
        "chapter_id": _NONEMPTY,
        "learning_units": {"type": "array", "items": _LEARNING_UNIT},
    },
    ("chapter_id", "learning_units"),
)
_REVIEW_DECISION = _closed(
    {
        "unit_id": _NONEMPTY,
        "decision": {
            "type": "string",
            "enum": ["keep", "replace", "remove"],
        },
        "replacement": {"type": ["string", "null"]},
        "reason": _NONEMPTY,
    },
    ("unit_id", "decision", "replacement", "reason"),
)
CHAPTER_GUIDE_REVIEW_SCHEMA = _closed(
    {
        "decisions": {
            "type": "array",
            "items": _REVIEW_DECISION,
        },
    },
    ("decisions",),
)


def literature_request_prompt(
    *,
    blocks: Sequence[Mapping[str, Any]],
    intent: str,
) -> str:
    return _prompt(
        LITERATURE_REQUEST_PROMPT_VERSION,
        """
        Inspect the complete source before any chapter planning. Request only
        literature or caller evidence that can add concrete explanatory value
        beyond the source. The ensuing research log must inspect at least 20
        distinct candidates across three categories: sources explicitly named
        by the document, important prior history, and later work central to the
        main debates. Each request must state the reader need it serves and
        anchor that need to existing source block IDs. Candidate coverage is a
        research requirement, not an inclusion quota: select only directly
        relevant evidence, and never force a source into a learning unit or
        bibliography merely to meet the candidate count. Avoid requests whose
        only purpose is to summarize or restate the source. Never invent or
        modify source block IDs.
        """,
        {"intent": intent, "blocks": list(blocks)},
    )


def literature_survey_prompt(
    *,
    blocks: Sequence[Mapping[str, Any]],
    intent: str,
    selected_evidence: Sequence[Mapping[str, Any]],
) -> str:
    return _prompt(
        LITERATURE_SURVEY_PROMPT_VERSION,
        """
        Build a document-level, evidence-grounded literature survey for later
        chapter planning. Synthesize only claims supported by the selected
        evidence and source blocks. Every theme must cite supplied evidence IDs
        and existing block IDs. Record limitations explicitly; do not invent
        sources, identifiers, or unsupported consensus.
        """,
        {
            "intent": intent,
            "blocks": list(blocks),
            "selected_evidence": list(selected_evidence),
        },
    )


def chapter_plan_prompt(
    *,
    chapter_id: str,
    title: str,
    blocks: Sequence[Mapping[str, Any]],
    target_language: str,
    intent: str,
    literature_survey: Mapping[str, Any] | None = None,
    selected_evidence: Sequence[Mapping[str, Any]] = (),
) -> str:
    return _prompt(
        CHAPTER_PLAN_PROMPT_VERSION,
        """
        Plan selective textbook additions for this source chapter after reading
        the document-level literature survey. Do not write a chapter summary or
        guide. Propose a learning unit only when it answers a concrete reader
        question and adds value not already supplied by the source. The allowed
        value dimensions are motivation_or_argument_role,
        genuinely_different_presentation, deeper_or_nonconventional_implication,
        omitted_intermediate_reasoning, substantive_connection,
        reliable_history_or_fact, and materially_useful_later_development.
        added_value must name the concrete increment absent from the source.
        Paraphrase, same-meaning rewrite, repeated reasoning, and generic
        summary are not added value. State inline or chapter placement, exact
        source anchors, and selected evidence IDs. Treat inline and chapter
        placement as equally valid choices; there is no placement quota.
        Evidence IDs may be empty for a purely source-grounded clarification.
        Terminology and translation are owned by a separate workflow. Never
        invent source or evidence IDs.
        """,
        {
            "chapter_id": chapter_id,
            "title": title,
            "target_language": target_language,
            "intent": intent,
            "blocks": list(blocks),
            "literature_survey": dict(
                literature_survey
                or {"themes": [], "limitations": []}
            ),
            "selected_evidence": list(selected_evidence),
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
        Write only the planned learning units; do not write a chapter summary or
        guide. Unit IDs, kinds, titles, anchors, placement, reader questions,
        added-value statements, value dimensions, and evidence IDs must exactly
        match the plan. Do not turn a planned increment into paraphrase,
        same-meaning rewrite, repeated reasoning, or generic summary. Inline
        and chapter units are equally important and have no quota. A translation
        lane runs independently. Use the supplied chapter glossary consistently.
        Ground literature claims only in the selected evidence assigned to each
        unit.
        """,
        {
            "target_language": target_language,
            "language_result": dict(language_result),
            "plan": dict(plan),
            "blocks": list(blocks),
            "glossary": list(glossary),
            "selected_evidence": list(evidence),
        },
    )


def chapter_guide_review_prompt(
    *,
    plan: Mapping[str, Any],
    draft: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
) -> str:
    return _prompt(
        CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
        """
        Review every proposed learning unit against its reader question,
        added-value claim, source anchors, and selected evidence. Return exactly
        one keep, replace, or remove decision for every draft unit in draft
        order. A replacement changes content only and must remain grounded in
        the immutable source/evidence identities. Remove any unit that is only
        paraphrase, same-meaning rewrite, repeated reasoning, or generic summary,
        or whose added_value does not identify an increment absent from the
        source. Judge inline and chapter placements by the same value standard;
        keep no quota for either. Use null replacement for keep and remove. Do
        not add units or write a summary.
        """,
        {
            "plan": dict(plan),
            "draft": dict(draft),
            "source_blocks": list(blocks),
            "glossary": list(glossary),
            "selected_evidence": list(evidence),
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
    "LITERATURE_REQUEST_PLAN_SCHEMA",
    "LITERATURE_REQUEST_PROMPT_VERSION",
    "LITERATURE_SURVEY_PROMPT_VERSION",
    "LITERATURE_SURVEY_SCHEMA",
    "VALUE_DIMENSIONS",
    "chapter_guide_prompt",
    "chapter_guide_review_prompt",
    "chapter_plan_prompt",
    "literature_request_prompt",
    "literature_survey_prompt",
]
