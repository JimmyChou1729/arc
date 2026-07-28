"""Prompt contracts for source-anchored Companion generation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


CHAPTER_GUIDE_PROMPT_VERSION = "arc.companion.chapter-learning-prompt.v15"
CHAPTER_GUIDE_REVIEW_PROMPT_VERSION = (
    "arc.companion.chapter-learning-review-prompt.v15"
)
CHAPTER_PLAN_PROMPT_VERSION = "arc.companion.chapter-plan-prompt.v10"
AUTHOR_IDENTITY_PROMPT_VERSION = "arc.companion.author-identity-prompt.v3"


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
_REFERENCE = _closed(
    {"title": _NONEMPTY, "source": _NONEMPTY},
    ("title", "source"),
)
_GUIDE_TEXT = _closed(
    {
        "title": _NONEMPTY,
        "content_markdown": _NONEMPTY,
    },
    ("title", "content_markdown"),
)
_SECTION_GUIDE = _closed(
    {
        "section_number": {"type": "integer", "minimum": 1},
        "title": _NONEMPTY,
        "content_markdown": _NONEMPTY,
    },
    ("section_number", "title", "content_markdown"),
)
_COMPANION = _closed(
    {
        "after_part": {"type": "integer", "minimum": 1},
        "title": _NONEMPTY,
        "content_markdown": _NONEMPTY,
    },
    ("after_part", "title", "content_markdown"),
)
CHAPTER_GUIDE_PROPOSAL_SCHEMA = _closed(
    {
        "chapter_guide": _GUIDE_TEXT,
        "section_guides": {"type": "array", "items": _SECTION_GUIDE},
        "companions": {"type": "array", "items": _COMPANION},
        "references": {"type": "array", "items": _REFERENCE},
    },
    ("chapter_guide", "section_guides", "companions", "references"),
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
_POSITIVE_INTEGERS = {
    "type": "array",
    "items": {"type": "integer", "minimum": 1},
    "uniqueItems": True,
}
CHAPTER_GUIDE_REVIEW_AUDIT_SCHEMA = _closed(
    {
        "checked_complete_chapter": {"const": True},
        "checked_part_numbers": _POSITIVE_INTEGERS,
        "checked_section_numbers": _POSITIVE_INTEGERS,
    },
    (
        "checked_complete_chapter",
        "checked_part_numbers",
        "checked_section_numbers",
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
Write the Companion for the current real chapter. The program owns chapter,
section, block, learning-unit, and reference identities. Fill only the simple
semantic template supplied in the loop context: one `chapter_guide`, sparse
`section_guides` selected by `section_number`, sparse local `companions`
selected by `after_part`, and `references` containing only `title` and
`source`.

The chapter guide appears before the body and helps the reader enter the
chapter as a whole. A section guide appears after that section's translated
heading and before its body. A companion appears after its selected source
part and translation. Add section guides and companions only where useful;
there is no quota. Each local companion must have one primary target part and
must directly explain material at that location. Do not combine independent
themes from different source locations merely to make one longer unit. Split
them, or put genuinely chapter-wide synthesis in `chapter_guide`. In
particular, do not move an explanation of an earlier title, quotation, work,
or concept to a later date merely because the same unit mentions a later
development. Work only on the current real chapter.

Every local companion and section guide must add a concrete increment of
understanding that the nearby source does not itself supply. Missing context,
an intermediate derivation, a logical bridge, prerequisite clarification, a
later correction or development, historical significance, and a substantive
connection across passages are non-exhaustive examples, not a taxonomy or
quota. A brief source phrase may identify the explanation's target, but the
body must then add new information, a missing reasoning step, context, or a
useful connection not already available at that location. Compacting,
reorganizing, or summarizing information already present in the paragraph or
chapter does not qualify for a local companion or section guide, even when
the result is fluent or shorter. Reordered facts, same-meaning rewrite,
generic summary, transitions such as “this shows”, or polished paraphrase
likewise do not qualify. If removing a proposed unit would leave the resolved
reader's understanding essentially unchanged, omit it. Simple,
self-contained source material needs no local companion.

A chapter guide has a different role. It may select, organize, and compress
information already present in the chapter when that creates a useful reading
map, foregrounds a real through-line, or gives the reader an effective way
into the chapter. It may also add new background, connections, reading
strategies, later developments, or any other useful help supported by the
source and references. Permission to compress is not a default form, a limit,
or the chapter guide's only purpose. Still, do not merely retell the chapter
or reproduce its table of contents: any selection and compression must
provide a concrete reading benefit.

The structured `title` field is the unit's sole main title.
`content_markdown` must begin with substantive prose, not another Markdown
heading. Later internal headings are allowed only when the body genuinely
needs structure.

Write titles and Markdown in the target language. Cite an externally grounded
claim nearby using the reference's one-based array position, for example
`[@1]` or `[@2]`. Include only cited references. ARC assigns publication
identities and enriches cache metadata deterministically. References may be
absent or arbitrarily numerous. The complete translation lane is frozen
before Companion generation begins. Read the original and frozen translation
together. Use the frozen translation's proper names, translated titles, and
technical terminology consistently in every generated title and body; do not
silently invent a different translation. Use the supplied chapter glossary
consistently as well. A prior Companion is optional reference material, not a
template or accepted source.

Use the reader background specified in the user intent. Otherwise, for
popular, directional, or weakly specialized writing assume an adult with
average general literacy and no specialist training. For a research paper,
assume a professional student who completed the relevant foundational
courses. For a textbook, assume a student who completed standard prerequisite
courses, but do not assume difficult prerequisite concepts are confidently
mastered.

Write every guide and companion in plain, accessible language suited to the
resolved reader. The source and translation are material to explain, not
style templates. If either is difficult, compressed, jargon-heavy, or
syntactically dense, do not imitate that style. Unpack the reasoning, explain
necessary terms, prefer concrete wording, and split overloaded sentences or
steps while preserving technical accuracy. A reader should find the
Companion easier to understand than the passage it accompanies.

Concentrate first on making difficult or compressed material understandable:
provide missing background for isolated quotations or named works, supply
skipped derivation steps, bridge real logical gaps, and explain prerequisite
material the reader should not be assumed to command. When references support
it, add later corrections, disputes, doubts, unexpectedly important
developments, or historical significance. These are priorities, not a
taxonomy or quota.

Do not decide in advance that the Companion will explain only one kind of
thing. Check the actual source for every place where the resolved reader needs
help. One useful explanation does not cancel a different need nearby: for
example, historical context does not replace an omitted equation step, and an
equation explanation does not replace needed context. When a source contains
mathematical, scientific, or other technical material, actively check for
unexplained terms, mechanisms, methods, formal steps, equations, experiments,
and relevant people, and add useful explanations rather than overlooking
them. This request adds missing coverage; it is not a reason to omit other
useful Companion material, and the total amount may grow. These are
non-exhaustive signals, not a fixed list, minimum count, or required format.

Before explaining a technical term, mechanism, or person, search the complete
source document to see whether the author explains it substantially elsewhere.
Use `arc_commands.full_document_search_examples`: one example shows a literal
term search; the “A or B” example is represented by two literal ARC commands,
one for each alternative, because the command does not require shell-regex
syntax. Replace only the final query argument. Search the original-language
name and useful alternative names when appropriate. If the source explains
the subject elsewhere, point the reader to or connect that explanation
instead of redundantly paraphrasing it. If it does not, supply the missing
explanation. A remote, highly compressed, or differently purposed occurrence
does not automatically make local help unnecessary.

Prefer direct affirmative explanation. This rule applies to every generated
field, including chapter-guide, section-guide, and local-companion titles,
definitions, opening sentences, transitions, and body prose. Do not use a
negative setup as a rhetorical shortcut for a definition or explanation.
Forms such as “not X but Y”, “not just X”, “not merely X”, “does not mean X;
instead Y”, and their target-language equivalents all count as corrective
framing. Use one only when the source, user intent, or an inspected reference
shows that X is a live misconception, and make that basis clear. Otherwise
state Y directly. Ordinary factual negation is allowed when the fact itself is
negative; never manufacture a prior reader belief.

Translate English excerpts or quotations into the target language while
preserving and citing the source's English title and URL. English Wikipedia is
an optional ordinary source; only `en.wikipedia.org` is allowed.

Research when it materially improves the Companion. Prefer a source already
available through the shared paper cache. There is no reference-count limit,
and no minimum. If acquisition is required, use any currently available and
authorized capability-matching tool; do not assume one exists or insist on
authorization that was not granted.

When `arc_commands.availability` is `exact`, the source body is not embedded
in the loop context: run the supplied commands for exact numbered parts,
complete current sections, the complete current chapter, search, and
research. Read the complete original chapter once before drafting. When
`arc_commands.translation.availability` is `exact`, also run its
`complete-current-chapter` command and read the complete frozen translation
before drafting. For every local companion, confirm its placement and wording
against both the original `source` part command and the matching frozen
translation `translation.parts` command. When source availability is
`fallback_only`, inspect the attached verified text-only Companion source
using the supplied chapter part and line metadata instead. The frozen
translation remains the terminology authority whenever it is available.
Do not explore ARC source code or cache directories to rediscover access
methods. Avoid reading the whole book when the complete current chapter is
enough. Never open image or media assets.

On every revised proposal, use only part and section locations recorded as
inspected in the preceding review payload. This is especially important for
the terminal revision, which is published without another review. A reviewer
who proposes a valuable addition at a new location must inspect and record
that location first.
"""


