"""Calculation step-result construction for referee-owned decisions."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from _arc_workflows.calculate_config import CalculateStep


def _failed_step_result(
    step: CalculateStep,
    *,
    attempts: list[dict[str, Any]],
    error: str,
) -> dict[str, Any]:
    return {
        "step_id": step.step_id,
        "kind": step.kind,
        "status": "failed",
        "attempts": attempts,
        "accepted_output": None,
        "blocked_output": None,
        "reviewer_decision": None,
        "error": error,
    }


def _accepted_step_result(
    step: CalculateStep,
    *,
    attempts: list[dict[str, Any]],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "step_id": step.step_id,
        "kind": step.kind,
        "status": "accepted",
        "attempts": attempts,
        "accepted_output": _trusted_output(decision),
        "blocked_output": None,
        "reviewer_decision": copy.deepcopy(dict(decision)),
        "error": None,
    }


def _blocked_step_result(
    step: CalculateStep,
    *,
    attempts: list[dict[str, Any]],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    workflow_action = copy.deepcopy(dict(decision["workflow_action"]))
    action = str(workflow_action["action"])
    status = (
        "blocked_for_user"
        if action == "pause_for_human"
        else "blocked_for_revision"
    )
    trusted_output = _trusted_output(decision)
    return {
        "step_id": step.step_id,
        "kind": step.kind,
        "status": status,
        "attempts": attempts,
        "accepted_output": trusted_output,
        "blocked_output": {
            "reason": action,
            "workflow_action": workflow_action,
            "trusted_results": copy.deepcopy(
                list(decision.get("trusted_results", []))
            ),
            "remarks": copy.deepcopy(list(decision.get("remarks", []))),
            "review_reasoning": str(decision.get("review_reasoning", "")),
        },
        "reviewer_decision": copy.deepcopy(dict(decision)),
        "error": None,
    }


def _trusted_output(decision: Mapping[str, Any]) -> dict[str, Any] | None:
    trusted_results = decision.get("trusted_results")
    if not isinstance(trusted_results, list) or not trusted_results:
        return None
    return {"trusted_results": copy.deepcopy(trusted_results)}


def _retry_feedback_record(
    decision: Mapping[str, Any],
    *,
    attempt_number: int,
    blind_reference: bool = False,
) -> dict[str, Any]:
    shared_instruction = str(decision["workflow_action"]["reason"])
    if blind_reference:
        shared_instruction = (
            "Recompute the supplied step independently. Do not use or infer "
            "reviewer-only reference material."
        )
    return {
        "attempt_number": attempt_number,
        "action": "retry",
        "shared_instruction": shared_instruction,
    }


__all__ = [
    "_accepted_step_result",
    "_blocked_step_result",
    "_failed_step_result",
    "_retry_feedback_record",
]
