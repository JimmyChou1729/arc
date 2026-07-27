"""Evidence-first prompt contracts for the current Companion workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


LITERATURE_REQUEST_PROMPT_VERSION = (
    "arc.companion.literature-request-prompt.v3"
)
EVIDENCE_RESEARCH_PROMPT_VERSION = (
    "arc.companion.evidence-research-prompt.v2"
)
LITERATURE_SURVEY_PROMPT_VERSION = (
    "arc.companion.literature-survey-prompt.v3"
)
CHAPTER_GUIDE_PROMPT_VERSION = "arc.companion.chapter-learning-prompt.v7"
CHAPTER_GUIDE_REVIEW_PROMPT_VERSION = (
    "arc.companion.chapter-learning-review-prompt.v7"
)
CHAPTER_PLAN_PROMPT_VERSION = "arc.companion.chapter-plan-prompt.v8"
AUTHOR_IDENTITY_PROMPT_VERSION = "arc.companion.author-identity-prompt.v2"


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
_READER_PROFILE = _closed(
    {
        "source_type": {
            "type": "string",
            "enum": [
                "user_specified",
                "popular_or_directional",
                "research_paper",
                "textbook",
                "other",
            ],
        },
        "assumed_background": _NONEMPTY,
        "basis": _NONEMPTY,
    },
    ("source_type", "assumed_background", "basis"),
)
_READER_NEED = _closed(
    {
        "block_id": _NONEMPTY,
        "needs_companion": {"type": "boolean"},
        "reason": _NONEMPTY,
        "learning_unit_ids": _STRING_IDS,
    },
    (
        "block_id",
        "needs_companion",
        "reason",
        "learning_unit_ids",
    ),
)
CHAPTER_PLAN_SCHEMA = _closed(
    {
        "chapter_id": _NONEMPTY,
        "reader_profile": _READER_PROFILE,
        "reader_needs": {"type": "array", "items": _READER_NEED},
        "learning_units": {"type": "array", "items": _PLANNED_UNIT},
    },
    (
        "chapter_id",
        "reader_profile",
        "reader_needs",
        "learning_units",
    ),
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
CHAPTER_GUIDE_PROPOSAL_SCHEMA = _closed(
    {
        "learning_units": {"type": "array", "items": _LEARNING_UNIT},
    },
    ("learning_units",),
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
CHAPTER_GUIDE_REVIEW_AUDIT_SCHEMA = _closed(
    {
        "reader_needs_satisfied": {"type": "boolean"},
        "grounding_sufficient": {"type": "boolean"},
        "remaining_issues": {
            "type": "array",
            "items": _NONEMPTY,
            "uniqueItems": True,
        },
    },
    (
        "reader_needs_satisfied",
        "grounding_sufficient",
        "remaining_issues",
    ),
)


def literature_request_prompt(
    *,
    block_ids: Sequence[str],
    intent: str,
    has_prior_companion: bool = False,
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
            "block_ids": list(block_ids),
            "source_inputs": _source_input_manifest(
                has_prior_companion=has_prior_companion
            ),
        },
    )


def literature_survey_prompt(
    *,
    block_ids: Sequence[str],
    intent: str,
    has_prior_companion: bool = False,
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
            "block_ids": list(block_ids),
            "source_inputs": _source_input_manifest(
                has_prior_companion=has_prior_companion,
                additional=("selected-evidence",),
            ),
        },
    )


def evidence_research_prompt(
    *,
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
            "source_inputs": _source_input_manifest(
                additional=("literature-requests",)
            ),
        },
    )


def chapter_plan_prompt(
    *,
    chapter_id: str,
    title: str,
    document_title: str | None = None,
    document_outline: Sequence[str] = (),
    block_ids: Sequence[str],
    target_language: str,
    intent: str,
    has_prior_companion: bool = False,
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

        First resolve the reader profile. An explicit reader background in the
        user intent overrides every default. Otherwise, for popular,
        directional, or weakly specialized writing, assume an adult with
        average general literacy and no specialist training. For a research
        paper, assume a professional student who has completed the relevant
        discipline's foundational courses. For a textbook, assume a student
        who has completed its standard prerequisite courses, but do not assume
        difficult prerequisite concepts are already mastered confidently. Use
        the document title, outline, source, and intent to choose and explain
        the profile; do not infer expertise merely from an interested reader.

        Audit every supplied source block exactly once and in source order in
        `reader_needs`. Decide whether the resolved reader needs Companion help,
        give a concrete reason, and list the learning units that cover that
        need. A block marked as needing help must map to at least one unit whose
        anchors include that block. One unit may cover several related blocks;
        there is no per-block unit quota and no minimum unit count. Mark a block
        as not needing help only when its actual content is simple,
        self-contained, and understandable for the resolved reader, and explain
        why. Zero units are valid only when every block passes that audit.

        Look actively for unexplained works, people, events, schools,
        institutions, compressed historical claims, allusions, technical
        concepts, and skipped reasoning. Examples that rely on another text may
        require its plot, narrative levels, or mistaken-identity context. These
        are non-exhaustive signals of reader need, not required categories or
        content quotas.

        Give first priority to additions that make genuinely difficult or
        compressed material understandable for the resolved reader: supply the
        missing context behind an isolated quotation or named work, make
        skipped derivation steps explicit, bridge a real logical gap, or
        explain prerequisite knowledge that this reader profile should not be
        assumed to command confidently. Also look for evidence-grounded value
        that became visible after the source was written: later corrections,
        doubts, disputes, unexpectedly important developments, and the
        historical significance of a passage or work. These priorities do not
        remove any other useful explanatory form and do not impose a category
        or quantity quota.

        Prefer direct affirmative explanation. Use corrective contrasts such
        as “not X but Y” only when the source, user intent, or selected evidence
        establishes that the misconception actually exists. Never invent a
        belief for the reader merely to create an explanatory effect. In
        particular, do not write “the author is not denying X, but asking Y”
        unless some supplied material actually advances the denial claim; state
        affirmatively that the author uses X to ask Y and spend the saved
        attention on missing context. Write the reader profile, audit reasons,
        and purposes in the target language.
        """,
        {
            "chapter_id": chapter_id,
            "title": title,
            "document_title": document_title or title,
            "document_outline": list(document_outline),
            "target_language": target_language,
            "intent": intent,
            "block_ids": list(block_ids),
            "source_inputs": _source_input_manifest(
                has_prior_companion=has_prior_companion,
                additional=("literature-survey", "selected-evidence"),
            ),
        },
    )


