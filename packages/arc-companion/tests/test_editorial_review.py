from __future__ import annotations

import copy

import pytest

from arc_companion.editorial_review import (
    EDITORIAL_INVENTORY_SCHEMA,
    EDITORIAL_REVIEW_SCHEMA,
    EditorialReviewError,
    editorial_proposal_digest,
    editorial_unit_content_digest,
    freeze_editorial_inventory,
    resolve_editorial_review,
    unavailable_editorial_review,
    validate_editorial_report,
)


def _unit(
    unit_id: str,
    body: str,
    *,
    purpose: str = "companion",
    citations: tuple[str, ...] = (),
) -> dict:
    return {
        "unit_id": unit_id,
        "title": f"Title {unit_id}",
        "anchor_block_ids": [f"block-{unit_id}"],
        "placement": "inline",
        "purpose": purpose,
        "content_markdown": body,
        "citations": list(citations),
    }


def _chapters() -> tuple[dict, ...]:
    return (
        {
            "chapter_id": "chapter-a",
            "title": "Chapter A",
            "translation_result": {"secret": "translated text must be excluded"},
            "learning_units": [
                _unit("unit-a", "Shared context [@ref-a].", citations=("ref-a",)),
                _unit("unit-local", "Locally necessary repetition."),
            ],
        },
        {
            "chapter_id": "chapter-b",
            "title": "Chapter B",
            "translation_result": {"secret": "another translation"},
            "learning_units": [
                _unit("unit-b", "Shared context, with a chapter-specific payoff."),
            ],
        },
        {
            "chapter_id": "chapter-c",
            "title": "Chapter C",
            "translation_result": None,
            "learning_units": [_unit("unit-c", "Disposable repetition.")],
        },
    )


def _edit(
    chapters: tuple[dict, ...],
    edit_id: str,
    unit_id: str,
    action: str,
    **replacement: str,
) -> dict:
    unit = next(
        unit
        for chapter in chapters
        for unit in chapter["learning_units"]
        if unit["unit_id"] == unit_id
    )
    return {
        "edit_id": edit_id,
        "unit_id": unit_id,
        "base_content_digest": editorial_unit_content_digest(unit),
        "action": action,
        **replacement,
    }


def _proposal(chapters: tuple[dict, ...], inventory_digest: str) -> dict:
    return {
        "inventory_digest": inventory_digest,
        "findings": [
            {
                "finding_id": "finding-shared",
                "unit_ids": ["unit-a", "unit-b"],
                "redundancy_assessment": "The setup is mechanically repeated.",
                "retained_value_analysis": "The second chapter still needs its payoff.",
                "edits": [
                    _edit(
                        chapters,
                        "edit-revise-a",
                        "unit-a",
                        "revise",
                        title="Focused setup",
                        markdown_body="Focused context [@ref-a].",
                    ),
                    _edit(chapters, "edit-omit-b", "unit-b", "omit"),
                ],
            },
            {
                "finding_id": "finding-disposable",
                "unit_ids": ["unit-a", "unit-c"],
                "redundancy_assessment": "The third unit repeats the setup.",
                "retained_value_analysis": "It adds no local value.",
                "edits": [_edit(chapters, "edit-omit-c", "unit-c", "omit")],
            },
        ],
    }


def _review(proposal: dict, inventory_digest: str) -> dict:
    return {
        "schema_version": "arc.proposer_reviewer.review.v1",
        "action": "stop",
        "reason": "Only the exact source-preserving edits should be applied.",
        "feedback": {"editorial-proposer": "Final audit complete."},
        "payload": {
            "inventory_digest": inventory_digest,
            "proposal_digest": editorial_proposal_digest(proposal),
            "approved_edit_ids": ["edit-revise-a", "edit-omit-b"],
            "rejected_edits": [
                {"edit_id": "edit-omit-c", "reason": "Keep its local framing."}
            ],
        },
    }


def _resolved_unit(resolution, unit_id: str):
    return next(
        (
            unit
            for chapter in resolution.chapters
            for unit in chapter["learning_units"]
            if unit["unit_id"] == unit_id
        ),
        None,
    )


def test_freeze_inventory_is_deterministic_complete_and_guide_only() -> None:
    chapters = _chapters()

    first = freeze_editorial_inventory(chapters)
    second = freeze_editorial_inventory(copy.deepcopy(chapters))

    assert first == second
    assert first.document["schema_version"] == EDITORIAL_INVENTORY_SCHEMA
    assert first.document["chapter_count"] == 3
    assert first.document["unit_count"] == 4
    assert first.document["reference_ids"] == ["ref-a"]
    assert first.applicable is True
    assert "translated text must be excluded" not in first.full_text
    assert "Locally necessary repetition." in first.full_text
    unit = first.document["chapters"][0]["units"][0]
    lines = first.full_text.splitlines()
    text_range = unit["view_range"]
    assert lines[text_range["title_line"] - 1] == "# Title unit-a"
    assert lines[text_range["markdown_line_start"] - 1] == (
        "Shared context [@ref-a]."
    )
    assert text_range["line_end"] == text_range["markdown_line_end"]


