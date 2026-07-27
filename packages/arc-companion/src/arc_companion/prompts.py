"""Prompt contracts for source-anchored Companion generation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


CHAPTER_GUIDE_PROMPT_VERSION = "arc.companion.chapter-learning-prompt.v9"
CHAPTER_GUIDE_REVIEW_PROMPT_VERSION = (
    "arc.companion.chapter-learning-review-prompt.v9"
)
CHAPTER_PLAN_PROMPT_VERSION = "arc.companion.chapter-plan-prompt.v10"
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
_PLANNED_UNIT = _closed(
    {
        "unit_id": _NONEMPTY,
        "anchor_block_ids": _BLOCK_IDS,
        "placement": {
            "type": "string",
            "enum": ["inline", "chapter"],
        },
        "purpose": _NONEMPTY,
    },
    (
        "unit_id",
        "anchor_block_ids",
        "placement",
        "purpose",
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
_CACHED_RESOURCE = _closed(
    {
        "resource_sha256": {"type": "string", "minLength": 64},
        "resource_size": {"type": "integer", "minimum": 0},
        "media_type": _NONEMPTY,
        "source_locator": {"type": "string"},
        "filename": {"type": "string"},
    },
    (
        "resource_sha256",
        "resource_size",
        "media_type",
        "source_locator",
        "filename",
    ),
)
_REFERENCE_IDENTITY = _closed(
    {
        "arxiv_id": {"type": "string"},
        "dois": _STRING_IDS,
        "urls": _STRING_IDS,
        "title": {"type": "string"},
        "inspire_recid": {"type": "string"},
    },
    ("arxiv_id", "dois", "urls", "title", "inspire_recid"),
)
_CACHED_MATERIAL = _closed(
    {
        "identity": _REFERENCE_IDENTITY,
        "resources": {
            "type": "array",
            "items": _CACHED_RESOURCE,
            "minItems": 1,
        },
        "readable_resource": {
            "anyOf": [_CACHED_RESOURCE, {"type": "null"}],
        },
    },
    ("identity", "resources", "readable_resource"),
)
_REFERENCE = _closed(
    {
        "reference_id": _NONEMPTY,
        "title": _NONEMPTY,
        "source": _NONEMPTY,
        "dois": _STRING_IDS,
        "arxiv_ids": _STRING_IDS,
        "cached_document": {
            "type": ["object", "null"],
            "properties": {
                "source_format": {
                    "type": "string",
                    "enum": ["html", "markdown", "tex", "pdf"],
                },
                "source_sha256": {"type": "string", "minLength": 64},
                "source_size": {"type": "integer", "minimum": 0},
                "media_type": _NONEMPTY,
                "parser_contract": _NONEMPTY,
                "parsed_document_sha256": {
                    "type": "string",
                    "minLength": 64,
                },
            },
            "required": [
                "source_format",
                "source_sha256",
                "source_size",
                "media_type",
                "parser_contract",
                "parsed_document_sha256",
            ],
            "additionalProperties": False,
        },
        "cached_material": {
            "anyOf": [_CACHED_MATERIAL, {"type": "null"}],
        },
    },
    (
        "reference_id",
        "title",
        "source",
        "dois",
        "arxiv_ids",
        "cached_document",
        "cached_material",
    ),
)
_LEARNING_UNIT = _closed(
    {
        "unit_id": _NONEMPTY,
        "title": _NONEMPTY,
        "anchor_block_ids": _BLOCK_IDS,
        "placement": {
            "type": "string",
            "enum": ["inline", "chapter"],
        },
        "purpose": _NONEMPTY,
        "content_markdown": _NONEMPTY,
    },
    (
        "unit_id",
        "title",
        "anchor_block_ids",
        "placement",
        "purpose",
        "content_markdown",
    ),
)
CHAPTER_GUIDE_SCHEMA = _closed(
    {
        "chapter_id": _NONEMPTY,
        "learning_units": {"type": "array", "items": _LEARNING_UNIT},
        "references": {"type": "array", "items": _REFERENCE},
    },
    ("chapter_id", "learning_units", "references"),
)
CHAPTER_GUIDE_PROPOSAL_SCHEMA = _closed(
    {
        "learning_units": {"type": "array", "items": _LEARNING_UNIT},
        "references": {"type": "array", "items": _REFERENCE},
    },
    ("learning_units", "references"),
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
CHAPTER_GUIDE_REVIEW_AUDIT_SCHEMA = _closed(
    {
        "reader_needs_satisfied": {"type": "boolean"},
        "grounding_sufficient": {"type": "boolean"},
        "remaining_issues": {
            "type": "array",
            "items": _NONEMPTY,
            "uniqueItems": True,
        },
        "suggested_learning_units": {
            "type": "array",
            "items": _PLANNED_UNIT,
        },
        "suggested_references": {
            "type": "array",
            "items": _REFERENCE,
        },
    },
    (
        "reader_needs_satisfied",
        "grounding_sufficient",
        "remaining_issues",
        "suggested_learning_units",
        "suggested_references",
    ),
)


def chapter_plan_prompt(
    *,
    chapter_id: str,
    title: str,
    document_title: str | None = None,
    document_outline: Sequence[str] = (),
    block_ids: Sequence[str],
    block_access: Sequence[Mapping[str, Any]] = (),
    target_language: str,
    intent: str,
    has_prior_companion: bool = False,
) -> str:
    return _prompt(
        CHAPTER_PLAN_PROMPT_VERSION,
        """
        Audit this source chapter and seed selective additions. ARC supplies
        source context, anchors, and recoverable work state; it does not prescribe
        a creative or pedagogical form. Propose a learning unit only for a
        concrete increment absent from the source. `purpose` must say what
        that increment is, not impose a presentation format. Questions,
        close reading, distinctions, argument maps, history, counterexamples,
        objections, connections, and reading paths are non-exhaustive
        inspirations, not a required taxonomy or quota. Inline and chapter
        placement are equally valid; paragraph-local and cross-paragraph work
        are equally valid; there is no default or quota for either. Do not add
        paraphrase, same-meaning rewrite, repeated reasoning, or generic
        summary. Give exact source anchors. Terminology and translation are
        owned by a separate workflow. Never invent source block IDs. A prior
        Companion may be supplied as optional reference.
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
        `reader_needs`. Decide whether the resolved reader needs Companion help
        and give a concrete reason. Seed learning units when a useful approach
        is already clear, but the seed may be empty: the mandatory chapter
        proposer-reviewer loop owns the final additions. If a need lists a seed
        unit, that unit must anchor the block. One unit may cover several related blocks;
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
        as “not X but Y” only when the source, user intent, or an inspected reference
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
            "block_access": [dict(item) for item in block_access],
            "source_inputs": _source_input_manifest(
                has_prior_companion=has_prior_companion,
            ),
        },
    )