_CHAPTER_GUIDE_INSTRUCTION = """
Write only the planned learning units. ARC supplies source context,
evidence, anchors, and recoverable work state; it does not constrain
your creative form. Preserve planned unit IDs and ground every unit in
its planned anchors and evidence. Use only planned unit IDs and keep
their order. A revision may omit a unit only when reviewer feedback
identifies it as redundant and the remaining units still cover every
required reader need. Choose the form that makes its
particular increment clearest: questions, close reading, distinctions,
argument maps, history, counterexamples, objections, connections, and
reading paths are non-exhaustive inspirations, not a taxonomy or quota.
Do not turn a planned increment into paraphrase, same-meaning rewrite,
repeated reasoning, or generic summary. Inline and chapter units, and
paragraph-local and cross-paragraph units, are equally important with no
quota. Write title and Markdown in the target language. Cite every
evidence-grounded claim near the claim as `[@evidence-id]`; use only
selected evidence IDs assigned to that unit. A translation lane runs
independently; use the supplied chapter glossary consistently. A prior
Companion may be supplied as optional reference material. Improve,
extend, recombine, or discard it freely. It is neither a template nor an
accepted source of current citations; use only the current selected
evidence IDs in new output.

Write for the plan's resolved reader profile and satisfy every mapped
reader need. Concentrate first on making difficult or compressed source
material understandable: give the necessary background for an isolated
quotation or named work, supply skipped derivation steps, bridge a real
logical gap, and explain prerequisite material the resolved reader should
not be assumed to command confidently. When selected evidence supports
it, add later corrections, disputes, doubts, unexpectedly important
developments, or the historical significance of the passage or work.
These are priorities, not a required taxonomy, form, or quantity quota.

Prefer direct affirmative explanation. Use “not X but Y” or another
corrective contrast only when the source, user intent, or selected
evidence establishes that the misconception actually exists; do not
manufacture a prior reader belief to make prose sound explanatory. Never
write “the author is not denying X, but asking Y” merely as a transition;
state what the author uses X to investigate and devote attention to the
missing context. Translate English excerpts or quotations into the target
language while citing their assigned English source identity nearby.

The source body is not embedded in the loop context. Inspect the
`companion-source-index` workspace input first. In direct mode, prefer the
exact cache-only `arc-paper` operations listed there and pass the cached
document reference unchanged. Read only the chapter sections or search
results needed for this loop. If the cache command is unavailable, read
the verified text-only `companion-source` workspace input. Never open
image or media assets. The index is authoritative for chapter and block
IDs and for effective equation labels.
"""