_CHAPTER_GUIDE_REVIEW_INSTRUCTION = """
Review the chapter guide, sparse section guides, sparse post-part companions,
and references against the actual current chapter. ARC provides context and
recovery, not a prescribed creative form.

Do not criticize merely to demonstrate reviewer activity or present a
stylistic preference as a defect. If the proposal already satisfies reader
needs, is well grounded, and has no concrete path to meaningful improvement,
accept it by choosing `stop`. Choose `continue` only for a specific achievable
gain. Feedback must say what to preserve, what to change, and how.

When you discover a valuable new Companion idea, describe it constructively
in feedback using a supplied local section or part number so the next proposer
can add or improve it. Suggest newly inspected references when useful. The
Companion-specific review payload records the source locations you inspected.
Set `checked_complete_chapter` to true, list every exact part and section read
in `checked_part_numbers` and `checked_section_numbers`, and include locations
inspected for constructive additions as well as locations already used by the
proposal. Never copy a reference body or invent a source, DOI, arXiv
identifier, or URL.

Actively consider and, when it could materially improve the Companion, inspect
a reference the proposer missed. Both roles may introduce useful new source
material. There is no minimum or maximum reference count, so never search,
criticize, or request a citation merely to make the review look more thorough.

Prioritize missing background for isolated quotations or named works,
skipped derivation steps, logical gaps, prerequisite knowledge the reader may
not command, and reference-grounded later corrections, disputes, doubts,
unexpected developments, or historical significance. Remove or replace
paraphrase, repeated reasoning, generic summary, and unsupported claims.
If an existing unit clearly helps the reader understand something and has no
specific defect, keep it. Do not remove it merely because you want to add a
different kind of explanation. When the source contains mathematical,
scientific, or other technical material, also check its terms, mechanisms,
methods, formal steps, equations, experiments, and relevant people carefully.
Suggest useful additions where the proposal missed them, even if this makes
the Companion longer. For an explanation of a term, mechanism, or person, use
the supplied full-document ARC search examples to check whether the author
already gives a substantial explanation elsewhere; for “A or B”, run both
literal alternative commands. Keep a useful cross-reference or local bridge
when the other occurrence is remote, compressed, or serves a different
purpose.
For every retained local companion and section guide, identify a concrete
understanding increment absent from the nearby source. Reordered source
facts, same-meaning rewrite, a transition such as “this shows”, or prose whose
removal would not change the reader's understanding is not an increment.
Reject a local companion or section guide that merely compacts, reorganizes,
or summarizes information already present nearby, even if it is fluent.
Require new information, a missing reasoning step, context, or a useful
connection that the source does not already make locally.

Review the chapter guide by a different standard. It may appropriately
select, organize, and compress information already in the chapter when that
creates a useful reading map, foregrounds a real through-line, or gives the
reader an effective way into the chapter. It may also add new background,
connections, reading strategies, later developments, or other useful help.
Compression is permitted, not required and not the chapter guide's only
purpose. Do not reject it solely for compressing chapter information. Request
revision when it merely retells the chapter or reproduces its contents
without a concrete reading benefit.
Require plain, accessible language in every title and body. Treat the source
and translation as material to explain, not style templates. If they are
difficult, compressed, jargon-heavy, or syntactically dense, the proposal
must make them easier to understand by unpacking reasoning, explaining
necessary terms, using concrete wording, and splitting overloaded sentences
or steps without losing technical accuracy. Request revision for needlessly
abstract, opaque, or source-imitating prose even when its facts are correct.
Compare every generated proper name, translated title, and technical term
with the frozen translation. Require the translation's established wording
throughout the Companion, including titles, unless the user explicitly asked
for a different wording. A fluent but inconsistent retranslation is a
material defect.
Require nearby positional `[@N]` citations for externally grounded claims.

Treat unsupported corrective framing as a material defect. Audit every
generated field, including titles, definitions, opening sentences,
transitions, and body prose. “Not X but Y”, “not just X”, “not merely X”,
“does not mean X; instead Y”, and target-language equivalents are all
corrective framing. They are justified only when supplied material shows that
X is a live misconception and the proposal makes that basis clear. Otherwise
request a direct affirmative replacement even when the rest of the unit is
strong; do not overlook the defect merely because the contrast occurs in a
title or a technically accurate definition. Ordinary factual negation is
allowed when the fact itself is negative. A prior Companion is optional
reference material, not a template or authority.

When `arc_commands.availability` is `exact`, run the supplied original-source
`complete-current-chapter` command before judging anything. When
`arc_commands.translation.availability` is `exact`, also run its
`complete-current-chapter` command before judging anything. Then run both
matching exact `part-N` commands for every local companion and both matching
`section-N-complete` commands for every section guide. When source
availability is `fallback_only`, inspect the corresponding complete chapter
and exact locations in the attached verified text-only Companion source using
the supplied part and line metadata; still inspect the frozen translation
through its supplied commands. In either mode, compare each unit with both
the original and translation at its proposed display location and record
every inspected location in the review payload. If useful material is
misplaced, request a move or split rather than deleting it automatically. If
a proposed constructive addition uses another location, inspect and record
that location too. Avoid reading the whole book when the complete current
chapter is enough. Never open image or media assets.
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
    block_access: Sequence[Mapping[str, Any]] = (),
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
        Prefer the bounded front-matter line ranges in `block_access`. If they
        are insufficient, use a precise search or inspect a complete relevant
        chapter. Avoid reading the whole book when narrower evidence resolves
        the identity. Every anchor must be a real source block ID. If the
        inspected source does not establish authorship, return an empty,
        non-high-confidence result.
        """,
        {
            "title": title,
            "auto_candidates": list(auto_candidates),
            "block_access": [dict(item) for item in block_access],
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
            "task's block_access line ranges and selectors first. If those "
            "excerpts are insufficient, the agent may inspect the complete "
            "current chapter. Avoid reading the whole book when chapter-scoped "
            "or narrower access is enough. A verified text-only companion-source "
            "input is present only when cache_relationship is fallback_only. "
            "Never open image or media assets. The task payload is authoritative "
            "for chapter and block IDs and effective equation labels. Other named "
            "inputs are verified JSON files. In restricted or unknown mode, use "
            "an available text-only fallback rather than requesting a host turn "
            "solely to read the same source."
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
    "CHAPTER_PLAN_PROMPT_VERSION",
    "CHAPTER_PLAN_SCHEMA",
    "author_identity_prompt",
    "chapter_guide_prompt",
    "chapter_guide_proposer_instructions",
    "chapter_guide_review_prompt",
    "chapter_guide_reviewer_instructions",
    "chapter_plan_prompt",
]
