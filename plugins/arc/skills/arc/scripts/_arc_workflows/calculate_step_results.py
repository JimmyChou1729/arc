"""Calculation step-result construction and retry transition policy."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from _arc_workflows.calculate_config import (
    CalculateConfig,
    CalculateStep,
    _bool_default,
)
from _arc_workflows.calculate_consensus_policy import (
    _default_expert_question,
    _default_workflow_action,
    _human_gate_pause_statuses_from_mapping,
    _normalized_source_discrepancies,
    _normalized_workflow_action,
    _valid_ids,
    _workflow_action_requires_human,
)


def _failed_step_result(
    config: CalculateConfig,
    step: CalculateStep,
    *,
    attempts: list[dict[str, Any]],
    error: str,
) -> dict[str, Any]:
    gated_block = _human_gate_blocked_step_result(
        config,
        step,
        attempts=attempts,
        consensus={
            "status": "failed",
            "analysis": error,
            "workflow_action": _default_workflow_action("failed", error),
        },
        trigger_status="failed",
        error=error,
    )
    if gated_block is not None:
        return gated_block
    return {
        "step_id": step.step_id,
        "kind": step.kind,
        "status": "failed",
        "attempts": attempts,
        "accepted_output": None,
        "blocked_output": None,
        "reviewer_consensus": None,
        "error": error,
    }


def _human_gate_blocked_step_result(
    config: CalculateConfig,
    step: CalculateStep,
    *,
    attempts: list[dict[str, Any]],
    consensus: Mapping[str, Any],
    trigger_status: str,
    error: str | None = None,
) -> dict[str, Any] | None:
    if not _human_gate_enabled(config):
        return None
    if trigger_status not in _human_gate_pause_statuses(config):
        return None

    workflow_action = _normalized_workflow_action(
        consensus.get("workflow_action"),
        trigger_status,
    )
    requires_human = _workflow_action_requires_human(workflow_action)
    if requires_human:
        workflow_action = copy.deepcopy(workflow_action)
        workflow_action["action"] = "pause_for_human"
        workflow_action["requires_human"] = True
    step_status = (
        "blocked_for_user" if requires_human else "blocked_for_revision"
    )
    expert_question = str(
        workflow_action.get("expert_question", "")
    ).strip()
    if not expert_question:
        expert_question = _default_expert_question(
            trigger_status,
            workflow_action,
        )
    blocked_output = {
        "reason": "human_gate",
        "trigger_status": trigger_status,
        "requires_human": requires_human,
        "workflow_action": workflow_action,
        "expert_question": expert_question,
        "analysis": str(consensus.get("analysis", "")),
        "last_consensus": copy.deepcopy(dict(consensus)),
    }
    if error:
        blocked_output["error"] = error
    return {
        "step_id": step.step_id,
        "kind": step.kind,
        "status": step_status,
        "attempts": attempts,
        "accepted_output": None,
        "blocked_output": blocked_output,
        "reviewer_consensus": dict(consensus),
        "error": error,
    }


def _workflow_action_blocked_step_result(
    step: CalculateStep,
    *,
    attempts: list[dict[str, Any]],
    consensus: Mapping[str, Any],
) -> dict[str, Any] | None:
    workflow_action = _normalized_workflow_action(
        consensus.get("workflow_action"),
        str(consensus.get("status", "")),
    )
    if (
        workflow_action["action"] == "continue"
        and workflow_action["requires_human"] is False
    ):
        return None
    requires_human = _workflow_action_requires_human(workflow_action)
    expert_question = str(
        workflow_action.get("expert_question", "")
    ).strip()
    if requires_human and not expert_question:
        expert_question = _default_expert_question(
            str(consensus.get("status", "")),
            workflow_action,
        )
    return {
        "step_id": step.step_id,
        "kind": step.kind,
        "status": (
            "blocked_for_user" if requires_human else "blocked_for_revision"
        ),
        "attempts": attempts,
        "accepted_output": None,
        "blocked_output": {
            "reason": "workflow_action",
            "trigger_status": str(consensus.get("status", "")),
            "requires_human": requires_human,
            "workflow_action": workflow_action,
            "expert_question": expert_question,
            "analysis": str(consensus.get("analysis", "")),
            "last_consensus": copy.deepcopy(dict(consensus)),
        },
        "reviewer_consensus": dict(consensus),
    }


def _reference_disagrees_step_result(
    step: CalculateStep,
    *,
    attempts: list[dict[str, Any]],
    consensus: Mapping[str, Any],
) -> dict[str, Any]:
    workflow_action = _normalized_workflow_action(
        consensus.get("workflow_action"),
        "reference_disagrees",
    )
    requires_human = _workflow_action_requires_human(workflow_action)
    step_status = (
        "blocked_for_user" if requires_human else "blocked_for_revision"
    )
    expert_question = str(
        workflow_action.get("expert_question", "")
    ).strip()
    if not expert_question:
        expert_question = _default_expert_question(
            "reference_disagrees",
            workflow_action,
        )
    return {
        "step_id": step.step_id,
        "kind": step.kind,
        "status": step_status,
        "attempts": attempts,
        "accepted_output": None,
        "blocked_output": {
            "reason": "reference_disagrees",
            "trigger_status": "reference_disagrees",
            "requires_human": requires_human,
            "workflow_action": workflow_action,
            "expert_question": expert_question,
            "analysis": str(consensus.get("analysis", "")),
            "last_consensus": copy.deepcopy(dict(consensus)),
        },
        "reviewer_consensus": dict(consensus),
    }


def _retry_feedback_record(
    review: Mapping[str, Any],
    consensus: Mapping[str, Any],
    *,
    attempt_number: int,
    blind_reference: bool = False,
) -> dict[str, Any]:
    if blind_reference:
        return {
            "attempt_number": attempt_number,
            "status": "retry_required",
            "analysis": (
                "Recompute the supplied calculation independently. Do not use "
                "validation-only material as a derivation input."
            ),
            "likely_wrong_proposer_ids": [],
            "recalculate_proposer_ids": [],
            "proposer_feedback": {},
        }
    feedback = review.get("feedback", {})
    proposer_feedback = (
        {
            str(worker_id): {"message": str(message)}
            for worker_id, message in feedback.items()
        }
        if isinstance(feedback, Mapping)
        else {}
    )
    return {
        "attempt_number": attempt_number,
        "status": str(consensus.get("status", "")),
        "analysis": str(consensus.get("analysis", "")),
        "likely_wrong_proposer_ids": copy.deepcopy(
            list(consensus.get("likely_wrong_proposer_ids", []))
        ),
        "recalculate_proposer_ids": copy.deepcopy(
            list(consensus.get("recalculate_proposer_ids", []))
        ),
        "proposer_feedback": copy.deepcopy(proposer_feedback),
    }


def _next_active_for_two_agree(
    consensus: Mapping[str, Any],
    all_proposer_ids: list[str],
) -> list[str] | None:
    recalculate = _valid_ids(
        consensus.get("recalculate_proposer_ids", []),
        all_proposer_ids,
    )
    likely_wrong = _valid_ids(
        consensus.get("likely_wrong_proposer_ids", []),
        all_proposer_ids,
    )
    next_active = recalculate or likely_wrong
    if len(next_active) == 1:
        return next_active
    return None


def _human_gate_enabled(config: CalculateConfig) -> bool:
    return _bool_default(config.human_gate.get("enabled", False), False)


def _human_gate_pause_statuses(config: CalculateConfig) -> tuple[str, ...]:
    return _human_gate_pause_statuses_from_mapping(config.human_gate)


def _source_discrepancy_blocked_step_result(
    step: CalculateStep,
    *,
    attempts: list[dict[str, Any]],
    consensus: Mapping[str, Any],
) -> dict[str, Any] | None:
    source_discrepancies = _normalized_source_discrepancies(
        consensus.get("source_discrepancies")
    )
    unresolved = [
        item
        for item in source_discrepancies
        if item["status"] in {"likely_source_error", "ambiguous_convention"}
    ]
    if not unresolved:
        return None
    questions = []
    for item in unresolved:
        question = item["decision_question"].strip()
        if not question:
            source_claim = item["source_claim"].strip() or "the source claim"
            derived_result = (
                item["derived_result"].strip() or "the derived result"
            )
            question = (
                f"Should ARC treat {source_claim} or {derived_result} "
                "as the premise?"
            )
        questions.append(f"- {item['item_id']}: {question}")
    expert_question = (
        "Accepted derivation has source discrepancies that need human "
        f"resolution before step `{step.step_id}` can become an accepted "
        "premise:\n"
        + "\n".join(questions)
    )
    workflow_action = {
        "action": "pause_for_human",
        "requires_human": True,
        "issue_type": "source_discrepancy",
        "proposed_revision": None,
        "reason": "accepted result has non-confirmed source discrepancy",
        "expert_question": expert_question,
    }
    return {
        "step_id": step.step_id,
        "kind": step.kind,
        "status": "blocked_for_user",
        "attempts": attempts,
        "accepted_output": None,
        "blocked_output": {
            "reason": "source_discrepancy_requires_human",
            "trigger_status": "all_agree",
            "requires_human": True,
            "workflow_action": workflow_action,
            "expert_question": expert_question,
            "source_discrepancies": copy.deepcopy(unresolved),
            "analysis": str(consensus.get("analysis", "")),
            "last_consensus": copy.deepcopy(dict(consensus)),
        },
        "reviewer_consensus": dict(consensus),
    }


__all__ = [
    "_failed_step_result",
    "_human_gate_blocked_step_result",
    "_next_active_for_two_agree",
    "_reference_disagrees_step_result",
    "_retry_feedback_record",
    "_source_discrepancy_blocked_step_result",
    "_workflow_action_blocked_step_result",
]
