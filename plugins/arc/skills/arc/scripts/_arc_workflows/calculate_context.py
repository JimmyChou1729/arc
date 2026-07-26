"""Scientific caller-context construction for ARC calculation batches."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from _arc_workflows.calculate_config import (
    CalculateConfig,
    CalculateStep,
    _integrity_reference,
)


_STEP_ACCEPTANCE_INSTRUCTIONS = {
    "new_derivation": (
        "Derive the target quantity from the allowed premises without assuming "
        "the target expression. State the derivation, checks, and validity scope."
    ),
    "check_known_result": (
        "Independently verify the stated known result under the declared "
        "conventions and scope. Classify agreement, equivalence after allowed "
        "transformations, or a concrete discrepancy."
    ),
    "formal_setup": (
        "Construct the requested formal object or controlled setup and state "
        "exactly what downstream reduction remains. Do not claim completion of "
        "a calculation beyond the declared formal endpoint."
    ),
}


def caller_context(
    config: CalculateConfig,
    step: CalculateStep,
    *,
    attempt_number: int,
    active_proposer_ids: list[str],
    locked_outputs: dict[str, Any],
    retry_feedback: list[dict[str, Any]],
    accepted_step_outputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the complete proposer-visible scientific packet without key filtering."""

    return {
        "step_id": step.step_id,
        "step_kind": step.kind,
        "step_prompt": step.prompt,
        "step_acceptance_instruction": _STEP_ACCEPTANCE_INSTRUCTIONS[step.kind],
        "allowed_context": copy.deepcopy(step.allowed_context),
        "attempt_number": attempt_number,
        "active_proposer_ids": list(active_proposer_ids),
        "locked_outputs": copy.deepcopy(locked_outputs),
        "retry_feedback": copy.deepcopy(retry_feedback),
        "accepted_prior_step_outputs": copy.deepcopy(dict(accepted_step_outputs)),
        "max_recalculations": config.max_recalculations,
        "integrity_reference": _integrity_reference(
            config.defaults.get("integrity_reference_path")
        ),
        "consensus_instruction": (
            "Work only on this calculation step. Respect "
            "accepted_prior_step_outputs and locked_outputs as already accepted "
            "unless explicitly asked to check them."
        ),
    }


__all__ = ["caller_context"]