def test_inventory_freezes_complete_reference_union_and_checks_citations() -> None:
    chapters = _chapters()

    inventory = freeze_editorial_inventory(
        chapters,
        frozen_reference_ids=("ref-a", "ref-unused", "ref-a"),
    )

    assert inventory.document["reference_ids"] == ["ref-a", "ref-unused"]
    with pytest.raises(EditorialReviewError, match="absent from frozen references"):
        freeze_editorial_inventory(
            chapters,
            frozen_reference_ids=("ref-unused",),
        )


@pytest.mark.parametrize(
    "chapters",
    [
        (_chapters()[0],),
        (
            _chapters()[0],
            {"chapter_id": "empty", "title": "Empty", "learning_units": []},
        ),
    ],
)
def test_too_few_comparable_chapters_is_not_applicable(chapters) -> None:
    inventory = freeze_editorial_inventory(chapters)

    resolved = resolve_editorial_review(chapters, inventory, None, None)

    assert inventory.applicable is False
    assert resolved.chapters == chapters
    assert resolved.report["status"] == "not_applicable"
    assert resolved.report["counts"]["reviewed_units"] == 0


def test_final_closed_audit_applies_only_exact_approved_revise_and_omit() -> None:
    chapters = _chapters()
    frozen = copy.deepcopy(chapters)
    inventory = freeze_editorial_inventory(chapters)
    proposal = _proposal(chapters, inventory.inventory_digest)
    review = _review(proposal, inventory.inventory_digest)

    resolved = resolve_editorial_review(
        chapters,
        inventory,
        proposal,
        review,
        proposer_artifact_digest="a" * 64,
        reviewer_artifact_digest="b" * 64,
    )

    assert chapters == frozen
    revised = _resolved_unit(resolved, "unit-a")
    assert revised["unit_id"] == "unit-a"
    assert revised["title"] == "Focused setup"
    assert revised["content_markdown"] == "Focused context [@ref-a]."
    assert revised["citations"] == ["ref-a"]
    assert _resolved_unit(resolved, "unit-b") is None
    assert _resolved_unit(resolved, "unit-c")["content_markdown"] == (
        "Disposable repetition."
    )
    report = resolved.report
    validate_editorial_report(report)
    assert report["schema_version"] == EDITORIAL_REVIEW_SCHEMA
    assert report["status"] == "applied"
    assert report["counts"] == {
        "reviewed_units": 4,
        "findings": 2,
        "proposed_edits": 3,
        "revised_units": 1,
        "omitted_units": 1,
        "rejected_edits": 1,
    }
    omitted = next(
        item for item in report["changes"] if item["edit_id"] == "edit-omit-b"
    )
    rejected = next(
        item for item in report["changes"] if item["edit_id"] == "edit-omit-c"
    )
    assert omitted["original"]["markdown_body"].startswith("Shared context")
    assert omitted["final"] is None
    assert rejected["final"] == rejected["original"]
    assert rejected["rejection_reason"] == "Keep its local framing."
    assert report["findings"][0]["approval_status"] == "approved"
    assert report["findings"][1]["approval_status"] == "rejected"


def test_invalid_approved_edits_are_preserved_and_reported_rejected() -> None:
    chapters = _chapters()
    inventory = freeze_editorial_inventory(chapters)
    valid = _edit(
        chapters,
        "valid-edit",
        "unit-a",
        "revise",
        title="Valid local increment",
        markdown_body="A valid refinement [@ref-a].",
    )
    stale = _edit(chapters, "stale-edit", "unit-b", "omit")
    stale["base_content_digest"] = "0" * 64
    unknown_citation = _edit(
        chapters,
        "citation-edit",
        "unit-c",
        "revise",
        title="Bad citation",
        markdown_body="Unsupported [@ref-new].",
    )
    proposal = {
        "inventory_digest": inventory.inventory_digest,
        "findings": [
            {
                "finding_id": "finding",
                "unit_ids": ["unit-a", "unit-b", "unit-c"],
                "redundancy_assessment": "There is some mechanical overlap.",
                "retained_value_analysis": "Keep chapter-specific information.",
                "edits": [valid, stale, unknown_citation],
            }
        ],
    }
    review = {
        "schema_version": "arc.proposer_reviewer.review.v1",
        "action": "stop",
        "reason": "Approve the proposed operations subject to ARC validation.",
        "feedback": {"editorial-proposer": "Done."},
        "payload": {
            "inventory_digest": inventory.inventory_digest,
            "proposal_digest": editorial_proposal_digest(proposal),
            "approved_edit_ids": ["valid-edit", "stale-edit", "citation-edit"],
            "rejected_edits": [],
        },
    }

    resolved = resolve_editorial_review(chapters, inventory, proposal, review)

    assert _resolved_unit(resolved, "unit-a")["title"] == "Valid local increment"
    assert _resolved_unit(resolved, "unit-b") is not None
    assert _resolved_unit(resolved, "unit-c") is not None
    assert resolved.report["counts"]["revised_units"] == 1
    assert resolved.report["counts"]["rejected_edits"] == 2
    reasons = {
        item["edit_id"]: item["rejection_reason"]
        for item in resolved.report["changes"]
    }
    assert "stale" in reasons["stale-edit"]
    assert "unknown reference" in reasons["citation-edit"]


