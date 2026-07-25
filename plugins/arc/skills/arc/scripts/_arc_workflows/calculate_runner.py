"""Attempt and outer-run orchestration for the ARC calculate workflow."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable, Mapping

from arc_jobs import RunEngine, RunRepository, RunSpec, RunStatus
from arc_llm import LLMTaskService
from arc_proposer_reviewer import (
    BatchRequest,
    CommittedRound,
    ProposerReviewerHandler,
    ProposerReviewerService,
    read_batch_round,
)
from arc_proposer_reviewer.protocol import encode_batch_request

from _arc_workflows.calculate_config import (
    CALCULATE_RESULT_SCHEMA,
    CalculateConfig,
    CalculateStep,
    _jsonable,
    _write_json,
    load_calculation_config,
)
from _arc_workflows.calculate_consensus import (
    _failed_step_result,
    _human_gate_blocked_step_result,
    _next_active_for_two_agree,
    _reference_disagrees_step_result,
    _retry_feedback_record,
    _review_consensus,
    _source_discrepancy_blocked_step_result,
    _valid_ids,
)
from _arc_workflows.calculate_prompts import (
    _attempt_batch_request,
    _attempt_id,
    _batch_run_id,
)


RETRYABLE_CONSENSUS_STATUSES = {"reference_disagrees", "two_agree", "all_disagree", "unresolved"}
BatchExecutor = Callable[[BatchRequest, Path, str], CommittedRound]


def run_calculation(
    config: CalculateConfig | Mapping[str, Any],
    *,
    batch_executor: BatchExecutor | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    calculation = config if isinstance(config, CalculateConfig) else load_calculation_config(config)
    run_root = calculation.run_dir / calculation.run_id
    if dry_run:
        return _dry_run_result(calculation, run_root)

    run_root.mkdir(parents=True, exist_ok=True)
    _write_json(run_root / "config.json", _jsonable(calculation))

    executor = batch_executor or _execute_public_batch
    step_results: list[dict[str, Any]] = []
    accepted_step_outputs: dict[str, Any] = {}
    overall_status = "completed"
    for step in calculation.steps:
        step_result = _run_calculation_step(
            calculation,
            step,
            executor=executor,
            run_root=run_root,
            accepted_step_outputs=accepted_step_outputs,
        )
        step_results.append(step_result)
        if step_result["status"] in {"blocked_for_user", "blocked_for_revision"}:
            overall_status = step_result["status"]
            break
        if step_result["status"] == "failed":
            overall_status = "failed"
            break
        if step_result["status"] == "accepted":
            accepted_step_outputs[step.step_id] = copy.deepcopy(step_result["accepted_output"])

    result = {
        "schema_version": CALCULATE_RESULT_SCHEMA,
        "status": overall_status,
        "run_id": calculation.run_id,
        "run_root": str(run_root),
        "proposer_count": calculation.proposer_count,
        "max_recalculations": calculation.max_recalculations,
        "human_gate": copy.deepcopy(calculation.human_gate),
        "steps": step_results,
        "warnings_summary": _aggregate_warnings_summary(step_results),
    }
    _write_json(run_root / "state.json", result)
    return result


def _run_calculation_step(
    config: CalculateConfig,
    step: CalculateStep,
    *,
    executor: BatchExecutor,
    run_root: Path,
    accepted_step_outputs: Mapping[str, Any],
) -> dict[str, Any]:
    all_proposer_ids = _proposer_ids(config.proposer_count)
    active_proposer_ids = list(all_proposer_ids)
    locked_outputs: dict[str, Any] = {}
    retry_feedback: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    max_attempts = config.max_recalculations + 1

    for attempt_number in range(1, max_attempts + 1):
        try:
            request = _attempt_batch_request(
                config,
                step,
                attempt_number=attempt_number,
                active_proposer_ids=active_proposer_ids,
                locked_outputs=locked_outputs,
                retry_feedback=retry_feedback,
                accepted_step_outputs=accepted_step_outputs,
            )
        except Exception as exc:
            return _failed_step_result(config, step, attempts=attempts, error=str(exc))
        attempt_id = _attempt_id(step.step_id, attempt_number)
        batch_run_id = _batch_run_id(config.run_id, attempt_id)
        try:
            committed_round = executor(request, run_root / "attempt-batches", batch_run_id)
        except Exception as exc:
            attempts.append(
                {
                    "attempt_number": attempt_number,
                    "active_proposer_ids": list(active_proposer_ids),
                    "batch_run_id": batch_run_id,
                    "batch_loop_id": attempt_id,
                    "error": str(exc),
                    "warnings_summary": _empty_warnings_summary(),
                }
            )
            return _failed_step_result(config, step, attempts=attempts, error=str(exc))
        attempt_record = {
            "attempt_number": attempt_number,
            "active_proposer_ids": list(active_proposer_ids),
            "batch_run_id": batch_run_id,
            "batch_loop_id": attempt_id,
            "warnings_summary": _empty_warnings_summary(),
        }

        try:
            review = _review_from_committed_round(committed_round)
            proposer_outputs = {
                proposer_id: output
                for proposer_id, output in committed_round.proposals.items()
                if proposer_id in active_proposer_ids
            }
            review_consensus = _review_consensus(
                review,
                active_proposer_ids=active_proposer_ids,
                selectable_proposer_ids=list(
                    dict.fromkeys([*active_proposer_ids, *[proposer_id for proposer_id in locked_outputs]])
                ),
                reviewer_reference_claim=step.reviewer_reference_claim,
            )
        except Exception as exc:
            attempt_record["error"] = str(exc)
            attempts.append(attempt_record)
            return _failed_step_result(config, step, attempts=attempts, error=str(exc))
        attempt_record["consensus"] = review_consensus
        attempts.append(attempt_record)

        status = str(review_consensus.get("status", "unresolved"))
        retryable_status = status in RETRYABLE_CONSENSUS_STATUSES
        retry_budget_available = attempt_number < max_attempts

        if status == "all_agree":
            source_discrepancy_block = _source_discrepancy_blocked_step_result(
                step,
                attempts=attempts,
                consensus=review_consensus,
            )
            if source_discrepancy_block is not None:
                return source_discrepancy_block
            accepted_result = review_consensus.get("accepted_result")
            return {
                "step_id": step.step_id,
                "kind": step.kind,
                "status": "accepted",
                "attempts": attempts,
                "accepted_output": accepted_result
                if accepted_result is not None
                else {"proposer_outputs": proposer_outputs},
                "blocked_output": None,
                "reviewer_consensus": review_consensus,
            }

        if retryable_status and retry_budget_available:
            retry_feedback.append(
                _retry_feedback_record(
                    review,
                    review_consensus,
                    attempt_number=attempt_number,
                    blind_reference=step.reviewer_reference_claim is not None,
                )
            )
            if status == "two_agree":
                next_active = _next_active_for_two_agree(review_consensus, all_proposer_ids)
                if next_active is not None:
                    agreed_ids = _valid_ids(review_consensus.get("agreed_proposer_ids", []), all_proposer_ids)
                    for proposer_id in agreed_ids:
                        if proposer_id in proposer_outputs:
                            locked_outputs[proposer_id] = proposer_outputs[proposer_id]
                    active_proposer_ids = next_active
                    continue

            active_proposer_ids = list(all_proposer_ids)
            locked_outputs = {}
            continue

        gated_block = _human_gate_blocked_step_result(
            config,
            step,
            attempts=attempts,
            consensus=review_consensus,
            trigger_status=status,
        )
        if gated_block is not None:
            return gated_block

        if status == "reference_disagrees":
            return _reference_disagrees_step_result(
                step,
                attempts=attempts,
                consensus=review_consensus,
            )

        if retryable_status and attempt_number >= max_attempts:
            return {
                "step_id": step.step_id,
                "kind": step.kind,
                "status": "blocked_for_user",
                "attempts": attempts,
                "accepted_output": None,
                "blocked_output": {
                    "analysis": str(review_consensus.get("analysis", "")),
                    "last_consensus": review_consensus,
                },
                "reviewer_consensus": review_consensus,
            }

        if status == "two_agree":
            next_active = _next_active_for_two_agree(review_consensus, all_proposer_ids)
            if next_active is not None:
                agreed_ids = _valid_ids(review_consensus.get("agreed_proposer_ids", []), all_proposer_ids)
                for proposer_id in agreed_ids:
                    if proposer_id in proposer_outputs:
                        locked_outputs[proposer_id] = proposer_outputs[proposer_id]
                active_proposer_ids = next_active
                continue

        active_proposer_ids = list(all_proposer_ids)
        locked_outputs = {}

    raise AssertionError("unreachable calculation loop exit")


def _empty_warnings_summary() -> dict[str, Any]:
    return {
        "structured_output_warning_count": 0,
        "structured_output_warnings_path": "",
        "cache_warning_count": 0,
        "cache_warnings_path": "",
    }


def _aggregate_warnings_summary(step_results: list[dict[str, Any]]) -> dict[str, Any]:
    structured_count = 0
    cache_count = 0
    structured_paths: list[str] = []
    cache_paths: list[str] = []
    for step in step_results:
        for attempt in step.get("attempts", []):
            if not isinstance(attempt, Mapping):
                continue
            summary = attempt.get("warnings_summary")
            if not isinstance(summary, Mapping):
                continue
            structured_count += int(summary.get("structured_output_warning_count") or 0)
            cache_count += int(summary.get("cache_warning_count") or 0)
            if path := str(summary.get("structured_output_warnings_path") or ""):
                structured_paths.append(path)
            if path := str(summary.get("cache_warnings_path") or ""):
                cache_paths.append(path)
    return {
        "structured_output_warning_count": structured_count,
        "structured_output_warnings_paths": sorted(set(structured_paths)),
        "cache_warning_count": cache_count,
        "cache_warnings_paths": sorted(set(cache_paths)),
    }


def _proposer_ids(count: int) -> list[str]:
    return [f"proposer_{index:03d}" for index in range(1, count + 1)]


def _execute_public_batch(
    request: BatchRequest, run_root: Path, run_id: str
) -> CommittedRound:
    """Execute one independent batch and expand only its committed first round."""

    repository = RunRepository(run_root)
    handler = ProposerReviewerHandler(ProposerReviewerService(LLMTaskService()))
    snapshot = RunEngine(repository).execute(
        RunSpec(run_id, handler.name, encode_batch_request(request)), handler
    )
    if snapshot.status is not RunStatus.SUCCEEDED:
        detail = ""
        if snapshot.error is not None:
            detail = f": {snapshot.error.message}"
        raise RuntimeError(f"calculation batch ended as {snapshot.status.value}{detail}")
    return read_batch_round(repository, run_id, request.loops[0].loop_id, 1)


def _review_from_committed_round(committed_round: CommittedRound) -> Mapping[str, Any]:
    if committed_round.round_number != 1:
        raise ValueError("calculation attempts may consume only their committed first round")
    if not isinstance(committed_round.review, Mapping):
        raise ValueError("committed reviewer output must be an object")
    return committed_round.review


def _dry_run_result(config: CalculateConfig, run_root: Path) -> dict[str, Any]:
    return {
        "schema_version": CALCULATE_RESULT_SCHEMA,
        "status": "dry_run",
        "run_id": config.run_id,
        "run_root": str(run_root),
        "proposer_count": config.proposer_count,
        "max_recalculations": config.max_recalculations,
        "human_gate": copy.deepcopy(config.human_gate),
        "steps": [{"step_id": step.step_id, "kind": step.kind} for step in config.steps],
    }


__all__ = [
    "BatchExecutor",
    "_execute_public_batch",
    "_review_from_committed_round",
    "_run_calculation_step",
    "run_calculation",
]
