"""Evidence-first prompt contracts for the current Companion workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


LITERATURE_REQUEST_PROMPT_VERSION = (
    "arc.companion.literature-request-prompt.v2"
)
EVIDENCE_RESEARCH_PROMPT_VERSION = (
    "arc.companion.evidence-research-prompt.v1"
)
LITERATURE_SURVEY_PROMPT_VERSION = (
    "arc.companion.literature-survey-prompt.v2"
)
CHAPTER_GUIDE_PROMPT_VERSION = "arc.companion.chapter-learning-prompt.v4"
CHAPTER_GUIDE_REVIEW_PROMPT_VERSION = (
    "arc.companion.chapter-learning-review-prompt.v4"
)
CHAPTER_PLAN_PROMPT_VERSION = "arc.companion.chapter-plan-prompt.v5"
AUTHOR_IDENTITY_PROMPT_VERSION = "arc.companion.author-identity-prompt.v1"


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
_EVIDENCE_CANDIDATE = _closed(
    {
        "evidence_id": _NONEMPTY,
        "title": _NONEMPTY,
        "content": _NONEMPTY,
        "source": _NONEMPTY,
    },
    ("evidence_id", "title", "content", "source"),
)
_EVIDENCE_RESPONSE = _closed(
    {
        "request_id": _NONEMPTY,
        "candidates": {
            "type": "array",
            "items": _EVIDENCE_CANDIDATE,
        },
        "selected_evidence_ids": _STRING_IDS,
        "selection_rationale": _NONEMPTY,
    },
    (
        "request_id",
        "candidates",
        "selected_evidence_ids",
        "selection_rationale",
    ),
)
EVIDENCE_RESEARCH_SCHEMA = _closed(
    {
        "responses": {
            "type": "array",
            "items": _EVIDENCE_RESPONSE,
            "minItems": 1,
        }
    },
    ("responses",),
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
        "anchor_block_ids": _BLOCK_IDS,
        "placement": {
            "type": "string",
            "enum": ["inline", "chapter"],
        },
        "purpose": _NONEMPTY,
        "evidence_ids": _STRING_IDS,
    },
    (
        "unit_id",
        "anchor_block_ids",
        "placement",
        "purpose",
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
        "unit_id": _NONEMPTY,
        "title": _NONEMPTY,
        "content_markdown": _NONEMPTY,
    },
    ("unit_id", "title", "content_markdown"),
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
        "replacement_title": {"type": ["string", "null"]},
        "replacement_markdown": {"type": ["string", "null"]},
        "reason": _NONEMPTY,
    },
    (
        "unit_id",
        "decision",
        "replacement_title",
        "replacement_markdown",
        "reason",
    ),
)
AUTHOR_IDENTITY_SCHEMA = _closed(
    {
        "authors": _STRING_IDS,
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
        },
        "basis": _NONEMPTY,
        "anchor_block_ids": _STRING_IDS,
    },
    ("authors", "confidence", "basis", "anchor_block_ids"),
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
    prior_companion: Mapping[str, Any] | None = None,
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
        modify source block IDs. A prior Companion may be supplied as optional
        working context. Use it to notice promising gaps, connections, and
        evidence leads, but do not treat its content, structure, or bibliography
        as current requirements.
        """,
        {
            "intent": intent,
            "blocks": list(blocks),
            "prior_companion": (
                dict(prior_companion)
                if prior_companion is not None
                else None
            ),
        },
    )


def literature_survey_prompt(
    *,
    blocks: Sequence[Mapping[str, Any]],
    intent: str,
    selected_evidence: Sequence[Mapping[str, Any]],
    prior_companion: Mapping[str, Any] | None = None,
) -> str:
    return _prompt(
        LITERATURE_SURVEY_PROMPT_VERSION,
        """
        Build a document-level, evidence-grounded literature survey for later
        chapter planning. Synthesize only claims supported by the selected
        evidence and source blocks. Every theme must cite supplied evidence IDs
        and existing block IDs. Record limitations explicitly; do not invent
        sources, identifiers, or unsupported consensus. A prior Companion is
        optional reference material: improve on useful insights when current
        evidence supports them, but do not inherit its claims or bibliography
        without present support.
        """,
        {
            "intent": intent,
            "blocks": list(blocks),
            "selected_evidence": list(selected_evidence),
            "prior_companion": (
                dict(prior_companion)
                if prior_companion is not None
                else None
            ),
        },
    )


