"""Validation for evidence-first selective Companion generation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .prompts import VALUE_DIMENSIONS

class CompanionContentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_literature_request_plan(
    value: Any,
    *,
    block_ids: Sequence[str],
) -> dict[str, Any]:
    result = _exact(value, {"requests"}, "literature request plan")
    requests = _mapping_list(result["requests"], "literature requests")
    if not requests:
        raise CompanionContentError(
            "literature_request_plan_invalid",
            "literature request plan must inspect candidate evidence",
        )
    _unique(requests, "request_id", "literature request")
    allowed = set(block_ids)
    normalized: list[dict[str, Any]] = []
    expected = {
        "request_id",
        "kind",
        "query",
        "purpose",
        "anchor_block_ids",
    }
    for request in requests:
        if set(request) != expected:
            raise CompanionContentError(
                "literature_request_plan_invalid",
                "literature request has invalid fields",
            )
        kind = request["kind"]
        if kind not in {"paper", "web", "user"}:
            raise CompanionContentError(
                "literature_request_plan_invalid",
                "literature request kind is invalid",
            )
        anchors = _validate_anchors(
            request["anchor_block_ids"], allowed
        )
        normalized.append(
            {
                "request_id": _nonempty(
                    request["request_id"], "request ID"
                ),
                "kind": kind,
                "query": _nonempty(request["query"], "request query"),
                "purpose": _nonempty(
                    request["purpose"], "request purpose"
                ),
                "anchor_block_ids": anchors,
            }
        )
    return {"requests": normalized}


def validate_literature_survey(
    value: Any,
    *,
    block_ids: Sequence[str],
    evidence_ids: Sequence[str],
) -> dict[str, Any]:
    result = _exact(
        value, {"themes", "limitations"}, "literature survey"
    )
    themes = _mapping_list(result["themes"], "literature themes")
    _unique(themes, "theme_id", "literature theme")
    allowed_blocks = set(block_ids)
    allowed_evidence = set(evidence_ids)
    normalized: list[dict[str, Any]] = []
    expected = {
        "theme_id",
        "title",
        "synthesis",
        "anchor_block_ids",
        "evidence_ids",
    }
    for theme in themes:
        if set(theme) != expected:
            raise CompanionContentError(
                "literature_survey_invalid",
                "literature theme has invalid fields",
            )
        cited = _string_ids(
            theme["evidence_ids"],
            "literature theme evidence",
            allow_empty=False,
        )
        if any(item not in allowed_evidence for item in cited):
            raise CompanionContentError(
                "literature_survey_invalid",
                "literature theme cites unknown selected evidence",
            )
        normalized.append(
            {
                "theme_id": _nonempty(
                    theme["theme_id"], "literature theme ID"
                ),
                "title": _nonempty(
                    theme["title"], "literature theme title"
                ),
                "synthesis": _nonempty(
                    theme["synthesis"], "literature theme synthesis"
                ),
                "anchor_block_ids": _validate_anchors(
                    theme["anchor_block_ids"], allowed_blocks
                ),
                "evidence_ids": cited,
            }
        )
    limitations = _string_ids(
        result["limitations"], "literature limitations"
    )
    return {"themes": normalized, "limitations": limitations}


def validate_chapter_plan(
    value: Any,
    *,
    chapter_id: str,
    block_ids: Sequence[str],
    evidence_ids: Sequence[str] = (),
) -> dict[str, Any]:
    result = _exact(
        value,
        {"chapter_id", "learning_units"},
        "chapter plan",
    )
    if result["chapter_id"] != chapter_id:
        raise CompanionContentError(
            "chapter_plan_invalid", "chapter plan changed chapter_id"
        )
    units = _mapping_list(result["learning_units"], "learning units")
    _unique(units, "unit_id", "chapter plan learning-unit")
    allowed_blocks = set(block_ids)
    allowed_evidence = set(evidence_ids)
    normalized = [
        _validate_planned_unit(
            item,
            allowed_blocks=allowed_blocks,
            allowed_evidence=allowed_evidence,
        )
        for item in units
    ]
    return {"chapter_id": chapter_id, "learning_units": normalized}


def validate_chapter_guide(
    value: Any,
    *,
    plan: Mapping[str, Any],
    evidence_ids: Sequence[str] = (),
    allow_removed: bool = False,
) -> dict[str, Any]:
    result = _exact(
        value,
        {"chapter_id", "learning_units"},
        "chapter learning output",
    )
    if result["chapter_id"] != plan.get("chapter_id"):
        raise CompanionContentError(
            "chapter_guide_invalid",
            "chapter learning output changed chapter_id",
        )
    units = _mapping_list(result["learning_units"], "learning units")
    planned_list = _mapping_list(
        plan.get("learning_units"), "planned learning units"
    )
    planned = {item["unit_id"]: item for item in planned_list}
    unit_ids = [item.get("unit_id") for item in units]
    planned_ids = list(planned)
    coverage_valid = (
        _is_ordered_subsequence(unit_ids, planned_ids)
        if allow_removed
        else unit_ids == planned_ids
    )
    if not coverage_valid:
        raise CompanionContentError(
            "learning_unit_coverage_invalid",
            (
                "reviewed learning-unit IDs must be an ordered subset "
                "of the selective plan"
                if allow_removed
                else "learning-unit IDs must exactly match the selective plan"
            ),
        )
    allowed_evidence = set(evidence_ids)
    normalized: list[dict[str, Any]] = []
    immutable = (
        "kind",
        "title",
        "anchor_block_ids",
        "placement",
        "reader_question",
        "added_value",
        "value_dimensions",
        "evidence_ids",
    )
    for item in units:
        if set(item) != {
            "unit_id",
            *immutable,
            "content",
        }:
            raise CompanionContentError(
                "chapter_guide_invalid",
                "learning unit has invalid fields",
            )
        planned_item = planned[str(item["unit_id"])]
        for field in immutable:
            if item.get(field) != planned_item.get(field):
                raise CompanionContentError(
                    "learning_unit_anchor_invalid",
                    f"learning unit changed planned {field}",
                )
        cited = _string_ids(
            item["evidence_ids"], "learning-unit evidence"
        )
        if any(item not in allowed_evidence for item in cited):
            raise CompanionContentError(
                "evidence_citation_invalid",
                "learning unit cites unknown selected evidence",
            )
        normalized.append(
            {
                **dict(planned_item),
                "content": _nonempty(
                    item.get("content"), "learning-unit content"
                ),
            }
        )
    return {
        "chapter_id": str(result["chapter_id"]),
        "learning_units": normalized,
    }


def _is_ordered_subsequence(
    values: Sequence[Any], expected: Sequence[Any]
) -> bool:
    if (
        any(not isinstance(value, str) for value in values)
        or len(set(values)) != len(values)
    ):
        return False
    position = 0
    for value in values:
        while position < len(expected) and expected[position] != value:
            position += 1
        if position == len(expected):
            return False
        position += 1
    return True


def apply_safe_guide_review(
    draft: Mapping[str, Any],
    review: Any,
) -> tuple[dict[str, Any], tuple[dict[str, str], ...]]:
    result = _exact(
        review, {"decisions"}, "chapter learning review"
    )
    units = _mapping_list(
        draft.get("learning_units"), "learning units"
    )
    decisions = _mapping_list(result["decisions"], "review decisions")
    expected_ids = [str(item["unit_id"]) for item in units]
    if [item.get("unit_id") for item in decisions] != expected_ids:
        raise CompanionContentError(
            "review_patch_unsafe",
            "review decisions must exactly cover draft units in order",
        )
    output: list[dict[str, Any]] = []
    audit: list[dict[str, str]] = []
    for unit, raw in zip(units, decisions, strict=True):
        if set(raw) != {
            "unit_id",
            "decision",
            "replacement",
            "reason",
        }:
            raise CompanionContentError(
                "review_patch_unsafe",
                "review decision has invalid fields",
            )
        decision = raw["decision"]
        replacement = raw["replacement"]
        reason = _nonempty(raw["reason"], "review reason")
        if decision not in {"keep", "replace", "remove"}:
            raise CompanionContentError(
                "review_patch_unsafe", "review decision is invalid"
            )
        if decision == "replace":
            replacement = _nonempty(
                replacement, "review replacement"
            )
            output.append({**unit, "content": replacement})
        elif replacement is not None:
            raise CompanionContentError(
                "review_patch_unsafe",
                "keep/remove decisions require null replacement",
            )
        elif decision == "keep":
            output.append(unit)
        audit.append(
            {
                "unit_id": str(unit["unit_id"]),
                "decision": str(decision),
                "reason": reason,
            }
        )
    return (
        {
            "chapter_id": draft["chapter_id"],
            "learning_units": output,
        },
        tuple(audit),
    )


def _validate_planned_unit(
    item: Mapping[str, Any],
    *,
    allowed_blocks: set[str],
    allowed_evidence: set[str],
) -> dict[str, Any]:
    expected = {
        "unit_id",
        "kind",
        "title",
        "anchor_block_ids",
        "placement",
        "reader_question",
        "added_value",
        "value_dimensions",
        "evidence_ids",
    }
    if set(item) != expected:
        raise CompanionContentError(
            "chapter_plan_invalid",
            "planned learning unit has invalid fields",
        )
    kind = item["kind"]
    if kind not in {
        "prerequisite",
        "intuition",
        "derivation",
        "example",
        "misconception",
        "further_reading",
    }:
        raise CompanionContentError(
            "chapter_plan_invalid", "learning-unit kind is invalid"
        )
    placement = item["placement"]
    if placement not in {"inline", "chapter"}:
        raise CompanionContentError(
            "chapter_plan_invalid", "learning-unit placement is invalid"
        )
    evidence = _string_ids(
        item["evidence_ids"], "planned learning-unit evidence"
    )
    if any(value not in allowed_evidence for value in evidence):
        raise CompanionContentError(
            "chapter_plan_invalid",
            "planned learning unit cites unknown selected evidence",
        )
    if kind == "further_reading" and not evidence:
        raise CompanionContentError(
            "chapter_plan_invalid",
            "further-reading units require selected evidence",
        )
    value_dimensions = _string_ids(
        item["value_dimensions"],
        "learning-unit value dimensions",
        allow_empty=False,
    )
    if any(
        dimension not in VALUE_DIMENSIONS
        for dimension in value_dimensions
    ):
        raise CompanionContentError(
            "chapter_plan_invalid",
            "learning-unit value dimension is unsupported",
        )
    return {
        "unit_id": _nonempty(item["unit_id"], "learning-unit ID"),
        "kind": kind,
        "title": _nonempty(item["title"], "learning-unit title"),
        "anchor_block_ids": _validate_anchors(
            item["anchor_block_ids"], allowed_blocks
        ),
        "placement": placement,
        "reader_question": _nonempty(
            item["reader_question"], "reader question"
        ),
        "added_value": _nonempty(
            item["added_value"], "added value"
        ),
        "value_dimensions": value_dimensions,
        "evidence_ids": evidence,
    }


def _exact(
    value: Any, fields: set[str], description: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CompanionContentError(
            "model_output_invalid", f"{description} has invalid fields"
        )
    return dict(value)


def _mapping_list(
    value: Any, description: str
) -> list[dict[str, Any]]:
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
        any(
            not isinstance(item, str) or not item
            for item in identities
        )
        or len(identities) != len(set(identities))
    ):
        raise CompanionContentError(
            "model_output_invalid", f"{description} IDs are invalid"
        )


def _validate_anchors(
    value: Any, allowed: set[str]
) -> list[str]:
    anchors = _string_ids(
        value, "source anchors", allow_empty=False
    )
    if any(item not in allowed for item in anchors):
        raise CompanionContentError(
            "source_anchor_invalid",
            "source anchors must be unique existing block IDs",
        )
    return anchors


def _string_ids(
    value: Any,
    description: str,
    *,
    allow_empty: bool = True,
) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or any(
            not isinstance(item, str) or not item.strip()
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise CompanionContentError(
            "model_output_invalid",
            f"{description} must be unique non-empty strings",
        )
    return [item.strip() for item in value]


__all__ = [
    "CompanionContentError",
    "apply_safe_guide_review",
    "validate_chapter_guide",
    "validate_chapter_plan",
    "validate_literature_request_plan",
    "validate_literature_survey",
]