_CHAPTER_GUIDE_INSTRUCTION = """
Write the chapter's useful learning units. ARC supplies source context,
anchors, and recoverable work state; it does not constrain your creative
form. Treat planned units as seeds, not a frozen list: preserve a useful
seed's identity when revising it, and add or remove units when this materially
improves the Companion. Every final unit must use exact chapter block anchors,
and the final set must cover every block marked `needs_companion`. A unit may
cover several blocks. Choose the form that makes its increment clearest.
Questions, close reading, distinctions, argument maps, history,
counterexamples, objections, connections, and reading paths are
non-exhaustive inspirations, not a taxonomy or quota. Do not add paraphrase,
same-meaning rewrite, repeated reasoning, or generic summary.

Write titles and Markdown in the target language. Cite each externally
grounded claim nearby as `[@reference-id]` and include compact metadata for
that proposal-local ID in `references`. When a reference was admitted to the
shared reference-material cache, return its actual canonical `cached_material`
handle; do not claim caching without that handle. ARC will deterministically assign the
published reference identity and inject the chapter identity. References may
be absent or arbitrarily numerous. Include only references actually cited by
a learning unit. Proposal references contain metadata and an optional shared
paper-cache handle, never a copied reference body. Preserve every DOI and
arXiv identifier found for a cited source. A translation lane runs
independently; use the supplied chapter glossary consistently. A prior
Companion is optional reference material, not a template or accepted source.

Write for the resolved reader profile. Concentrate first on making difficult
or compressed source material understandable: provide missing background for
an isolated quotation or named work, supply skipped derivation steps, bridge
a real logical gap, and explain prerequisite material the reader should not be
assumed to command confidently. When inspected references support it, add
later corrections, disputes, doubts, unexpectedly important developments, or
the historical significance of the passage or work. These are priorities, not
a taxonomy, form, or quantity quota.

Prefer direct affirmative explanation. Use “not X but Y” or another corrective
contrast only when the source, user intent, or an inspected reference
establishes that the misconception actually exists. Never manufacture a prior
reader belief merely to make prose sound explanatory.

Translate English excerpts or quotations into the target language while
preserving and citing the source's English title and URL. English Wikipedia is
an optional ordinary source. If Wikipedia is used, only `en.wikipedia.org` is
allowed; use the English page and translate the relevant explanation rather
than citing another language edition.

Research locally and economically. Prefer an exact source already available
through the shared paper cache for a DOI, arXiv identifier, or URL. Search for
a source when it could materially improve explanation, historical context, or
later-information accuracy. Actively consider whether a useful reference is
missing, including when the seed plan names none. There is no minimum or
maximum reference count: do not search, cite, or add material merely to create
the appearance of research. If acquisition is required, discover and use any
currently available, authorized, capability-matching download tool. Do not
assume that such a capability exists or insist when authorization is absent.
A cache miss or cache write failure may remain local to your research process.

The source body is not embedded in the loop context. Inspect the
`companion-source-index` workspace input first and use its exact cache-only
paper operations when available, passing the cached reference unchanged. Use
this loop's `block_access` line ranges and selectors to read only the chapter
sections or search results needed for this loop. A verified text-only
`companion-source` input exists only in fallback-only mode. Never open image
or media assets. The loop context is authoritative for chapter and block IDs
and for effective equation labels.
"""


