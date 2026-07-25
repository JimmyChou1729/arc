"""Operational and qualification policy for the ARC ideas workflow."""

from __future__ import annotations

import copy
import os
import re
from typing import Any, Mapping

from arc_proposer_reviewer import BatchRequest, LoopSpec

from _arc_workflows.ideas_config import ConfigError, IdeasConfig


MODEL_TIER_RANKS = {"low": 1, "medium": 2, "high": 3, "xhigh": 4}
DEFAULT_CROSS_DOMAIN_PROFILES = [
    {
        "profile_id": "forward_transfer",
        "mission": (
            "Treat the first domain card as the source and choose the strongest distinct target from the remaining "
            "cards. Transfer one concrete, mature source method, mechanism, formal structure, or constraint."
        ),
    },
    {
        "profile_id": "reverse_transfer",
        "mission": (
            "Treat the first domain card as the target and choose the strongest distinct source from the remaining "
            "cards. Find a reverse transfer that creates a substantive new target result."
        ),
    },
    {
        "profile_id": "method_transfer",
        "mission": (
            "Compare both directions and choose the strongest method or formalism transfer. State the exact "
            "translation dictionary and the target calculation it newly enables."
        ),
    },
    {
        "profile_id": "observable_or_constraint_transfer",
        "mission": (
            "Compare both directions and transfer an observable, consistency condition, validation strategy, "
            "or constraint that yields a new discriminating target-domain result."
        ),
    },
    {
        "profile_id": "high_upside_wildcard",
        "mission": (
            "Pursue the highest-upside feasible bridge, including a challenge to a standard target assumption. "
            "Require explicit compatibility checks, a bounded first calculation, and a kill criterion."
        ),
    },
]


def max_concurrent_loops(proposal_count: int) -> int:
    raw = os.environ.get("ARC_IDEAS_MAX_CONCURRENT_LOOPS", "12")
    try:
        configured = int(raw)
    except ValueError as exc:
        raise ConfigError(
            "ARC_IDEAS_MAX_CONCURRENT_LOOPS must be a positive integer"
        ) from exc
    if configured <= 0:
        raise ConfigError(
            "ARC_IDEAS_MAX_CONCURRENT_LOOPS must be a positive integer"
        )
    return min(proposal_count, configured)


def concurrency_warning(
    config: IdeasConfig,
    proposal_count: int,
    *,
    max_concurrent: int,
    request: BatchRequest,
) -> str:
    round_counts = sorted({loop.max_rounds for loop in request.loops})
    round_text = (
        f"{round_counts[0]} reviewer reports per loop"
        if len(round_counts) == 1
        else f"reviewer report counts {round_counts}"
    )
    return (
        "WARNING: Running "
        f"{len(config.variants)} variants x {config.loops_per_variant} proposer-reviewer loops "
        f"with {round_text} and loop concurrency capped at {max_concurrent} ({proposal_count} loops). "
        "The typed batch stores durable artifacts through arc-jobs."
    )


def model_tier_warnings(request: BatchRequest) -> list[str]:
    problems: list[str] = []
    for loop in request.loops:
        reviewer_rank = MODEL_TIER_RANKS.get(loop.reviewer.model.tier)
        if reviewer_rank is None:
            continue
        for proposer in loop.proposers:
            proposer_rank = MODEL_TIER_RANKS.get(proposer.model.tier)
            if proposer_rank is not None and proposer_rank > reviewer_rank:
                problems.append(
                    f"{loop.loop_id}: {proposer.worker_id}={proposer.model.tier} > "
                    f"{loop.reviewer.worker_id}={loop.reviewer.model.tier}"
                )
    if not problems:
        return []
    return [
        "WARNING: REVIEWER MODEL TIER BELOW PROPOSER. "
        "Reviewer feedback may be less useful when the reviewer is configured with a lower model tier than the proposer. "
        "Affected assignments: " + "; ".join(problems)
    ]