def test_unknown_unit_raw_html_and_duplicate_target_are_never_applied() -> None:
    chapters = _chapters()
    inventory = freeze_editorial_inventory(chapters)
    first = _edit(chapters, "edit-one", "unit-a", "omit")
    second = _edit(
        chapters,
        "edit-two",
        "unit-a",
        "revise",
        title="Conflicting",
        markdown_body="Conflict.",
    )
    unknown = {
        "edit_id": "edit-unknown",
        "unit_id": "unit-unknown",
        "base_content_digest": "1" * 64,
        "action": "omit",
    }
    html = _edit(
        chapters,
        "edit-html",
        "unit-c",
        "revise",
        title="Unsafe",
        markdown_body="<script>bad()</script>",
    )
    proposal = {
        "inventory_digest": inventory.inventory_digest,
        "findings": [
            {
                "finding_id": "finding",
                "unit_ids": ["unit-a", "unit-b", "unit-c"],
                "redundancy_assessment": "Possible overlap.",
                "retained_value_analysis": "Local context matters.",
                "edits": [first, second, unknown, html],
            }
        ],
    }
    review = {
        "schema_version": "arc.proposer_reviewer.review.v1",
        "action": "stop",
        "reason": "Audit.",
        "feedback": {"editorial-proposer": "Done."},
        "payload": {
            "inventory_digest": inventory.inventory_digest,
            "proposal_digest": editorial_proposal_digest(proposal),
            "approved_edit_ids": [
                "edit-one",
                "edit-two",
                "edit-unknown",
                "edit-html",
            ],
            "rejected_edits": [],
        },
    }

    resolved = resolve_editorial_review(chapters, inventory, proposal, review)

    assert resolved.chapters == chapters
    assert resolved.report["status"] == "no_changes"
    reasons = [item["rejection_reason"] for item in resolved.report["changes"]]
    assert any("multiple editorial edits" in item for item in reasons)
    assert any("unknown unit" in item for item in reasons)
    assert any("Markdown is invalid" in item for item in reasons)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda review: review.update(action="continue"),
        lambda review: review["payload"].update(proposal_digest="0" * 64),
        lambda review: review["payload"]["approved_edit_ids"].pop(),
        lambda review: review["payload"]["approved_edit_ids"].append("unknown-edit"),
    ],
)
def test_nonclosing_or_misbound_audit_applies_nothing(mutate) -> None:
    chapters = _chapters()
    inventory = freeze_editorial_inventory(chapters)
    proposal = _proposal(chapters, inventory.inventory_digest)
    review = _review(proposal, inventory.inventory_digest)
    mutate(review)

    resolved = resolve_editorial_review(chapters, inventory, proposal, review)

    assert resolved.chapters == chapters
    assert resolved.report["status"] == "no_changes"
    assert resolved.report["counts"]["rejected_edits"] == 3
    assert all(not item["applied"] for item in resolved.report["changes"])


def test_unavailable_preserves_guides_and_inventory_must_match() -> None:
    chapters = _chapters()
    inventory = freeze_editorial_inventory(chapters)

    unavailable = unavailable_editorial_review(
        chapters,
        inventory,
        reason="provider failed before consensus",
    )

    assert unavailable.chapters == chapters
    assert unavailable.report["status"] == "unavailable"
    assert unavailable.report["warnings"] == ["provider failed before consensus"]

    changed = copy.deepcopy(chapters)
    changed[0]["learning_units"][0]["title"] = "Changed title"
    with pytest.raises(EditorialReviewError, match="does not match"):
        resolve_editorial_review(changed, inventory, None, None)
