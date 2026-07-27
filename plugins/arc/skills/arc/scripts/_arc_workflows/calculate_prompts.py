"""Prompt, worker, and materialized batch construction for ARC calculations."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from arc_proposer_reviewer import (
    BatchFailurePolicy,
    BatchRequest,
    LoopSpec,
    ProposerFailurePolicy,
)
from arc_proposer_reviewer.models import BATCH_SCHEMA_VERSION

from _arc_workflows.calculate_config import (
    CALCULATOR_IDS,
    CalculateConfig,
    CalculateStep,
)
from _arc_workflows.calculate_context import caller_context
from _arc_workflows.calculate_prompt_builders import (
    proposer_worker,
    reviewer_worker,
)


def _attempt_batch_request(
    config: CalculateConfig,
    step: CalculateStep,
    *,
    attempt_number: int,
    retry_feedback: list[dict[str, Any]],
    accepted_step_outputs: Mapping[str, Any],
) -> BatchRequest:
    """Build one deterministic, independent public proposer-reviewer batch."""

    attempt_id = _attempt_id(step.step_id, attempt_number)
    calculator_ids = list(CALCULATOR_IDS)
    context = caller_context(
        config,
        step,
        attempt_number=attempt_number,
        retry_feedback=retry_feedback,
        accepted_step_outputs=accepted_step_outputs,
    )
    return BatchRequest(
        schema_version=BATCH_SCHEMA_VERSION,
        batch_id=_batch_run_id(config.run_id, attempt_id),
        loops=(
            LoopSpec(
                loop_id=attempt_id,
                context=context,
                proposers=tuple(
                    proposer_worker(
                        config,
                        proposer_id,
                        blind_reference=bool(step.reviewer_reference_claim),
                    )
                    for proposer_id in calculator_ids
                ),
                reviewer=reviewer_worker(
                    config,
                    calculator_ids,
                    reviewer_reference_claim=step.reviewer_reference_claim,
                ),
                max_rounds=1,
                allow_early_stop=False,
                on_proposer_failure=ProposerFailurePolicy.FAIL_LOOP,
            ),
        ),
        failure_policy=BatchFailurePolicy.COLLECT,
    )


def _attempt_id(step_id: str, attempt_number: int) -> str:
    return _bounded_id(f"{step_id}_attempt_{attempt_number:03d}")


def _batch_run_id(config_run_id: str, attempt_id: str) -> str:
    return _bounded_id(f"calculate_{config_run_id}_{attempt_id}")


def _bounded_id(candidate: str) -> str:
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
    return f"{candidate[:115]}-{digest}"


__all__ = [
    "_attempt_batch_request",
    "_attempt_id",
    "_batch_run_id",
]
