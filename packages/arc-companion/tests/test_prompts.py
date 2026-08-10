from __future__ import annotations

from arc_companion.prompts import (
    LEGACY_CHAPTER_GUIDE_PROMPT_VERSION_V16,
    LEGACY_CHAPTER_GUIDE_REVIEW_PROMPT_VERSION_V16,
    chapter_guide_proposer_instructions,
    chapter_guide_reviewer_instructions,
)
from arc_companion.request_contracts import CompanionGenerationRecipe


def test_guide_prompts_treat_paratext_as_model_judgment() -> None:
    proposer = chapter_guide_proposer_instructions()
    reviewer = chapter_guide_reviewer_instructions()

    assert "current source segment" in proposer
    assert "current real chapter" not in proposer
    assert "current source segment" in reviewer
    assert "current real chapter" not in reviewer
    for prompt in (proposer, reviewer):
        assert "publication metadata" in prompt
        assert (
            "prefer `chapter_guide: null`" in prompt
            or "prefer a null guide" in prompt
        )
        assert "title keywords" in prompt
        assert "preface" in prompt
        assert "cross-segment reading route" in prompt
        assert "specific reading action or understanding increment" in prompt


def test_guide_prompts_bind_local_section_numbers() -> None:
    proposer = chapter_guide_proposer_instructions()
    reviewer = chapter_guide_reviewer_instructions()

    for prompt in (proposer, reviewer):
        assert "section_number" in prompt
        assert "source heading" in prompt or "source-heading" in prompt
        assert "sections" in prompt


def test_legacy_guide_prompt_recipe_remains_decodable() -> None:
    recipe = CompanionGenerationRecipe(
        chapter_guide_prompt=LEGACY_CHAPTER_GUIDE_PROMPT_VERSION_V16,
        chapter_guide_review_prompt=(
            LEGACY_CHAPTER_GUIDE_REVIEW_PROMPT_VERSION_V16
        ),
    )

    proposer = chapter_guide_proposer_instructions(
        recipe.chapter_guide_prompt
    )
    reviewer = chapter_guide_reviewer_instructions(
        recipe.chapter_guide_review_prompt
    )
    assert LEGACY_CHAPTER_GUIDE_PROMPT_VERSION_V16 in proposer
    assert LEGACY_CHAPTER_GUIDE_REVIEW_PROMPT_VERSION_V16 in reviewer
    assert "numeral printed in the source heading" not in proposer
    assert "source-heading numerals" not in reviewer