_CHAPTER_GUIDE_REVIEW_INSTRUCTION = """
Review every proposed learning unit against its planned purpose, source
anchors, selected evidence, reader profile, and reader-needs audit. ARC
provides context and recovery, not a prescribed creative form. Judge
inline/chapter and paragraph-local/cross-paragraph work by the same value
standard. Questions, close reading, distinctions, argument maps, history,
counterexamples, objections, connections, reading paths, and other forms
are non-exhaustive possibilities, never a quota.

Do not criticize merely to demonstrate reviewer activity, and do not
present a stylistic preference as a defect. If the proposal already
satisfies its reader needs, is well grounded, and has no concrete path to
meaningful improvement, accept it by choosing `stop`. Choose `continue`
only when the targeted feedback identifies a specific, achievable gain.
Feedback must be constructive rather than merely negative: explain what
to preserve, what to change, and how. When you discover a valuable new
Companion idea, include it in the proposer feedback with the relevant
source anchor and selected evidence identity so the next proposal can add
it within an existing planned unit whose anchors and assigned evidence
support it. Never invent a unit, source, or evidence identifier or ask the
proposer to change the frozen plan.

Prioritize missing background for isolated quotations or named works,
skipped derivation steps, logical gaps, prerequisite knowledge the
resolved reader may not command, and evidence-grounded later corrections,
disputes, doubts, unexpected developments, or historical significance.
Remove or replace paraphrase, repeated reasoning, generic summary, and
unsupported claims. Require nearby `[@evidence-id]` citations for
evidence-grounded claims. Never recommend removing the final useful unit
covering a block that needs help.

Treat unsupported corrective framing as a material defect. A “not X but
Y” contrast is justified only when the source, user intent, or selected
evidence shows that X is a live misconception. Otherwise tell the
proposer to state Y affirmatively and use the space for the missing
context, reasoning, or later development. A prior Companion is optional
reference material, not a template or authority.

The source body is not embedded in the loop context. Inspect the
`companion-source-index` workspace input first. In direct mode, prefer its
exact cache-only `arc-paper` operations; otherwise use the verified
text-only `companion-source` input. Read only the source passages required
to check this chapter. Never open image or media assets. Treat the index
as authoritative for chapter and block identities.
"""


def chapter_guide_proposer_instructions() -> str:
    return _instruction_contract(
        CHAPTER_GUIDE_PROMPT_VERSION,
        _CHAPTER_GUIDE_INSTRUCTION,
    )


def chapter_guide_reviewer_instructions() -> str:
    return _instruction_contract(
        CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
        _CHAPTER_GUIDE_REVIEW_INSTRUCTION,
    )


def chapter_guide_prompt(
    *,
    plan: Mapping[str, Any],
    block_ids: Sequence[str],
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    language_result: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    has_prior_companion: bool = False,
) -> str:
    return _prompt(
        CHAPTER_GUIDE_PROMPT_VERSION,
        _CHAPTER_GUIDE_INSTRUCTION,
        {
            "target_language": target_language,
            "language_result": dict(language_result),
            "plan": dict(plan),
            "block_ids": list(block_ids),
            "glossary": list(glossary),
            "selected_evidence": list(evidence),
            "source_inputs": _source_input_manifest(
                has_prior_companion=has_prior_companion
            ),
        },
    )


def chapter_guide_review_prompt(
    *,
    plan: Mapping[str, Any],
    draft: Mapping[str, Any],
    block_ids: Sequence[str],
    glossary: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    has_prior_companion: bool = False,
) -> str:
    return _prompt(
        CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
        _CHAPTER_GUIDE_REVIEW_INSTRUCTION,
        {
            "plan": dict(plan),
            "draft": dict(draft),
            "block_ids": list(block_ids),
            "glossary": list(glossary),
            "selected_evidence": list(evidence),
            "source_inputs": _source_input_manifest(
                has_prior_companion=has_prior_companion
            ),
        },
    )


def author_identity_prompt(
    *,
    title: str,
    auto_candidates: Sequence[Mapping[str, Any]],
) -> str:
    return _prompt(
        AUTHOR_IDENTITY_PROMPT_VERSION,
        """
        Verify publication authorship from the supplied title, verified source
        inputs, and automatically parsed candidates with their bases. Author names are
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
            "auto_candidates": list(auto_candidates),
            "source_inputs": _source_input_manifest(),
        },
    )


def _source_input_manifest(
    *,
    has_prior_companion: bool = False,
    additional: Sequence[str] = (),
) -> dict[str, Any]:
    """Describe verified workspace inputs without embedding their content."""

    inputs = [
        "companion-source-index",
        "companion-source",
        *additional,
    ]
    if has_prior_companion:
        inputs.append("prior-companion")
    return {
        "input_ids": inputs,
        "instructions": (
            "Inspect companion-source-index first. In direct mode, prefer the "
            "exact cache-only arc-paper operations listed there and pass its "
            "cached document reference unchanged; read only relevant sections "
            "or search results. If cache access is unavailable, read the "
            "verified text-only companion-source input. Never open image or "
            "media assets. The index is authoritative for chapter IDs, block "
            "IDs, and effective equation labels. Other named inputs are "
            "verified JSON files. In restricted or unknown mode, use the "
            "text-only fallback rather than requesting a host turn solely to "
            "read the same source."
        ),
    }


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


def _instruction_contract(version: str, instruction: str) -> str:
    return (
        f"Contract: {version}\n\n"
        + " ".join(line.strip() for line in instruction.strip().splitlines())
    )


__all__ = [
    "AUTHOR_IDENTITY_PROMPT_VERSION",
    "AUTHOR_IDENTITY_SCHEMA",
    "CHAPTER_GUIDE_PROMPT_VERSION",
    "CHAPTER_GUIDE_PROPOSAL_SCHEMA",
    "CHAPTER_GUIDE_REVIEW_AUDIT_SCHEMA",
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
    "chapter_guide_proposer_instructions",
    "chapter_guide_review_prompt",
    "chapter_guide_reviewer_instructions",
    "chapter_plan_prompt",
    "evidence_research_prompt",
    "literature_request_prompt",
    "literature_survey_prompt",
]
