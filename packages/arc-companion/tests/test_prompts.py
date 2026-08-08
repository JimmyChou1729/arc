from __future__ import annotations

from arc_companion.prompts import (
    chapter_guide_proposer_instructions,
    chapter_guide_reviewer_instructions,
)


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
