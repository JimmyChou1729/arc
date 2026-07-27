"""Validate the referee-owned scientific decision for one calculation attempt."""

from __future__ import annotations

import copy
from typing import Any, Mapping


_PUBLIC_REVIEW_FIELDS = {
    "schema_version",
    "action",
    "reason",
    "feedback",
    "payload",
}
_DECISION_FIELDS = {
    "calculator_assessments",
    "review_reasoning",
    "trusted_results",
    "remarks",
    "workflow_action",
}
_ASSESSMENT_FIELDS = {"proposer_id", "assessment", "reason"}
_TRUSTED_RESULT_FIELDS = {
    "summary",
    "final_result",
    "derivation",
    "validity_scope",
    "supporting_proposer_ids",
    "selected_proposer_id",
    "comparison_reasoning",
}
_REMARK_FIELDS = {"status", "summary", "reason", "related_proposer_ids"}
_WORKFLOW_ACTION_FIELDS = {
    "action",
    "reason",
    "proposed_revision",
    "expert_question",
}


def _review_decision(
    review: Mapping[str, Any],
    *,
    active_proposer_ids: list[str],
) -> dict[str, Any]:
    """Return a structurally valid referee decision without judging its science."""

    if len(active_proposer_ids) != 2 or len(set(active_proposer_ids)) != 2:
        raise ValueError("calculate review requires exactly two calculator ids")
    _require_exact_fields(review, _PUBLIC_REVIEW_FIELDS, "review")
    if review.get("schema_version") != "arc.proposer_reviewer.review.v1":
        raise ValueError(
            "review must use the public proposer-reviewer review envelope"
        )
    if review.get("action") not in {"continue", "stop"}:
        raise ValueError("review.action must be continue or stop")
    _nonempty_text(review.get("reason"), "review.reason")

    payload = review.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("review.payload must be an object")
    _require_exact_fields(payload, _DECISION_FIELDS, "review.payload")
    decision = copy.deepcopy(dict(payload))

    _validate_calculator_assessments(
        decision.get("calculator_assessments"),
        active_proposer_ids=active_proposer_ids,
    )
    _nonempty_text(
        decision.get("review_reasoning"),
        "review.payload.review_reasoning",
    )
    trusted_results = _validate_trusted_results(
        decision.get("trusted_results"),
        active_proposer_ids=active_proposer_ids,
    )
    _validate_remarks(
        decision.get("remarks"),
        active_proposer_ids=active_proposer_ids,
    )
    workflow_action = _validate_workflow_action(
        decision.get("workflow_action")
    )
    action = workflow_action["action"]
    if action == "continue" and not trusted_results:
        raise ValueError(
            "workflow_action=continue requires at least one trusted result"
        )
    if action == "retry" and trusted_results:
        raise ValueError(
            "workflow_action=retry requires trusted_results to be empty"
        )
    return decision


def _validate_calculator_assessments(
    raw: Any,
    *,
    active_proposer_ids: list[str],
) -> None:
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError("calculator_assessments must contain exactly two items")
    assessed_ids: list[str] = []
    for index, item in enumerate(raw):
        field = f"calculator_assessments[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{field} must be an object")
        _require_exact_fields(item, _ASSESSMENT_FIELDS, field)
        proposer_id = _nonempty_text(item.get("proposer_id"), f"{field}.proposer_id")
        if proposer_id not in active_proposer_ids:
            raise ValueError(
                f"{field}.proposer_id must identify an active calculator"
            )
        if item.get("assessment") not in {
            "valid",
            "invalid",
            "indeterminate",
        }:
            raise ValueError(f"{field}.assessment is invalid")
        _nonempty_text(item.get("reason"), f"{field}.reason")
        assessed_ids.append(proposer_id)
    if len(set(assessed_ids)) != 2 or set(assessed_ids) != set(
        active_proposer_ids
    ):
        raise ValueError(
            "calculator_assessments must assess each active calculator once"
        )


