"""Normalization policy shared by calculation consensus and step results."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from _arc_workflows.calculate_config import (
    DEFAULT_HUMAN_GATE_PAUSE_STATUSES,
    _bool_default,
)


REVISION_ACTIONS = {"revise_plan", "split_step"}
SOURCE_DISCREPANCY_STATUSES = {
    "confirmed_source_error",
    "likely_source_error",
    "ambiguous_convention",
}


def _human_gate_pause_statuses_from_mapping(
    human_gate: Mapping[str, Any],
) -> tuple[str, ...]:
    statuses = human_gate.get(
        "pause_on_statuses",
        DEFAULT_HUMAN_GATE_PAUSE_STATUSES,
    )
    if not isinstance(statuses, (list, tuple)):
        return DEFAULT_HUMAN_GATE_PAUSE_STATUSES
    return tuple(str(status) for status in statuses)


def _normalized_workflow_action(raw: Any, trigger_status: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("consensus.workflow_action must be an object")

    allowed_actions = {
        "continue",
        "pause_for_human",
        "revise_plan",
        "split_step",
        "retry",
    }
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
    action = str(raw.get("action", "")).strip()
    if action not in allowed_actions:
        raise ValueError("workflow_action.action is invalid")
    issue_type = str(raw.get("issue_type", "")).strip()
    if issue_type not in allowed_issue_types:
        raise ValueError("workflow_action.issue_type is invalid")
    requires_human = raw.get("requires_human")
    if type(requires_human) is not bool:
        raise ValueError("workflow_action.requires_human must be a boolean")
    proposed_revision = raw.get("proposed_revision")
    if proposed_revision is not None and not isinstance(proposed_revision, str):
        raise ValueError("workflow_action.proposed_revision must be a string or null")
    if (
        action in REVISION_ACTIONS
        and requires_human is False
        and (
            not isinstance(proposed_revision, str)
            or not proposed_revision.strip()
        )
    ):
        raise ValueError(
            "nonhuman revise_plan/split_step requires a non-empty proposed_revision"
        )

    normalized = copy.deepcopy(raw)
    normalized["action"] = action
    normalized["requires_human"] = requires_human
    normalized["issue_type"] = issue_type
    normalized["reason"] = str(
        raw.get("reason", f"consensus status {trigger_status}") or ""
    )
    normalized["proposed_revision"] = proposed_revision
    normalized["expert_question"] = str(raw.get("expert_question", "") or "")
    return normalized


def _normalized_source_discrepancy_item(
    raw: Any,
    index: int,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("source_discrepancies[] items must be objects")
    status = str(raw.get("status", "") or "").strip()
    if status not in SOURCE_DISCREPANCY_STATUSES:
        raise ValueError("source_discrepancies[].status is invalid")
    return {
        "item_id": str(
            raw.get("item_id", "") or f"source_discrepancy_{index + 1}"
        ),
        "status": status,
        "source_claim": str(raw.get("source_claim", "") or ""),
        "derived_result": str(raw.get("derived_result", "") or ""),
        "confidence_reason": str(raw.get("confidence_reason", "") or ""),
        "reviewer_says_no_human_convention_choice_needed": _bool_default(
            raw.get(
                "reviewer_says_no_human_convention_choice_needed",
                False,
            ),
            False,
        ),
        "decision_question": str(raw.get("decision_question", "") or ""),
    }


def _normalized_source_discrepancies(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("consensus.source_discrepancies must be an array")
    return [
        _normalized_source_discrepancy_item(item, index)
        for index, item in enumerate(raw)
    ]


def _validate_source_discrepancies(consensus: dict[str, Any]) -> None:
    if "source_discrepancy" in consensus:
        raise ValueError(
            "consensus.source_discrepancy is not supported; "
            "use source_discrepancies"
        )
    source_discrepancies = _normalized_source_discrepancies(
        consensus.get("source_discrepancies")
    )
    for item in source_discrepancies:
        if not item["confidence_reason"].strip():
            raise ValueError(
                "source_discrepancies[].confidence_reason is required"
            )
        if (
            item["status"] in {"likely_source_error", "ambiguous_convention"}
            and not item["decision_question"].strip()
        ):
            raise ValueError(
                "source_discrepancies[].decision_question is required "
                "for unresolved items"
            )
        if (
            item["status"] == "confirmed_source_error"
            and not item[
                "reviewer_says_no_human_convention_choice_needed"
            ]
        ):
            item["status"] = "likely_source_error"
            item["confidence_reason"] = (
                item["confidence_reason"].rstrip()
                + " Reviewer did not explicitly state that no human "
                "convention choice is needed."
            )
    consensus["source_discrepancies"] = source_discrepancies


def _default_workflow_action(
    trigger_status: str,
    reason: str | None = None,
) -> dict[str, Any]:
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
        "reason": (
            reason
            or f"consensus status {trigger_status} requires expert decision"
        ),
        "expert_question": _default_expert_question(
            trigger_status,
            {"issue_type": issue_type},
        ),
    }


def _workflow_action_requires_human(
    workflow_action: Mapping[str, Any],
) -> bool:
    action = str(workflow_action.get("action", "")).strip()
    if (
        action in REVISION_ACTIONS
        and workflow_action.get("requires_human") is False
    ):
        return False
    return True


def _default_expert_question(
    trigger_status: str,
    workflow_action: Mapping[str, Any],
) -> str:
    issue_type = (
        str(workflow_action.get("issue_type", "other")).strip() or "other"
    )
    if trigger_status == "reference_disagrees":
        return (
            "Blind derivation disagrees with the note/reference claim. "
            "Which formula or premise should ARC use next?"
        )
    if trigger_status == "failed":
        return (
            "A worker or validation failure stopped this step. Should ARC "
            "retry, revise the work note or plan, or use a corrected premise?"
        )
    return (
        "Proposers did not reach accepted consensus "
        f"({trigger_status}, {issue_type}). What correction, premise, "
        "work-note revision, or plan revision should ARC use?"
    )


def _valid_ids(raw_ids: Any, all_proposer_ids: list[str]) -> list[str]:
    if not isinstance(raw_ids, list):
        return []
    allowed = set(all_proposer_ids)
    return [str(item) for item in raw_ids if str(item) in allowed]
