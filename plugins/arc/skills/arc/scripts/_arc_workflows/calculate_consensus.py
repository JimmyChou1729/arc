"""Consensus validation and pause-result policy for ARC calculations."""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping

from _arc_workflows.calculate_config import (
    DEFAULT_HUMAN_GATE_PAUSE_STATUSES,
    CalculateConfig,
    CalculateStep,
    _bool_default,
)


REVISION_ACTIONS = {"revise_plan", "split_step"}
SOURCE_DISCREPANCY_STATUSES = {
    "confirmed_source_error",
    "likely_source_error",
    "ambiguous_convention",
}


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

    workflow_action = _normalized_workflow_action(consensus.get("workflow_action"), trigger_status)
    requires_human = _workflow_action_requires_human(workflow_action, allow_nonhuman_control=True)
    if requires_human:
        workflow_action = copy.deepcopy(workflow_action)
        workflow_action["action"] = "pause_for_human"
        workflow_action["requires_human"] = True
    step_status = "blocked_for_user" if requires_human else "blocked_for_revision"
    expert_question = str(workflow_action.get("expert_question", "")).strip()
    if not expert_question:
        expert_question = _default_expert_question(trigger_status, workflow_action)
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


def _review_consensus(
    review: Mapping[str, Any],
    *,
    active_proposer_ids: list[str],
    selectable_proposer_ids: list[str] | None = None,
    reviewer_reference_claim: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if selectable_proposer_ids is None:
        selectable_proposer_ids = active_proposer_ids
    if review.get("schema_version") != "arc.proposer_reviewer.review.v1":
        raise ValueError("review must use the public proposer-reviewer review envelope")
    payload = review.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("review.payload must be an object")
    consensus = payload.get("consensus")
    if not isinstance(consensus, dict):
        raise ValueError("review.payload.consensus must be an object")
    status = consensus.get("status")
    allowed_statuses = {"all_agree", "two_agree", "all_disagree", "unresolved"}
    if reviewer_reference_claim:
        allowed_statuses.add("reference_disagrees")
    if status not in allowed_statuses:
        message = "consensus.status must be all_agree, two_agree, all_disagree, or unresolved"
        if reviewer_reference_claim:
            message += ", or reference_disagrees"
        raise ValueError(message)
    consensus = dict(consensus)
    _validate_source_discrepancies(consensus)
    consensus["workflow_action"] = _normalized_workflow_action(consensus.get("workflow_action"), str(status))
    if status == "all_agree":
        _validate_best_written_selection(
            consensus,
            active_proposer_ids=active_proposer_ids,
            selectable_proposer_ids=selectable_proposer_ids,
        )
        _validate_all_agree_agreement_assessment(consensus)
    if status == "reference_disagrees":
        _validate_best_written_selection(
            consensus,
            active_proposer_ids=active_proposer_ids,
            selectable_proposer_ids=selectable_proposer_ids,
        )
        _validate_reference_disagrees_agreement_assessment(
            consensus,
            active_proposer_ids=active_proposer_ids,
        )
    return consensus


def _validate_best_written_selection(
    consensus: Mapping[str, Any],
    *,
    active_proposer_ids: list[str],
    selectable_proposer_ids: list[str],
) -> None:
    best_written = consensus.get("best_written_proposer_id")
    if not isinstance(best_written, str) or not best_written.strip():
        raise ValueError("best_written_proposer_id is required for all_agree consensus")
    if best_written not in selectable_proposer_ids:
        raise ValueError("best_written_proposer_id must identify an active or locked proposer output")
    agreed_ids = _valid_ids(consensus.get("agreed_proposer_ids", []), active_proposer_ids)
    if best_written in active_proposer_ids and best_written not in agreed_ids:
        raise ValueError("best_written_proposer_id must be one of agreed_proposer_ids for all_agree consensus")
    reason = consensus.get("best_written_selection_reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("best_written_selection_reason is required for all_agree consensus")


def _reference_disagrees_step_result(
    step: CalculateStep,
    *,
    attempts: list[dict[str, Any]],
    consensus: Mapping[str, Any],
) -> dict[str, Any]:
    workflow_action = _normalized_workflow_action(consensus.get("workflow_action"), "reference_disagrees")
    requires_human = _workflow_action_requires_human(workflow_action)
    step_status = "blocked_for_user" if requires_human else "blocked_for_revision"
    expert_question = str(workflow_action.get("expert_question", "")).strip()
    if not expert_question:
        expert_question = _default_expert_question("reference_disagrees", workflow_action)
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


def _agreement_assessment(
    consensus: Mapping[str, Any],
    *,
    status: str,
    reject_special_limit_only: bool = True,
) -> Mapping[str, Any]:
    assessment = consensus.get("agreement_assessment")
    if not isinstance(assessment, dict):
        raise ValueError(f"{status} requires payload.consensus.agreement_assessment")
    summary = assessment.get("comparison_summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError(f"{status} requires agreement_assessment.comparison_summary")
    lowered_summary = summary.lower()
    if _has_weak_reliance_marker(lowered_summary):
        raise ValueError(
            f"{status} cannot rely on formatting, spacing, visual similarity, looks identical, or string equality"
        )
    if reject_special_limit_only and assessment.get("special_limit_only") is True:
        raise ValueError(f"{status} cannot accept agreement_assessment.special_limit_only=true")
    return assessment


def _has_weak_reliance_marker(lowered_summary: str) -> bool:
    weak_reliance_patterns = [
        r"\bby\s+visual\s+inspection\b",
        r"\bbased\s+on\s+visual\s+inspection\b",
        r"\brel(?:y|ies|ied|ying)\s+on\s+visual\s+inspection\b",
        r"\blooks?\s+identical\b",
        r"\bvisually\s+identical\b",
        r"\bstring[-\s]+equality\b",
        r"\bonly\s+spacing\b",
        r"\bonly\s+formatting\b",
        r"\bformatting\s+differences\b",
    ]
    for pattern in weak_reliance_patterns:
        for match in re.finditer(pattern, lowered_summary):
            if not _has_nearby_negation(lowered_summary, match.start()):
                return True
    return False


def _has_nearby_negation(lowered_summary: str, match_start: int) -> bool:
    prefix = lowered_summary[max(0, match_start - 32) : match_start]
    return re.search(r"\b(?:not|do\s+not|does\s+not|without|never)\b(?:\W+\w+){0,3}\W*$", prefix) is not None


def _validate_all_agree_agreement_assessment(consensus: Mapping[str, Any]) -> None:
    assessment = _agreement_assessment(consensus, status="all_agree")
    for field in [
        "target_quantity_match",
        "convention_match",
        "declared_scope_match",
        "agreement_covers_full_target",
        "accepted_by_reviewer_judgment",
    ]:
        if assessment.get(field) is not True:
            raise ValueError(f"all_agree requires agreement_assessment.{field}=true")


def _validate_reference_disagrees_agreement_assessment(
    consensus: Mapping[str, Any],
    *,
    active_proposer_ids: list[str],
) -> None:
    assessment = _agreement_assessment(
        consensus,
        status="reference_disagrees",
        reject_special_limit_only=False,
    )
    agreed_ids = _valid_ids(consensus.get("agreed_proposer_ids", []), active_proposer_ids)
    if len(set(agreed_ids)) < 2:
        raise ValueError("reference_disagrees requires two agreeing blind proposer ids")
    if assessment.get("accepted_by_reviewer_judgment") is not False:
        raise ValueError("reference_disagrees requires agreement_assessment.accepted_by_reviewer_judgment=false")
    mismatch_fields = [
        "target_quantity_match",
        "convention_match",
        "declared_scope_match",
        "agreement_covers_full_target",
    ]
    if not any(assessment.get(field) is False for field in mismatch_fields):
        raise ValueError(
            "reference_disagrees requires at least one agreement_assessment match field=false"
        )


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
        {str(worker_id): {"message": str(message)} for worker_id, message in feedback.items()}
        if isinstance(feedback, Mapping)
        else {}
    )
    return {
        "attempt_number": attempt_number,
        "status": str(consensus.get("status", "")),
        "analysis": str(consensus.get("analysis", "")),
        "likely_wrong_proposer_ids": copy.deepcopy(list(consensus.get("likely_wrong_proposer_ids", []))),
        "recalculate_proposer_ids": copy.deepcopy(list(consensus.get("recalculate_proposer_ids", []))),
        "proposer_feedback": copy.deepcopy(proposer_feedback),
    }


def _next_active_for_two_agree(consensus: Mapping[str, Any], all_proposer_ids: list[str]) -> list[str] | None:
    recalculate = _valid_ids(consensus.get("recalculate_proposer_ids", []), all_proposer_ids)
    likely_wrong = _valid_ids(consensus.get("likely_wrong_proposer_ids", []), all_proposer_ids)
    next_active = recalculate or likely_wrong
    if len(next_active) == 1:
        return next_active
    return None


def _human_gate_enabled(config: CalculateConfig) -> bool:
    return _bool_default(config.human_gate.get("enabled", False), False)


def _human_gate_pause_statuses(config: CalculateConfig) -> tuple[str, ...]:
    return _human_gate_pause_statuses_from_mapping(config.human_gate)


def _human_gate_pause_statuses_from_mapping(human_gate: Mapping[str, Any]) -> tuple[str, ...]:
    statuses = human_gate.get("pause_on_statuses", DEFAULT_HUMAN_GATE_PAUSE_STATUSES)
    if not isinstance(statuses, (list, tuple)):
        return DEFAULT_HUMAN_GATE_PAUSE_STATUSES
    return tuple(str(status) for status in statuses)


def _normalized_workflow_action(raw: Any, trigger_status: str) -> dict[str, Any]:
    default = _default_workflow_action(trigger_status)
    if not isinstance(raw, dict):
        return default

    allowed_actions = {"continue", "pause_for_human", "revise_plan", "split_step", "retry"}
    allowed_issue_types = {
        "none",
        "work_note_inadequate",
        "work_note_conflict",
        "plan_wrong",
        "step_too_coarse",
        "target_ambiguous",
        "source_mapping_error",
        "calculation_disagreement",
        "reference_disagreement",
        "source_discrepancy",
        "worker_failure",
        "other",
    }
    action = str(raw.get("action", default["action"])).strip()
    if action not in allowed_actions:
        action = default["action"]
    issue_type = str(raw.get("issue_type", default["issue_type"])).strip()
    if issue_type not in allowed_issue_types:
        issue_type = default["issue_type"]
    requires_human = _bool_default(
        raw.get("requires_human", default["requires_human"]),
        bool(default["requires_human"]),
    )

    normalized = copy.deepcopy(raw)
    normalized["action"] = action
    normalized["requires_human"] = requires_human
    normalized["issue_type"] = issue_type
    normalized["reason"] = str(raw.get("reason", default["reason"]) or default["reason"])
    normalized.setdefault("proposed_revision", None)
    normalized["expert_question"] = str(raw.get("expert_question", default["expert_question"]) or "")
    return normalized


def _normalized_source_discrepancy_item(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("source_discrepancies[] items must be objects")
    status = str(raw.get("status", "") or "").strip()
    if status not in SOURCE_DISCREPANCY_STATUSES:
        raise ValueError("source_discrepancies[].status is invalid")
    return {
        "item_id": str(raw.get("item_id", "") or f"source_discrepancy_{index + 1}"),
        "status": status,
        "source_claim": str(raw.get("source_claim", "") or ""),
        "derived_result": str(raw.get("derived_result", "") or ""),
        "confidence_reason": str(raw.get("confidence_reason", "") or ""),
        "reviewer_says_no_human_convention_choice_needed": _bool_default(
            raw.get("reviewer_says_no_human_convention_choice_needed", False),
            False,
        ),
        "decision_question": str(raw.get("decision_question", "") or ""),
    }


def _normalized_source_discrepancies(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("consensus.source_discrepancies must be an array")
    return [_normalized_source_discrepancy_item(item, index) for index, item in enumerate(raw)]


def _validate_source_discrepancies(consensus: dict[str, Any]) -> None:
    if "source_discrepancy" in consensus:
        raise ValueError("consensus.source_discrepancy is not supported; use source_discrepancies")
    source_discrepancies = _normalized_source_discrepancies(consensus.get("source_discrepancies"))
    for item in source_discrepancies:
        if not item["confidence_reason"].strip():
            raise ValueError("source_discrepancies[].confidence_reason is required")
        if item["status"] in {"likely_source_error", "ambiguous_convention"} and not item[
            "decision_question"
        ].strip():
            raise ValueError("source_discrepancies[].decision_question is required for unresolved items")
        if item["status"] == "confirmed_source_error" and not item[
            "reviewer_says_no_human_convention_choice_needed"
        ]:
            item["status"] = "likely_source_error"
            item["confidence_reason"] = (
                item["confidence_reason"].rstrip()
                + " Reviewer did not explicitly state that no human convention choice is needed."
            )
    consensus["source_discrepancies"] = source_discrepancies


def _source_discrepancy_blocked_step_result(
    step: CalculateStep,
    *,
    attempts: list[dict[str, Any]],
    consensus: Mapping[str, Any],
) -> dict[str, Any] | None:
    source_discrepancies = _normalized_source_discrepancies(consensus.get("source_discrepancies"))
    unresolved = [
        item for item in source_discrepancies if item["status"] in {"likely_source_error", "ambiguous_convention"}
    ]
    if not unresolved:
        return None
    questions = []
    for item in unresolved:
        question = item["decision_question"].strip()
        if not question:
            source_claim = item["source_claim"].strip() or "the source claim"
            derived_result = item["derived_result"].strip() or "the derived result"
            question = f"Should ARC treat {source_claim} or {derived_result} as the premise?"
        questions.append(f"- {item['item_id']}: {question}")
    expert_question = (
        "Accepted derivation has source discrepancies that need human resolution before "
        f"step `{step.step_id}` can become an accepted premise:\n" + "\n".join(questions)
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


def _default_workflow_action(trigger_status: str, reason: str | None = None) -> dict[str, Any]:
    issue_type_by_status = {
        "all_agree": "none",
        "reference_disagrees": "reference_disagreement",
        "two_agree": "calculation_disagreement",
        "all_disagree": "calculation_disagreement",
        "unresolved": "calculation_disagreement",
        "failed": "worker_failure",
    }
    if trigger_status == "all_agree":
        return {
            "action": "continue",
            "requires_human": False,
            "issue_type": "none",
            "proposed_revision": None,
            "reason": reason or "reviewer accepted all_agree consensus",
            "expert_question": "",
        }
    issue_type = issue_type_by_status.get(trigger_status, "other")
    return {
        "action": "pause_for_human",
        "requires_human": True,
        "issue_type": issue_type,
        "proposed_revision": None,
        "reason": reason or f"consensus status {trigger_status} requires expert decision",
        "expert_question": _default_expert_question(
            trigger_status,
            {"issue_type": issue_type},
        ),
    }


def _workflow_action_requires_human(
    workflow_action: Mapping[str, Any],
    *,
    allow_nonhuman_control: bool = False,
) -> bool:
    action = str(workflow_action.get("action", "")).strip()
    nonhuman_actions = set(REVISION_ACTIONS)
    if allow_nonhuman_control:
        nonhuman_actions.update({"continue", "retry"})
    if action in nonhuman_actions and workflow_action.get("requires_human") is False:
        return False
    return True


def _default_expert_question(trigger_status: str, workflow_action: Mapping[str, Any]) -> str:
    issue_type = str(workflow_action.get("issue_type", "other")).strip() or "other"
    if trigger_status == "reference_disagrees":
        return "Blind derivation disagrees with the note/reference claim. Which formula or premise should ARC use next?"
    if trigger_status == "failed":
        return "A worker or validation failure stopped this step. Should ARC retry, revise the work note or plan, or use a corrected premise?"
    return (
        "Proposers did not reach accepted consensus "
        f"({trigger_status}, {issue_type}). What correction, premise, work-note revision, or plan revision should ARC use?"
    )


def _valid_ids(raw_ids: Any, all_proposer_ids: list[str]) -> list[str]:
    if not isinstance(raw_ids, list):
        return []
    allowed = set(all_proposer_ids)
    return [str(item) for item in raw_ids if str(item) in allowed]


__all__ = [
    "_failed_step_result",
    "_human_gate_blocked_step_result",
    "_human_gate_pause_statuses_from_mapping",
    "_next_active_for_two_agree",
    "_reference_disagrees_step_result",
    "_retry_feedback_record",
    "_review_consensus",
    "_source_discrepancy_blocked_step_result",
    "_valid_ids",
]
