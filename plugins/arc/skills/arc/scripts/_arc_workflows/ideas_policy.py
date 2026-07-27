"""Operational policy and scientific-readiness diagnostics for ARC ideas."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any, Mapping

from arc_proposer_reviewer import BatchRequest, LoopSpec

from _arc_workflows.ideas_config import ConfigError, IdeasConfig


MODEL_TIER_RANKS = {"low": 1, "medium": 2, "high": 3, "xhigh": 4}


def scientific_run_status(
    durable_lifecycle: str,
    loop_lifecycles: Iterable[str],
    *,
    trace_verified: bool,
) -> str:
    """Separate scientific usability from the durable executor lifecycle."""
    if durable_lifecycle == "paused":
        return "paused"
    if durable_lifecycle != "succeeded" or not trace_verified:
        return "failed"
    lifecycles = tuple(loop_lifecycles)
    succeeded = sum(lifecycle == "succeeded" for lifecycle in lifecycles)
    if lifecycles and succeeded == len(lifecycles):
        return "succeeded"
    if succeeded:
        return "degraded"
    return "failed"


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


def loop_requires_idea_assessment(loop: LoopSpec) -> bool:
    schema = loop.reviewer.output_schema
    if not isinstance(schema, Mapping):
        return False
    required = schema.get("required")
    return isinstance(required, list) and "idea_assessment" in required


def scientific_readiness(
    assessment: Any,
) -> tuple[str, list[str], dict[str, Any]]:
    """Classify route-neutral reviewer caveats without excluding an idea."""
    if not isinstance(assessment, Mapping):
        return (
            "unassessed",
            ["missing_idea_assessment"],
            _empty_feasibility_classification(),
        )

    warnings: list[str] = []
    core_not_ready = False
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
        warnings.append("first_calculation_is_not_feasible")
        core_not_ready = True
    if assessment.get("bounded_first_calculation_ready") is not True:
        warnings.append("bounded_first_calculation_is_not_ready")
        core_not_ready = True
    if blocking_failures:
        warnings.append("blocking_feasibility_failures")
        warnings.extend(
            f"blocking_feasibility_failure: {failure}"
            for failure in blocking_failures
        )
        core_not_ready = True
    if manageable_risks:
        warnings.extend(
            f"manageable_feasibility_risk: {risk}"
            for risk in manageable_risks
        )
    if feasibility_status == "feasible_with_named_risk" and not manageable_risks:
        warnings.append(
            "feasible_with_named_risk_requires_named_manageable_risk"
        )
    if well_definedness == "partially_defined":
        warnings.append("mathematical_problem_is_partially_defined")
    elif well_definedness != "well_defined":
        warnings.append("mathematical_problem_is_not_well_defined")
        core_not_ready = True
    if external_method_status not in {"not_used", "valid"}:
        warnings.append(
            "external_method_status_is_"
            + (external_method_status or "missing")
        )
        rationale = str(assessment.get("external_method_rationale", "")).strip()
        if rationale:
            warnings.append(f"external_method_rationale: {rationale}")

    return (
        _readiness_state(warnings, core_not_ready=core_not_ready),
        list(dict.fromkeys(warnings)),
        {
            "policy": "model_selected_route_advisory_readiness",
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


def _empty_feasibility_classification() -> dict[str, Any]:
    return {
        "policy": "missing_assessment",
        "feasibility_status": "",
        "well_definedness": "",
        "bounded_first_calculation_ready": False,
        "blocking_failures": [],
        "manageable_risks": [],
        "external_method_status": "",
    }


def _readiness_state(
    warnings: list[str],
    *,
    core_not_ready: bool,
) -> str:
    if core_not_ready:
        return "not_ready"
    if warnings:
        return "ready_with_risk"
    return "ready"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
