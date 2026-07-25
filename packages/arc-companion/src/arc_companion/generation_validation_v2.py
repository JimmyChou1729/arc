"""Validation for the guide-only lanes in Companion's v2 workflow."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .generation_validation import CompanionContentError


def validate_chapter_plan(
    value: Any,
    *,
    chapter_id: str,
    block_ids: Sequence[str],
) -> dict[str, Any]:
    result = _exact(
        value,
        {"chapter_id", "guide", "learning_units", "evidence_requests"},
        "chapter plan",
    )
    if result["chapter_id"] != chapter_id:
        raise CompanionContentError(
            "chapter_plan_invalid", "chapter plan changed chapter_id"
        )
    guide = _nonempty(result["guide"], "chapter guide")
    units = _mapping_list(result["learning_units"], "learning units")
    evidence = _mapping_list(
        result["evidence_requests"], "evidence requests"
    )
    _unique(units, "unit_id", "chapter plan learning-unit")
    _unique(evidence, "request_id", "chapter plan evidence-request")
    allowed = set(block_ids)
    for item in (*units, *evidence):
        _validate_anchors(item.get("anchor_block_ids"), allowed)
    return {
        "chapter_id": chapter_id,
        "guide": guide,
        "learning_units": units,
        "evidence_requests": evidence,
    }


def validate_chapter_guide(
    value: Any,
    *,
    plan: Mapping[str, Any],
    evidence_ids: Sequence[str] = (),
) -> dict[str, Any]:
    result = _exact(
        value,
        {"chapter_id", "guide", "learning_units"},
        "chapter guide",
    )
    if result["chapter_id"] != plan.get("chapter_id"):
        raise CompanionContentError(
            "chapter_guide_invalid", "chapter guide changed chapter_id"
        )
    units = _mapping_list(result["learning_units"], "learning units")
    planned = {
        item["unit_id"]: item
        for item in _mapping_list(
            plan.get("learning_units"), "planned learning units"
        )
    }
    if [item.get("unit_id") for item in units] != list(planned):
        raise CompanionContentError(
            "learning_unit_coverage_invalid",
            "learning-unit IDs must exactly match the selective plan",
        )
    allowed_evidence = set(evidence_ids)
    for item in units:
        planned_item = planned[str(item["unit_id"])]
        for immutable in ("kind", "title", "anchor_block_ids"):
            if item.get(immutable) != planned_item.get(immutable):
                raise CompanionContentError(
                    "learning_unit_anchor_invalid",
                    f"learning unit changed planned {immutable}",
                )
        citations = item.get("citations")
        if (
            not isinstance(citations, list)
            or len(citations) != len(set(citations))
            or any(
                not isinstance(citation, str)
                or citation not in allowed_evidence
                for citation in citations
            )
        ):
            raise CompanionContentError(
                "evidence_citation_invalid",
                "learning-unit citation does not name frozen evidence",
            )
        _nonempty(item.get("content"), "learning-unit content")
    return {
        "chapter_id": str(result["chapter_id"]),
        "guide": _nonempty(result["guide"], "chapter guide"),
        "learning_units": units,
    }


def apply_safe_guide_review(
    draft: Mapping[str, Any],
    review: Any,
) -> tuple[dict[str, Any], str]:
    result = _exact(
        review,
        {"guide_replacement", "learning_unit_patches", "summary"},
        "chapter guide review",
    )
    guide = result["guide_replacement"]
    if guide is not None and not isinstance(guide, str):
        raise CompanionContentError(
            "review_patch_unsafe", "guide replacement must be string or null"
        )
    units = [
        dict(item)
        for item in _mapping_list(
            draft.get("learning_units"), "learning units"
        )
    ]
    patches = _mapping_list(
        result["learning_unit_patches"], "learning-unit patches"
    )
    _unique(patches, "id", "review patch")
    by_id = {item["unit_id"]: item for item in units}
    for patch in patches:
        if set(patch) != {"id", "replacement"}:
            raise CompanionContentError(
                "review_patch_unsafe", "review patch has invalid fields"
            )
        patch_id = patch["id"]
        replacement = patch["replacement"]
        if patch_id not in by_id or not isinstance(replacement, str):
            raise CompanionContentError(
                "review_patch_unsafe",
                "review patch targets an unknown ID or invalid replacement",
            )
        by_id[str(patch_id)]["content"] = replacement
    summary = _nonempty(result["summary"], "review summary")
    return (
        {
            "chapter_id": draft["chapter_id"],
            "guide": guide if guide is not None else draft["guide"],
            "learning_units": units,
        },
        summary,
    )


def _exact(
    value: Any, fields: set[str], description: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CompanionContentError(
            "model_output_invalid", f"{description} has invalid fields"
        )
    return dict(value)


def _mapping_list(value: Any, description: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise CompanionContentError(
            "model_output_invalid",
            f"{description} must be an array of objects",
        )
    return [dict(item) for item in value]


def _nonempty(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompanionContentError(
            "model_output_invalid", f"{description} must be non-empty"
        )
    return value.strip()


def _unique(
    values: Sequence[Mapping[str, Any]], key: str, description: str
) -> None:
    identities = [item.get(key) for item in values]
    if (
        any(not isinstance(item, str) or not item for item in identities)
        or len(identities) != len(set(identities))
    ):
        raise CompanionContentError(
            "model_output_invalid", f"{description} IDs are invalid"
        )


def _validate_anchors(value: Any, allowed: set[str]) -> None:
    if (
        not isinstance(value, list)
        or not value
        or len(value) != len(set(value))
        or any(
            not isinstance(item, str) or item not in allowed for item in value
        )
    ):
        raise CompanionContentError(
            "source_anchor_invalid",
            "source anchors must be unique existing block IDs",
        )


__all__ = [
    "apply_safe_guide_review",
    "validate_chapter_guide",
    "validate_chapter_plan",
]