def evidence_research_prompt(
    *,
    requests: Sequence[Mapping[str, Any]],
    blocks: Sequence[Mapping[str, Any]],
    target_language: str,
    intent: str,
) -> str:
    return _prompt(
        EVIDENCE_RESEARCH_PROMPT_VERSION,
        """
        Act as the evidence researcher for this Companion. Complete every
        literature request by using the search, web, paper, and other research
        tools available in the current host mode. In direct mode, use those
        tools yourself; if the host mode requires a host turn, use only the
        standard arc-llm host-turn contract. Inspect at least 20 distinct
        candidates across the full research log, including relevant sources
        named by the document, important prior history, and later work central
        to the main debates. This is a research-coverage requirement, not a
        selection quota. Give every candidate a unique evidence ID, return
        exactly one response for every planned request, select only candidates
        that add direct explanatory value, and explain each selection in the
        target language.

        English Wikipedia is an optional ordinary candidate, never a required
        source or default authority. Only en.wikipedia.org URLs are allowed for
        Wikipedia; discard every other language edition and find the English
        page or a different source. Write candidate evidence notes and
        selection rationales in the target language. Translate any English
        quotation or excerpt used in those notes into the target language while
        keeping the English page title, English URL, and citation identity.
        Never substitute a translated Wikipedia edition for the English source.
        Preserve source URLs and never invent sources or identifiers.
        """,
        {
            "target_language": target_language,
            "intent": intent,
            "requests": list(requests),
            "blocks": list(blocks),
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
    prior_companion: Mapping[str, Any] | None = None,
) -> str:
    return _prompt(
        CHAPTER_PLAN_PROMPT_VERSION,
        """
        Plan selective additions for this source chapter after reading the
        document-level literature survey. ARC supplies source context,
        evidence, anchors, and recoverable work state; it does not prescribe
        a creative or pedagogical form. Propose a learning unit only for a
        concrete increment absent from the source. `purpose` must say what
        that increment is, not impose a presentation format. Questions,
        close reading, distinctions, argument maps, history, counterexamples,
        objections, connections, and reading paths are non-exhaustive
        inspirations, not a required taxonomy or quota. Inline and chapter
        placement are equally valid; paragraph-local and cross-paragraph work
        are equally valid; there is no default or quota for either. Do not add
        paraphrase, same-meaning rewrite, repeated reasoning, or generic
        summary. Give exact source anchors and selected evidence IDs. Evidence
        IDs may be empty for a purely source-grounded addition. Terminology and
        translation are owned by a separate workflow. Never invent source or
        evidence IDs. A prior Companion may be supplied as optional reference.
        Preserve, deepen, recombine, or discard its ideas according to the
        current source, intent, and evidence; never copy its repeated format or
        treat it as a required template.
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
            "prior_companion": (
                dict(prior_companion)
                if prior_companion is not None
                else None
            ),
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
    prior_companion: Mapping[str, Any] | None = None,
) -> str:
    return _prompt(
        CHAPTER_GUIDE_PROMPT_VERSION,
        """
        Write only the planned learning units. ARC supplies source context,
        evidence, anchors, and recoverable work state; it does not constrain
        your creative form. Preserve planned unit IDs and ground every unit in
        its planned anchors and evidence. Choose the form that makes its
        particular increment clearest: questions, close reading, distinctions,
        argument maps, history, counterexamples, objections, connections, and
        reading paths are non-exhaustive inspirations, not a taxonomy or quota.
        Do not turn a planned increment into paraphrase, same-meaning rewrite,
        repeated reasoning, or generic summary. Inline and chapter units, and
        paragraph-local and cross-paragraph units, are equally important with no
        quota. Write title and Markdown in the target language. Cite every
        evidence-grounded claim near the claim as `[@evidence-id]`; use only
        selected evidence IDs assigned to that unit. A translation lane runs
        independently; use the supplied chapter glossary consistently.
        A prior Companion may be supplied as optional reference material.
        Improve, extend, recombine, or discard it freely. It is neither a
        template nor an accepted source of current citations; use only the
        current selected evidence IDs in new output.
        """,
        {
            "target_language": target_language,
            "language_result": dict(language_result),
            "plan": dict(plan),
            "blocks": list(blocks),
            "glossary": list(glossary),
            "selected_evidence": list(evidence),
            "prior_companion": (
                dict(prior_companion)
                if prior_companion is not None
                else None
            ),
        },
    )


def chapter_guide_review_prompt(
    *,
    plan: Mapping[str, Any],
    draft: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    prior_companion: Mapping[str, Any] | None = None,
) -> str:
    return _prompt(
        CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
        """
        Review every proposed learning unit against its planned purpose, source
        anchors, and selected evidence. ARC provides context and recovery, not
        a prescribed creative form. Return exactly one keep, replace, or remove
        decision for every draft unit in draft order. A replacement may change
        both title and Markdown, but may not change the unit identity, source
        anchors, placement, purpose, or evidence IDs. Remove units that are
        paraphrase, same-meaning rewrite, repeated reasoning, or generic
        summary. Questions, close reading, distinctions, argument maps,
        history, counterexamples, objections, connections, and reading paths
        are non-exhaustive inspirations, never a quota. Judge inline/chapter
        and paragraph-local/cross-paragraph work by the same value standard.
        Require nearby `[@evidence-id]` citations for evidence-grounded claims.
        Use null replacement fields for keep and remove. Do not add units or
        write a summary. A prior Companion, when supplied, is optional reference
        material rather than a template or an authority; judge the current
        draft against the current source, intent, and evidence.
        """,
        {
            "plan": dict(plan),
            "draft": dict(draft),
            "source_blocks": list(blocks),
            "glossary": list(glossary),
            "selected_evidence": list(evidence),
            "prior_companion": (
                dict(prior_companion)
                if prior_companion is not None
                else None
            ),
        },
    )


def author_identity_prompt(
    *,
    title: str,
    blocks: Sequence[Mapping[str, Any]],
    auto_candidates: Sequence[Mapping[str, Any]],
) -> str:
    return _prompt(
        AUTHOR_IDENTITY_PROMPT_VERSION,
        """
        Verify publication authorship from the supplied title, source blocks,
        and automatically parsed candidates with their bases. Author names are
        publication identity, not a constraint on Companion interpretation or
        creative form. Confirm or correct an automatic candidate when the
        supplied material supports it, or infer an author when there is no
        candidate only when the material makes the attribution very certain.
        Do not guess. Use high confidence only for a very certain attribution;
        at medium or low confidence, authors must be empty. Give the exact
        source anchors that support a high-confidence attribution. Explain the
        basis even when authors is empty. Never invent source block IDs.
        """,
        {
            "title": title,
            "blocks": list(blocks),
            "auto_candidates": list(auto_candidates),
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
    "AUTHOR_IDENTITY_PROMPT_VERSION",
    "AUTHOR_IDENTITY_SCHEMA",
    "CHAPTER_GUIDE_PROMPT_VERSION",
    "CHAPTER_GUIDE_REVIEW_PROMPT_VERSION",
    "CHAPTER_GUIDE_REVIEW_SCHEMA",
    "CHAPTER_GUIDE_SCHEMA",
    "CHAPTER_PLAN_PROMPT_VERSION",
    "CHAPTER_PLAN_SCHEMA",
    "EVIDENCE_RESEARCH_PROMPT_VERSION",
    "EVIDENCE_RESEARCH_SCHEMA",
    "LITERATURE_REQUEST_PLAN_SCHEMA",
    "LITERATURE_REQUEST_PROMPT_VERSION",
    "LITERATURE_SURVEY_PROMPT_VERSION",
    "LITERATURE_SURVEY_SCHEMA",
    "author_identity_prompt",
    "chapter_guide_prompt",
    "chapter_guide_review_prompt",
    "chapter_plan_prompt",
    "evidence_research_prompt",
    "literature_request_prompt",
    "literature_survey_prompt",
]
