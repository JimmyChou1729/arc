"""Validate reviewer consensus payloads for ARC calculations."""

from __future__ import annotations

import re
from typing import Any, Mapping

from _arc_workflows.calculate_consensus_policy import (
    _normalized_workflow_action,
    _validate_source_discrepancies,
    _valid_ids,
)


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
        raise ValueError(
            "review must use the public proposer-reviewer review envelope"
        )
    payload = review.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("review.payload must be an object")
    consensus = payload.get("consensus")
    if not isinstance(consensus, dict):
        raise ValueError("review.payload.consensus must be an object")
    status = consensus.get("status")
    allowed_statuses = {
        "all_agree",
        "two_agree",
        "all_disagree",
        "unresolved",
    }
    if reviewer_reference_claim:
        allowed_statuses.add("reference_disagrees")
    if status not in allowed_statuses:
        message = (
            "consensus.status must be all_agree, two_agree, "
            "all_disagree, or unresolved"
        )
        if reviewer_reference_claim:
            message += ", or reference_disagrees"
        raise ValueError(message)
    consensus = dict(consensus)
    _validate_consensus_proposer_ids(
        consensus,
        active_proposer_ids=active_proposer_ids,
    )
    _validate_source_discrepancies(consensus)
    consensus["workflow_action"] = _normalized_workflow_action(
        consensus.get("workflow_action"),
        str(status),
    )
    if status == "all_agree":
        _require_exact_agreement_set(
            consensus,
            active_proposer_ids=active_proposer_ids,
            status="all_agree",
        )
        _validate_best_written_selection(
            consensus,
            active_proposer_ids=active_proposer_ids,
            selectable_proposer_ids=selectable_proposer_ids,
        )
        _validate_accepted_result(
            consensus,
            selectable_proposer_ids=selectable_proposer_ids,
            expected_reference_claim_status=(
                "agrees" if reviewer_reference_claim else "not_applicable"
            ),
        )
        _validate_all_agree_agreement_assessment(consensus)
    if status == "reference_disagrees":
        _require_exact_agreement_set(
            consensus,
            active_proposer_ids=active_proposer_ids,
            status="reference_disagrees",
        )
        _validate_best_written_selection(
            consensus,
            active_proposer_ids=active_proposer_ids,
            selectable_proposer_ids=selectable_proposer_ids,
        )
        _validate_accepted_result(
            consensus,
            selectable_proposer_ids=selectable_proposer_ids,
            expected_reference_claim_status="disagrees",
        )
        _validate_reference_disagrees_agreement_assessment(
            consensus,
            active_proposer_ids=active_proposer_ids,
        )
    if (
        status not in {"all_agree", "reference_disagrees"}
        and consensus.get("accepted_result") is not None
    ):
        raise ValueError(f"{status} consensus requires accepted_result=null")
    return consensus


def _validate_consensus_proposer_ids(
    consensus: dict[str, Any],
    *,
    active_proposer_ids: list[str],
) -> None:
    allowed = set(active_proposer_ids)
    for field in [
        "agreed_proposer_ids",
        "likely_wrong_proposer_ids",
        "recalculate_proposer_ids",
    ]:
        raw = consensus.get(field)
        if not isinstance(raw, list):
            raise ValueError(f"{field} must be an array")
        if any(
            not isinstance(item, str) or not item or item not in allowed
            for item in raw
        ):
            raise ValueError(
                f"{field} must contain only active proposer ids"
            )
        if len(raw) != len(set(raw)):
            raise ValueError(f"{field} entries must be unique")
        consensus[field] = list(raw)


def _require_exact_agreement_set(
    consensus: Mapping[str, Any],
    *,
    active_proposer_ids: list[str],
    status: str,
) -> None:
    agreed_ids = consensus["agreed_proposer_ids"]
    if set(agreed_ids) != set(active_proposer_ids):
        raise ValueError(
            f"{status} agreed_proposer_ids must exactly match "
            "active proposer ids"
        )