def cross_domain_profile(
    config: IdeasConfig,
    *,
    idea_index: int,
) -> dict[str, str]:
    profiles = config.exploration_profiles or DEFAULT_CROSS_DOMAIN_PROFILES
    try:
        return copy.deepcopy(profiles[idea_index - 1])
    except IndexError as exc:
        raise ConfigError(
            f"No cross-domain exploration profile is configured for idea {idea_index}"
        ) from exc


def is_cross_domain_context(context: Mapping[str, Any]) -> bool:
    return (
        context.get("generation_mode") == "cross_domain"
        or context.get("variant_id") == "cross_domain"
    )


def loop_requires_idea_assessment(loop: LoopSpec) -> bool:
    schema = loop.reviewer.output_schema
    if not isinstance(schema, Mapping):
        return False
    required = schema.get("required")
    return isinstance(required, list) and "idea_assessment" in required


def single_domain_qualification(
    assessment: Any,
) -> tuple[bool, list[str], dict[str, Any]]:
    reasons: list[str] = []
    if not isinstance(assessment, Mapping):
        return (
            False,
            [*reasons, "missing_idea_assessment"],
            _empty_single_feasibility_classification(),
        )

    feasibility_status = str(assessment.get("feasibility_status", ""))
    well_definedness = str(assessment.get("mathematical_well_definedness", ""))
    external_method_status = str(assessment.get("external_method_status", ""))
    blocking_failures = _string_list(
        assessment.get("blocking_feasibility_failures")
    )
    manageable_risks = _string_list(
        assessment.get("manageable_feasibility_risks")
    )

    if feasibility_status not in {"feasible", "feasible_with_named_risk"}:
        reasons.append("first_calculation_is_not_feasible")
    if assessment.get("bounded_first_calculation_ready") is not True:
        reasons.append("bounded_first_calculation_is_not_ready")
    if blocking_failures:
        reasons.append("blocking_feasibility_failures")
    if feasibility_status == "feasible_with_named_risk" and not manageable_risks:
        reasons.append("feasible_with_named_risk_requires_named_manageable_risk")
    if well_definedness == "not_well_defined" or well_definedness not in {
        "well_defined",
        "partially_defined",
    }:
        reasons.append("mathematical_problem_is_not_well_defined")
    if external_method_status not in {"not_used", "valid"}:
        reasons.append("external_method_must_be_not_used_or_valid")

    return (
        not reasons,
        reasons,
        {
            "policy": "explicit_blocking_and_manageable_v1",
            "feasibility_status": feasibility_status,
            "well_definedness": well_definedness,
            "bounded_first_calculation_ready": (
                assessment.get("bounded_first_calculation_ready") is True
            ),
            "blocking_failures": blocking_failures,
            "manageable_risks": manageable_risks,
            "external_method_status": external_method_status,
        },
    )