_CHAPTER_GUIDE_REVIEW_INSTRUCTION = """
Review every proposed learning unit against its purpose, source anchors,
references, reader profile, and reader-needs audit. ARC provides context and
recovery, not a prescribed creative form. Judge all useful explanatory forms
by the same value standard; none is a quota.

Do not criticize merely to demonstrate reviewer activity or present a
stylistic preference as a defect. If the proposal already satisfies reader
needs, is well grounded, and has no concrete path to meaningful improvement,
accept it by choosing `stop`. Choose `continue` only for a specific achievable
gain. Feedback must say what to preserve, what to change, and how.

When you discover a valuable new Companion idea, return a constructive
suggestion with exact source anchors so the next proposer can add a new unit
or improve an existing one. You may suggest a newly inspected reference with
compact metadata and an optional shared paper-cache handle. Never copy a
reference body into the payload or invent a block ID, source, DOI, arXiv
identifier, or URL. Empty suggestion arrays are correct when no addition is
worth making.

Actively consider and, when it could materially improve the Companion, inspect
a reference the proposer missed. Both roles may introduce useful new source
material. There is no minimum or maximum reference count, so never search,
criticize, or request a citation merely to make the review look more thorough.

Prioritize missing background for isolated quotations or named works,
skipped derivation steps, logical gaps, prerequisite knowledge the reader may
not command, and reference-grounded later corrections, disputes, doubts,
unexpected developments, or historical significance. Remove or replace
paraphrase, repeated reasoning, generic summary, and unsupported claims.
Require nearby `[@reference-id]` citations for externally grounded claims.
Never recommend removing the final useful unit covering a required block.

Treat unsupported corrective framing as a material defect. A “not X but Y”
contrast is justified only when supplied material shows that X is a live
misconception. Otherwise request direct affirmative prose. A prior Companion
is optional reference material, not a template or authority.

Inspect the `companion-source-index` workspace input first and use its exact
cache-only paper operations with this loop's `block_access` line ranges and
selectors. A verified text-only `companion-source` input exists only in
fallback-only mode. Read only the passages required for this chapter and never
open image or media assets.
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
    block_access: Sequence[Mapping[str, Any]] = (),
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    language_result: Mapping[str, Any],
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
            "block_access": [dict(item) for item in block_access],
            "glossary": list(glossary),
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
    block_access: Sequence[Mapping[str, Any]] = (),
    glossary: Sequence[Mapping[str, Any]],
    has_prior_companion: bool = False,
) -> str:
    return _prompt(
        CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
        _CHAPTER_GUIDE_REVIEW_INSTRUCTION,
        {
            "plan": dict(plan),
            "draft": dict(draft),
            "block_ids": list(block_ids),
            "block_access": [dict(item) for item in block_access],
            "glossary": list(glossary),
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
        *additional,
    ]
    if has_prior_companion:
        inputs.append("prior-companion")
    return {
        "input_ids": inputs,
        "conditional_input_ids": [
            "companion-source when cache_relationship is fallback_only"
        ],
        "instructions": (
            "Inspect companion-source-index first. In direct mode, prefer the "
            "exact cache-only arc-paper operations listed there and pass its "
            "cached document reference unchanged. For chapter tasks, use the "
            "task's block_access line ranges and selectors to read only the "
            "current chapter or relevant search results. A verified text-only "
            "companion-source input is present only when cache_relationship is "
            "fallback_only. Never open image or media assets. The task payload "
            "is authoritative for chapter and block IDs and effective equation "
            "labels. Other named inputs are verified JSON files. In restricted "
            "or unknown mode, use an available text-only fallback rather than "
            "requesting a host turn solely to read the same source."
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
    "CHAPTER_GUIDE_SCHEMA",
    "CHAPTER_PLAN_PROMPT_VERSION",
    "CHAPTER_PLAN_SCHEMA",
    "author_identity_prompt",
    "chapter_guide_prompt",
    "chapter_guide_proposer_instructions",
    "chapter_guide_review_prompt",
    "chapter_guide_reviewer_instructions",
    "chapter_plan_prompt",
]
