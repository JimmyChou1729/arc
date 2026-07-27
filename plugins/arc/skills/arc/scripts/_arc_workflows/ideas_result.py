"""Public observation and result materialization for ARC ideas batches."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from arc_jobs import RunRepository
from arc_proposer_reviewer import (
    BatchInspection,
    BatchRequest,
    BatchTrace,
    read_batch_round,
)
from arc_proposer_reviewer.protocol import encode_batch_request

from _arc_workflows.ideas_config import IdeasConfig
from _arc_workflows.ideas_marking import load_marking_scheme
from _arc_workflows.ideas_policy import scientific_run_status
from _arc_workflows.ideas_ranking import (
    normalized_review_marks,
    proposal_title,
)
from _arc_workflows.ideas_models import IdeaPlan


IDEAS_RESULT_SCHEMA = "arc.workflow.ideas.result.v5"


def observed_result(
    config: IdeasConfig,
    *,
    repository: RunRepository,
    request: BatchRequest,
    ideas: list[IdeaPlan],
    warnings: list[str],
    max_concurrent: int,
    inspection: BatchInspection,
    trace: BatchTrace | None,
    portfolio_assessment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        score_table = round_score_table(
            ideas,
            repository=repository,
            run_id=inspection.run_id,
            trace=trace,
        )
    except Exception as exc:
        warnings.append(
            "committed_round_unavailable: "
            f"{type(exc).__name__}"
        )
        trace = None
        score_table = round_score_table(
            ideas,
            repository=None,
            run_id=None,
            trace=None,
        )
    loop_by_id = {loop.loop_id: loop for loop in inspection.loops}
    committed_rounds = (
        {loop.loop_id: len(loop.rounds) for loop in trace.loops}
        if trace is not None
        else {}
    )
    loops = []
    for idea in ideas:
        observed = loop_by_id[idea.loop_id]
        loops.append(
            {
                "idea_id": idea.idea_id,
                "variant_id": idea.variant_id,
                "idea_index": idea.idea_index,
                "loop_id": idea.loop_id,
                "lifecycle": observed.lifecycle,
                "phase": observed.phase,
                "current_round": observed.current_round,
                "rounds_completed": observed.rounds_completed,
                "committed_rounds": committed_rounds.get(idea.loop_id, 0),
                "active_workers": _document(observed.active_workers),
                "last_activity_at": observed.last_activity_at,
                "pause": _document(observed.pause),
                "failure": _document(observed.failure),
                "integrity_error": observed.integrity_error,
            }
        )
    status = scientific_run_status(
        inspection.durable_lifecycle,
        (loop.lifecycle for loop in inspection.loops),
        trace_verified=trace is not None,
    )
    rankable_loop_count = (
        sum(loop.lifecycle == "succeeded" for loop in inspection.loops)
        if trace is not None
        else 0
    )
    if status == "degraded":
        _append_warning(
            warnings,
            "WARNING: IDEAS RUN DEGRADED — only succeeded loops are rankable",
        )
    elif status == "failed" and rankable_loop_count == 0:
        _append_warning(
            warnings,
            "WARNING: IDEAS RUN FAILED — no loop produced a rankable result",
        )
    failure_codes = _failure_codes(inspection)
    if failure_codes:
        _append_warning(
            warnings,
            "WARNING: LOOP FAILURES — "
            + ", ".join(
                f"{code}={count}"
                for code, count in sorted(failure_codes.items())
            ),
        )
    reviewer_call_count = sum(committed_rounds.values())
    return {
        "schema_version": IDEAS_RESULT_SCHEMA,
        "status": status,
        "run_id": config.run_id,
        "run_root": str(repository.root),
        "generation_mode": "model_selected_route",
        "domain_package_count": len(
            config.domain_manifest["domain_packages"]
        ),
        "domain_manifest_path": str(config.domain_manifest_path),
        "warnings": warnings,
        "proposal_count": len(ideas),
        "reviewer_call_count": reviewer_call_count,
        "max_concurrent_loops": max_concurrent,
        "batch_request_artifact_id": "proposer-reviewer/request",
        "batch": {
            "batch_id": request.batch_id,
            "durable_lifecycle": inspection.durable_lifecycle,
            "run_revision": inspection.run_revision,
            "loop_revisions": dict(inspection.loop_revisions),
            "trace_verified": trace is not None,
            "loop_lifecycle_counts": dict(inspection.lifecycle_counts),
            "rankable_loop_count": rankable_loop_count,
            "activity_integrity_error": (
                inspection.activity_integrity_error
            ),
        },
        "portfolio_assessment": _portfolio_assessment_summary(
            portfolio_assessment
        ),
        "loops": loops,
        "round_score_table": score_table,
    }


def dry_run_result(
    config: IdeasConfig,
    *,
    request: BatchRequest,
    ideas: list[IdeaPlan],
    warnings: list[str],
    max_concurrent: int,
    workspace_inputs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": IDEAS_RESULT_SCHEMA,
        "status": "dry_run",
        "run_id": config.run_id,
        "run_root": str(config.run_dir.resolve()),
        "generation_mode": "model_selected_route",
        "domain_package_count": len(
            config.domain_manifest["domain_packages"]
        ),
        "domain_manifest_path": str(config.domain_manifest_path),
        "warnings": warnings,
        "proposal_count": len(ideas),
        "reviewer_call_count": sum(
            loop.max_rounds for loop in request.loops
        ),
        "max_concurrent_loops": max_concurrent,
        "batch_request": encode_batch_request(request),
        "workspace_inputs": list(workspace_inputs or ()),
        "portfolio_assessment": _portfolio_assessment_summary(None),
        "loops": [
            {
                "idea_id": idea.idea_id,
                "variant_id": idea.variant_id,
                "idea_index": idea.idea_index,
                "loop_id": idea.loop_id,
                "lifecycle": "validated",
                "phase": "not_started",
                "current_round": 1,
                "rounds_completed": 0,
                "committed_rounds": 0,
                "active_workers": [],
                "last_activity_at": None,
                "pause": None,
                "failure": None,
                "integrity_error": None,
            }
            for idea in ideas
        ],
        "round_score_table": round_score_table(
            ideas,
            repository=None,
            run_id=None,
            trace=None,
        ),
    }


def not_started_result(
    config: IdeasConfig,
    *,
    request: BatchRequest,
    ideas: list[IdeaPlan],
    warnings: list[str],
    max_concurrent: int,
    status: str,
    workspace_inputs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    result = dry_run_result(
        config,
        request=request,
        ideas=ideas,
        warnings=warnings,
        max_concurrent=max_concurrent,
        workspace_inputs=workspace_inputs,
    )
    result["status"] = status
    result.pop("batch_request", None)
    result["reviewer_call_count"] = 0
    return result


def _failure_codes(inspection: BatchInspection) -> dict[str, int]:
    result: dict[str, int] = {}
    for loop in inspection.loops:
        failure = loop.failure
        if failure is None:
            continue
        code = str(failure.code)
        result[code] = result.get(code, 0) + 1
    return result


def _document(value: Any) -> Any:
    if value is None:
        return None
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, tuple):
        return [_document(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _document(item) for key, item in value.items()}
    return value


def _append_warning(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


def _portfolio_assessment_summary(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if value is None:
        return {
            "status": "not_run",
            "input_digest": None,
            "ref": None,
            "reused": False,
            "reason": "run_not_observed",
        }
    ref = value.get("ref")
    return {
        "status": str(value.get("status", "failed")),
        "input_digest": (
            str(value["input_digest"])
            if isinstance(value.get("input_digest"), str)
            else None
        ),
        "ref": dict(ref) if isinstance(ref, Mapping) else None,
        "reused": value.get("reused") is True,
        "reason": (
            str(value["reason"])
            if isinstance(value.get("reason"), str)
            else None
        ),
    }


def round_score_table(
    ideas: list[IdeaPlan],
    *,
    repository: RunRepository | None,
    run_id: str | None,
    trace: BatchTrace | None,
) -> dict[str, Any]:
    by_loop = (
        {} if trace is None else {loop.loop_id: loop for loop in trace.loops}
    )
    rows = [
        _round_score_row(
            idea,
            repository=repository,
            run_id=run_id,
            committed_round_count=(
                len(by_loop.get(idea.loop_id).rounds)
                if idea.loop_id in by_loop
                else 0
            ),
        )
        for idea in ideas
    ]
    max_round = max(
        (
            max(
                (int(key) for key in row["total_scores_by_round"]),
                default=0,
            )
            for row in rows
        ),
        default=0,
    )
    columns = [
        "Idea",
        "Group",
        "Final Title",
        *[
            f"R{round_number}"
            for round_number in range(1, max_round + 1)
        ],
        f"Δ R1→R{max_round}" if max_round else "Δ",
        "Best",
    ]
    return {
        "schema_version": "arc.workflow.ideas.round_score_table.v1",
        "source": "committed_trace",
        "columns": columns,
        "rows": rows,
        "markdown": _round_score_markdown(
            columns,
            rows,
            max_round=max_round,
        ),
    }


def _round_score_row(
    idea: IdeaPlan,
    *,
    repository: RunRepository | None,
    run_id: str | None,
    committed_round_count: int,
) -> dict[str, Any]:
    scheme = load_marking_scheme(idea.variant.marking_scheme)
    rounds: dict[int, dict[str, Any]] = {}
    titles: dict[int, str] = {}
    if repository is not None and run_id is not None:
        for round_number in range(1, committed_round_count + 1):
            committed = read_batch_round(
                repository,
                run_id,
                idea.loop_id,
                round_number,
            )
            title = proposal_title(committed.proposals)
            if title:
                titles[round_number] = title
            marks = normalized_review_marks(committed.review, scheme)
            if marks is not None:
                rounds[round_number] = marks
    total_scores = {
        round_number: marks["total_score"]
        for round_number, marks in rounds.items()
        if isinstance(marks.get("total_score"), (int, float))
    }
    first_round = min(total_scores, default=None)
    last_round = max(total_scores, default=None)
    delta_total = (
        total_scores[last_round] - total_scores[first_round]
        if first_round is not None and last_round is not None
        else None
    )
    return {
        "idea_id": idea.idea_id,
        "variant_id": idea.variant_id,
        "group": idea.variant_id,
        "loop_id": idea.loop_id,
        "final_title": titles[max(titles)] if titles else "",
        "rounds": [
            {"round": round_number, "marks": rounds[round_number]}
            for round_number in sorted(rounds)
        ],
        "total_scores_by_round": {
            str(key): value for key, value in sorted(total_scores.items())
        },
        "delta_total": delta_total,
        "best_total": max(total_scores.values(), default=None),
    }


def _round_score_markdown(
    columns: list[str],
    rows: list[dict[str, Any]],
    *,
    max_round: int,
) -> str:
    lines = [
        "| " + " | ".join(columns) + " |",
        "|"
        + "|".join(
            (
                "---:"
                if column.startswith("R")
                or column in {"Best"}
                or column.startswith("Δ")
                else "---"
            )
            for column in columns
        )
        + "|",
    ]
    for row in rows:
        total_scores = {
            int(key): value
            for key, value in row["total_scores_by_round"].items()
        }
        values = [
            row["loop_id"],
            row["group"],
            str(row.get("final_title", "")).replace("|", "/"),
            *[
                _format_score(total_scores.get(round_number))
                for round_number in range(1, max_round + 1)
            ],
            _format_delta(row.get("delta_total")),
            _format_score(row.get("best_total")),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def _format_score(value: Any) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else ""


def _format_delta(value: Any) -> str:
    return f"{value:+g}" if isinstance(value, (int, float)) else ""