def cross_qualification(
    proposer: Mapping[str, Any],
    assessment: Any,
    marks: Mapping[str, Any],
    *,
    cross_context: Mapping[str, Any],
) -> tuple[bool, list[str], str, dict[str, Any]]:
    reasons: list[str] = []
    if not isinstance(assessment, Mapping):
        return (
            False,
            [*reasons, "missing_cross_domain_assessment"],
            "",
            _empty_compatibility_classification(),
        )

    cards = cross_context.get("domain_cards", [])
    known_domain_ids = {
        str(card.get("field_id", "")).strip()
        for card in cards
        if isinstance(card, Mapping) and str(card.get("field_id", "")).strip()
    }
    source = str(assessment.get("source_field_id", "")).strip()
    target = str(assessment.get("target_field_id", "")).strip()
    if not source or not target or source == target:
        reasons.append("source_and_target_must_be_distinct")
    if source not in known_domain_ids or target not in known_domain_ids:
        reasons.append("source_or_target_is_not_a_manifest_field")

    roles = proposer.get("domain_roles")
    if not isinstance(roles, Mapping):
        reasons.append("missing_proposer_domain_roles")
    elif str(roles.get("source_field_id", "")).strip() != source or str(
        roles.get("target_field_id", "")
    ).strip() != target:
        reasons.append("proposer_and_reviewer_domain_roles_disagree")

    required_values = {
        "transfer_status": "genuine",
        "source_ingredient_validity": "valid",
        "target_adaptation_validity": "valid",
    }
    for field, required in required_values.items():
        if assessment.get(field) != required:
            reasons.append(f"{field}_must_be_{required}")
    if assessment.get("target_contribution_status") not in {
        "substantial",
        "transformative",
    }:
        reasons.append("target_contribution_must_be_substantial_or_transformative")
    if assessment.get("feasibility_status") not in {
        "feasible",
        "feasible_with_named_risk",
    }:
        reasons.append("first_calculation_is_not_feasible")
    compatibility = compatibility_classification(assessment)
    if compatibility["blocking_failures"]:
        reasons.append("blocking_compatibility_failures")
    if (
        assessment.get("feasibility_status") == "feasible_with_named_risk"
        and not compatibility["manageable_risks"]
    ):
        reasons.append("feasible_with_named_risk_requires_named_manageable_risk")
    if assessment.get("disqualifying_reasons"):
        reasons.append("reviewer_reported_disqualifying_reasons")
    novelty = assessment.get("novelty_coverage")
    if not isinstance(novelty, Mapping) or not all(
        novelty.get(scope) is True
        for scope in ("source_domain", "target_domain", "intersection")
    ):
        reasons.append("source_target_and_intersection_novelty_checks_are_required")

    thresholds = {
        "cross_domain_transfer_quality": 10,
        "substantive_target_contribution": 14,
        "scientific_value": 6,
        "calculation_feasibility": 6,
        "problem_well_definedness": 6,
    }
    for field, minimum in thresholds.items():
        try:
            value = float(marks.get(field, 0))
        except (TypeError, ValueError):
            value = 0
        if value < minimum:
            reasons.append(f"{field}_below_{minimum}")

    signature = normalized_transfer_signature(assessment.get("transfer_signature"))
    if not signature:
        reasons.append("complete_transfer_signature_is_required")
    return not reasons, reasons, signature, compatibility


def compatibility_classification(
    assessment: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        "blocking_compatibility_failures" in assessment
        or "manageable_compatibility_risks" in assessment
    ):
        return {
            "policy": "explicit_blocking_and_manageable_v2",
            "blocking_failures": _string_list(
                assessment.get("blocking_compatibility_failures")
            ),
            "manageable_risks": _string_list(
                assessment.get("manageable_compatibility_risks")
            ),
        }

    legacy = _string_list(assessment.get("compatibility_failures"))
    if assessment.get("feasibility_status") == "feasible_with_named_risk":
        return {
            "policy": "legacy_compatibility_failures_as_named_risks",
            "blocking_failures": [],
            "manageable_risks": legacy,
        }
    return {
        "policy": "legacy_compatibility_failures_as_blocking",
        "blocking_failures": legacy,
        "manageable_risks": [],
    }


def normalized_transfer_signature(raw: Any) -> str:
    if not isinstance(raw, Mapping):
        return ""
    fields = (
        "direction",
        "transferred_ingredient",
        "target_result",
        "first_calculation",
    )
    values = [
        re.sub(r"\s+", " ", str(raw.get(field, "")).strip().lower())
        for field in fields
    ]
    if any(not value for value in values):
        return ""
    return " | ".join(values)


def normalized_central_mechanism(raw: Any) -> str:
    if not isinstance(raw, Mapping):
        return ""
    values = [
        re.sub(r"\s+", " ", str(raw.get(field, "")).strip().lower())
        for field in ("direction", "transferred_ingredient")
    ]
    if any(not value for value in values):
        return ""
    return " | ".join(values)


def _empty_single_feasibility_classification() -> dict[str, Any]:
    return {
        "policy": "missing_assessment",
        "feasibility_status": "",
        "well_definedness": "",
        "bounded_first_calculation_ready": False,
        "blocking_failures": [],
        "manageable_risks": [],
        "external_method_status": "",
    }


def _empty_compatibility_classification() -> dict[str, Any]:
    return {
        "policy": "missing_assessment",
        "blocking_failures": [],
        "manageable_risks": [],
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