def _validate_trusted_results(
    raw: Any,
    *,
    active_proposer_ids: list[str],
) -> list[Mapping[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("trusted_results must be an array")
    for index, item in enumerate(raw):
        field = f"trusted_results[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{field} must be an object")
        _require_exact_fields(item, _TRUSTED_RESULT_FIELDS, field)
        for name in {
            "summary",
            "final_result",
            "derivation",
            "validity_scope",
            "comparison_reasoning",
        }:
            _nonempty_text(item.get(name), f"{field}.{name}")
        supporting_ids = item.get("supporting_proposer_ids")
        if not isinstance(supporting_ids, list):
            raise ValueError(
                f"{field}.supporting_proposer_ids must be an array"
            )
        if any(
            not isinstance(proposer_id, str)
            or proposer_id not in active_proposer_ids
            for proposer_id in supporting_ids
        ):
            raise ValueError(
                f"{field}.supporting_proposer_ids must contain only active "
                "proposer ids"
            )
        if len(supporting_ids) != len(set(supporting_ids)):
            raise ValueError(
                f"{field}.supporting_proposer_ids entries must be unique"
            )
        if set(supporting_ids) != set(active_proposer_ids):
            raise ValueError(
                f"{field}.supporting_proposer_ids must exactly match active "
                "proposer ids"
            )
        selected = _nonempty_text(
            item.get("selected_proposer_id"),
            f"{field}.selected_proposer_id",
        )
        if selected not in active_proposer_ids:
            raise ValueError(
                f"{field}.selected_proposer_id must identify an active calculator"
            )
    return raw


def _validate_remarks(
    raw: Any,
    *,
    active_proposer_ids: list[str],
) -> None:
    if not isinstance(raw, list):
        raise ValueError("remarks must be an array")
    for index, item in enumerate(raw):
        field = f"remarks[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{field} must be an object")
        _require_exact_fields(item, _REMARK_FIELDS, field)
        if item.get("status") != "untrusted":
            raise ValueError(f"{field}.status must be untrusted")
        _nonempty_text(item.get("summary"), f"{field}.summary")
        _nonempty_text(item.get("reason"), f"{field}.reason")
        related_ids = item.get("related_proposer_ids")
        if not isinstance(related_ids, list):
            raise ValueError(f"{field}.related_proposer_ids must be an array")
        if any(
            not isinstance(item_id, str)
            or item_id not in active_proposer_ids
            for item_id in related_ids
        ):
            raise ValueError(
                f"{field}.related_proposer_ids must contain only active "
                "calculator ids"
            )
        if len(related_ids) != len(set(related_ids)):
            raise ValueError(
                f"{field}.related_proposer_ids entries must be unique"
            )


def _validate_workflow_action(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("workflow_action must be an object")
    _require_exact_fields(raw, _WORKFLOW_ACTION_FIELDS, "workflow_action")
    action = raw.get("action")
    if action not in {"continue", "retry", "replan", "pause_for_human"}:
        raise ValueError("workflow_action.action is invalid")
    _nonempty_text(raw.get("reason"), "workflow_action.reason")
    proposed_revision = _nullable_text(
        raw.get("proposed_revision"),
        "workflow_action.proposed_revision",
    )
    expert_question = _nullable_text(
        raw.get("expert_question"),
        "workflow_action.expert_question",
    )
    if action == "replan" and not proposed_revision:
        raise ValueError(
            "workflow_action=replan requires a non-empty proposed_revision"
        )
    if action == "pause_for_human" and not expert_question:
        raise ValueError(
            "workflow_action=pause_for_human requires a non-empty expert_question"
        )
    return copy.deepcopy(dict(raw))


def _nullable_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value.strip() or None


def _nonempty_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    field: str,
) -> None:
    missing = sorted(expected - set(value))
    if missing:
        raise ValueError(f"{field} is missing required field: {missing[0]}")
    unknown = sorted(set(value) - expected)
    if unknown:
        raise ValueError(f"{field} contains unsupported field: {unknown[0]}")


__all__ = ["_review_decision"]