def _validate_accepted_result(
    consensus: Mapping[str, Any],
    *,
    selectable_proposer_ids: list[str],
    expected_reference_claim_status: str,
) -> None:
    result = consensus.get("accepted_result")
    if not isinstance(result, dict):
        raise ValueError("accepted_result must be an object")
    required = {
        "summary",
        "final_result",
        "derivation",
        "validity_scope",
        "selected_proposer_id",
        "reference_claim_status",
        "source_proposer_id",
    }
    if set(result) != required:
        raise ValueError(
            "accepted_result must contain exactly the closed "
            "accepted-result fields"
        )
    for field in [
        "summary",
        "final_result",
        "derivation",
        "validity_scope",
        "selected_proposer_id",
        "reference_claim_status",
        "source_proposer_id",
    ]:
        if not isinstance(result[field], str) or not result[field].strip():
            raise ValueError(
                f"accepted_result.{field} must be a non-empty string"
            )
    best_written = consensus.get("best_written_proposer_id")
    for field in ["selected_proposer_id", "source_proposer_id"]:
        if result[field] not in selectable_proposer_ids:
            raise ValueError(
                f"accepted_result.{field} must identify an active or "
                "locked proposer output"
            )
        if result[field] != best_written:
            raise ValueError(
                f"accepted_result.{field} must match "
                "best_written_proposer_id"
            )
    if result["reference_claim_status"] not in {
        "agrees",
        "disagrees",
        "not_applicable",
    }:
        raise ValueError(
            "accepted_result.reference_claim_status is invalid"
        )
    if result["reference_claim_status"] != expected_reference_claim_status:
        raise ValueError(
            "accepted_result.reference_claim_status must be "
            f"{expected_reference_claim_status}"
        )


def _validate_best_written_selection(
    consensus: Mapping[str, Any],
    *,
    active_proposer_ids: list[str],
    selectable_proposer_ids: list[str],
) -> None:
    best_written = consensus.get("best_written_proposer_id")
    if not isinstance(best_written, str) or not best_written.strip():
        raise ValueError(
            "best_written_proposer_id is required for all_agree consensus"
        )
    if best_written not in selectable_proposer_ids:
        raise ValueError(
            "best_written_proposer_id must identify an active or "
            "locked proposer output"
        )
    agreed_ids = _valid_ids(
        consensus.get("agreed_proposer_ids", []),
        active_proposer_ids,
    )
    if best_written in active_proposer_ids and best_written not in agreed_ids:
        raise ValueError(
            "best_written_proposer_id must be one of agreed_proposer_ids "
            "for all_agree consensus"
        )
    reason = consensus.get("best_written_selection_reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            "best_written_selection_reason is required for all_agree consensus"
        )


def _agreement_assessment(
    consensus: Mapping[str, Any],
    *,
    status: str,
    reject_special_limit_only: bool = True,
) -> Mapping[str, Any]:
    assessment = consensus.get("agreement_assessment")
    if not isinstance(assessment, dict):
        raise ValueError(
            f"{status} requires payload.consensus.agreement_assessment"
        )
    summary = assessment.get("comparison_summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError(
            f"{status} requires agreement_assessment.comparison_summary"
        )
    lowered_summary = summary.lower()
    if _has_weak_reliance_marker(lowered_summary):
        raise ValueError(
            f"{status} cannot rely on formatting, spacing, visual similarity, "
            "looks identical, or string equality"
        )
    if (
        reject_special_limit_only
        and assessment.get("special_limit_only") is True
    ):
        raise ValueError(
            f"{status} cannot accept "
            "agreement_assessment.special_limit_only=true"
        )
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


def _has_nearby_negation(
    lowered_summary: str,
    match_start: int,
) -> bool:
    prefix = lowered_summary[max(0, match_start - 32) : match_start]
    return (
        re.search(
            r"\b(?:not|do\s+not|does\s+not|without|never)\b"
            r"(?:\W+\w+){0,3}\W*$",
            prefix,
        )
        is not None
    )


def _validate_all_agree_agreement_assessment(
    consensus: Mapping[str, Any],
) -> None:
    assessment = _agreement_assessment(consensus, status="all_agree")
    for field in [
        "target_quantity_match",
        "convention_match",
        "declared_scope_match",
        "agreement_covers_full_target",
        "accepted_by_reviewer_judgment",
    ]:
        if assessment.get(field) is not True:
            raise ValueError(
                f"all_agree requires agreement_assessment.{field}=true"
            )


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
    agreed_ids = _valid_ids(
        consensus.get("agreed_proposer_ids", []),
        active_proposer_ids,
    )
    if len(set(agreed_ids)) < 2:
        raise ValueError(
            "reference_disagrees requires two agreeing blind proposer ids"
        )
    if assessment.get("accepted_by_reviewer_judgment") is not False:
        raise ValueError(
            "reference_disagrees requires "
            "agreement_assessment.accepted_by_reviewer_judgment=false"
        )
    mismatch_fields = [
        "target_quantity_match",
        "convention_match",
        "declared_scope_match",
        "agreement_covers_full_target",
    ]
    if not any(assessment.get(field) is False for field in mismatch_fields):
        raise ValueError(
            "reference_disagrees requires at least one "
            "agreement_assessment match field=false"
        )


__all__ = ["_review_consensus"]
