"""Verified-round normalization and formal ranking for ARC ideas."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

from arc_jobs import RunRepository
from arc_proposer_reviewer import (
    BatchRequest,
    LoopSpec,
    inspect_batch,
    read_batch_round,
    read_batch_trace,
)
from arc_proposer_reviewer.protocol import decode_batch_request

from _arc_workflows.ideas_marking import (
    normalized_marks,
    rank_key_from_marks,
    score_fields,
)
from _arc_workflows.ideas_policy import (
    loop_requires_idea_assessment,
    scientific_run_status,
    scientific_readiness,
)
from _arc_workflows.ideas_portfolio_assessment import (
    load_portfolio_assessment,
)
from _arc_workflows.ideas_report import (
    ideas_diagnostics,
)

SELECTED_ROUNDS_SCHEMA = "arc.ideas.selected_rounds.v8"
PARTIAL_SELECTED_ROUNDS_SCHEMA = "arc.ideas.partial_selected_rounds.v4"


class _RoundExclusion(ValueError):
    """A committed round that cannot participate in score ranking."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def rank_run(
    run_root: Path,
    run_id: str,
    *,
    mode: str = "formal",
) -> dict[str, Any]:
    """Rank verified committed rounds without exposing run-directory layout."""
    if mode not in {"formal", "partial"}:
        raise ValueError("mode must be formal or partial")
    repository = RunRepository(run_root)
    request = _batch_request(repository, run_id)
    try:
        inspection = inspect_batch(repository, run_id)
    except Exception:
        raise SystemExit(
            "cannot rank ideas because batch inspection is unavailable"
        ) from None
    try:
        trace = read_batch_trace(repository, run_id)
    except Exception:
        raise SystemExit(
            "cannot rank ideas because the committed proposer-reviewer trace "
            "is unavailable"
        ) from None

    contexts = {loop.loop_id: _loop_context(loop) for loop in request.loops}
    assessment_contracts = {
        loop.loop_id: loop_requires_idea_assessment(loop)
        for loop in request.loops
    }
    selected: list[dict[str, Any]] = []
    excluded_loops: list[dict[str, str]] = []
    excluded_rounds: list[dict[str, Any]] = []
    trace_by_loop = {loop.loop_id: loop for loop in trace.loops}
    inspection_by_loop = {loop.loop_id: loop for loop in inspection.loops}
    for loop in request.loops:
        loop_inspection = inspection_by_loop[loop.loop_id]
        loop_trace = trace_by_loop[loop.loop_id]
        lifecycle = loop_inspection.lifecycle
        if mode == "formal" and lifecycle != "succeeded":
            excluded_loops.append(
                {
                    "loop_id": loop.loop_id,
                    "status": lifecycle,
                    "reason": _exclusion_reason(loop_inspection),
                }
            )
            continue
        loop_context = contexts[loop.loop_id]
        scheme = _marking_scheme(loop_context, loop.loop_id)
        loop_rounds = []
        for round_ref in loop_trace.rounds:
            try:
                committed = read_batch_round(
                    repository,
                    run_id,
                    loop.loop_id,
                    round_ref.round_number,
                )
            except Exception:
                excluded_rounds.append(
                    {
                        "loop_id": loop.loop_id,
                        "round": round_ref.round_number,
                        "reason": "committed_round_artifact_unavailable",
                    }
                )
                continue
            try:
                entry = _round_entry(
                    loop,
                    committed,
                    scheme=scheme,
                    assessment_required=assessment_contracts[loop.loop_id],
                )
            except _RoundExclusion as exc:
                excluded_rounds.append(
                    {
                        "loop_id": loop.loop_id,
                        "round": round_ref.round_number,
                        "reason": exc.reason,
                    }
                )
                continue
            loop_rounds.append(entry)
        if not loop_rounds:
            excluded_loops.append(
                {
                    "loop_id": loop.loop_id,
                    "status": lifecycle,
                    "reason": (
                        "no_valid_committed_rounds"
                        if loop_trace.rounds
                        else "no_committed_rounds"
                    ),
                }
            )
            continue
        partial_metadata = (
            _partial_loop_metadata(loop_inspection, len(loop_rounds))
            if mode == "partial"
            else {}
        )
        best = dict(max(loop_rounds, key=rank_key))
        best["rounds"] = loop_rounds
        best["loop_lifecycle"] = lifecycle
        best.update(partial_metadata)
        selected.append(best)

    if mode == "partial" and not selected:
        raise SystemExit(
            "cannot create a partial ideas report because no complete "
            "valid committed proposer-reviewer round is available"
        )
    ranking = sorted(selected, key=rank_key, reverse=True)
    warnings: list[str] = []
    evidence_incomplete = [
        entry["loop_id"]
        for entry in ranking
        if not entry.get("evidence_checked")
        or not entry.get("tool_queries_used")
    ]
    if evidence_incomplete:
        warnings.append(
            "WARNING: NOVELTY SCOUTING INCOMPLETE — reviewer evidence or query "
            "records are missing for: "
            + ", ".join(evidence_incomplete)
        )
    if excluded_loops and mode == "formal":
        warnings.append(
            "WARNING: EXCLUDED NON-USABLE LOOPS — failed, incomplete, or "
            "structurally invalid loops were not ranked: "
            + ", ".join(
                f"{item['loop_id']} ({item['status']}: {item['reason']})"
                for item in excluded_loops
            )
        )
    if excluded_rounds:
        warnings.append(
            "WARNING: EXCLUDED INVALID COMMITTED ROUNDS — unreadable artifacts "
            "or untyped reviewer marks were not ranked: "
            + ", ".join(
                f"{item['loop_id']} round {item['round']} ({item['reason']})"
                for item in excluded_rounds
            )
        )
    top_three = ranking[:3]
    if len(top_three) < 3:
        warnings.append(
            f"WARNING: only {len(top_three)} candidates with valid "
            "committed rounds are available."
        )
    legacy_without_assessment = [
        loop_id
        for loop_id, required in assessment_contracts.items()
        if not required
    ]
    if legacy_without_assessment:
        warnings.append(
            "WARNING: legacy reviews do not contain idea_assessment; "
            "ranking retained their marks with unassessed readiness: "
            + ", ".join(legacy_without_assessment)
        )
    for index, entry in enumerate(ranking, start=1):
        entry["rank"] = index
        if mode == "partial":
            entry["provisional_rank"] = index
    formal_status = scientific_run_status(
        inspection.durable_lifecycle,
        (loop.lifecycle for loop in inspection.loops),
        trace_verified=True,
    )
    payload = {
        "schema_version": (
            SELECTED_ROUNDS_SCHEMA
            if mode == "formal"
            else PARTIAL_SELECTED_ROUNDS_SCHEMA
        ),
        "run_id": run_id,
        "status": formal_status if mode == "formal" else "provisional",
        "durable_lifecycle": inspection.durable_lifecycle,
        "run_revision": inspection.run_revision,
        "loop_revisions": dict(trace.loop_revisions),
        "user_intent": _run_user_intent(contexts),
        "generation_mode": _generation_mode(contexts),
        "marking_scheme": _representative_marking_scheme(contexts),
        "ranking": ranking,
        "top_three": top_three,
        "excluded_loops": excluded_loops,
        "excluded_rounds": excluded_rounds,
        "warnings": warnings,
    }
    if mode == "partial":
        payload.update(
            {
                "mode": "partial",
                "formal": False,
                "provisional": True,
                "ranking_kind": "non_formal_provisional",
                "formal_status": formal_status,
                "notice": (
                    "NON-FORMAL PROVISIONAL REPORT: this ranking uses only "
                    "trace-verified complete committed rounds from an "
                    "incomplete or non-rankable batch."
                ),
            }
        )
        payload["portfolio_assessment"] = {
            "status": "not_applicable",
            "input_digest": None,
            "ref": None,
            "reason": "partial_ranking_uses_an_incomplete_frontier",
        }
    else:
        payload["portfolio_assessment"] = load_portfolio_assessment(
            run_root,
            payload,
        )
        assessment_status = str(
            payload["portfolio_assessment"].get("status", "missing")
        )
        if assessment_status != "available":
            reason = str(
                payload["portfolio_assessment"].get("reason", "") or ""
            ).strip()
            suffix = f" — {reason}" if reason else ""
            warnings.append(
                "WARNING: PORTFOLIO ASSESSMENT "
                f"{assessment_status.upper()}{suffix}"
            )
    payload["diagnostics"] = ideas_diagnostics(
        run_id,
        ranking=ranking,
        top_three=top_three,
        warnings=warnings,
    )
    return payload


def _partial_loop_metadata(
    loop_inspection: Any,
    committed_round_count: int,
) -> dict[str, Any]:
    pause_reasons: list[str] = []
    if loop_inspection.pause is not None:
        for entry in loop_inspection.pause.entries:
            reason = (
                f"{entry.reason}:{entry.code}"
                if entry.code
                else entry.reason
            )
            if reason not in pause_reasons:
                pause_reasons.append(reason)
    return {
        "loop_lifecycle": loop_inspection.lifecycle,
        "committed_round_count": committed_round_count,
        "pause_reason": pause_reasons[0] if pause_reasons else None,
        "pause_reasons": pause_reasons,
    }


def normalized_review_marks(
    review: Any,
    scheme: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Normalize one public review payload for quick and formal score views."""
    payload = review_payload(review)
    marks = payload.get("marks")
    fields = score_fields(scheme)
    if not isinstance(marks, Mapping) or any(
        not _is_finite_number(marks.get(field))
        for field in fields
    ):
        return None
    return normalized_marks(marks, scheme)


def review_payload(review: Any) -> dict[str, Any]:
    if not isinstance(review, Mapping):
        return {}
    payload = review.get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def proposal_title(proposals: Mapping[str, Any]) -> str:
    for proposal in proposals.values():
        if isinstance(proposal, Mapping):
            title = proposal.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    return ""


def rank_key(entry: dict[str, Any]) -> tuple[float, ...]:
    return rank_key_from_marks(
        entry["marks"],
        round_number=entry["round"],
        scheme=entry["marking_scheme"],
    )


def _round_entry(
    loop: LoopSpec,
    committed: Any,
    *,
    scheme: Mapping[str, Any],
    assessment_required: bool,
) -> dict[str, Any]:
    proposer_id = next(
        (
            worker.worker_id
            for worker in loop.proposers
            if worker.worker_id in committed.proposals
        ),
        None,
    )
    if proposer_id is None:
        raise _RoundExclusion("configured_proposer_artifact_is_missing")
    raw_proposer_output = committed.proposals[proposer_id]
    if not isinstance(raw_proposer_output, Mapping):
        raise _RoundExclusion("invalid_proposer_artifact")
    proposer_output = dict(raw_proposer_output)
    if not isinstance(committed.review, Mapping):
        raise _RoundExclusion("invalid_review_artifact")
    review = dict(committed.review)
    payload = review_payload(review)
    marks = normalized_review_marks(review, scheme)
    if marks is None:
        raise _RoundExclusion("reviewer_marks_are_not_typed")
    title = proposer_output.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _RoundExclusion("proposal_title_is_missing")

    try:
        entry = {
            "loop_id": loop.loop_id,
            "round": committed.round_number,
            "title": title.strip(),
            "scientific_route": _scientific_route(proposer_output),
            "marks": marks,
            "evidence_checked": _string_list(payload.get("evidence_checked")),
            "tool_queries_used": _string_list(payload.get("tool_queries_used")),
            "reviewer_limitations": _reviewer_limitations(payload),
            "reviewer_benchmark": _reviewer_benchmark(payload),
            "proposer_output": proposer_output,
            "proposer_id": proposer_id,
            "proposer_artifact": _safe_artifact_ref(
                committed.proposal_refs[proposer_id]
            ),
            "proposal_artifacts": {
                worker_id: _safe_artifact_ref(ref)
                for worker_id, ref in sorted(
                    committed.proposal_refs.items()
                )
            },
            "review_artifact": _safe_artifact_ref(committed.review_ref),
            "transcript_artifacts": [
                _safe_artifact_ref(ref) for ref in committed.transcript_refs
            ],
            "marking_scheme": dict(scheme),
        }
    except (AttributeError, KeyError, TypeError):
        raise _RoundExclusion("invalid_artifact_reference") from None
    assessment = payload.get("idea_assessment")
    readiness, warnings, feasibility = scientific_readiness(assessment)
    legacy_assessment = payload.get("cross_domain_assessment")
    legacy_scientific_context = _legacy_scientific_context(
        legacy_assessment
    )
    entry.update(
        {
            "scientific_readiness": readiness,
            "scientific_warnings": warnings,
            "scientific_readiness_policy": (
                "reviewer_assessment_diagnostic"
                if assessment_required or isinstance(assessment, Mapping)
                else "legacy_no_common_assessment"
            ),
            "idea_assessment": (
                dict(assessment) if isinstance(assessment, Mapping) else {}
            ),
            "legacy_scientific_context": legacy_scientific_context,
            "feasibility_classification": feasibility,
        }
    )
    return entry


def _legacy_scientific_context(raw: Any) -> dict[str, Any]:
    """Preserve old cross-domain caveats without reviving route gates."""
    if not isinstance(raw, Mapping):
        return {}
    novelty = raw.get("novelty_coverage")
    return {
        "source_domain": str(raw.get("source_field_id", "")).strip(),
        "target_domain": str(raw.get("target_field_id", "")).strip(),
        "transfer_status": str(raw.get("transfer_status", "")).strip(),
        "target_contribution_status": str(
            raw.get("target_contribution_status", "")
        ).strip(),
        "source_ingredient_validity": str(
            raw.get("source_ingredient_validity", "")
        ).strip(),
        "target_adaptation_validity": str(
            raw.get("target_adaptation_validity", "")
        ).strip(),
        "feasibility_status": str(
            raw.get("feasibility_status", "")
        ).strip(),
        "resulting_new_capability": str(
            raw.get("resulting_new_capability", "")
        ).strip(),
        "blocking_compatibility_failures": _string_list(
            raw.get("blocking_compatibility_failures")
        ),
        "manageable_compatibility_risks": _string_list(
            raw.get("manageable_compatibility_risks")
        ),
        "novelty_coverage": (
            {
                scope: novelty.get(scope)
                for scope in (
                    "source_domain",
                    "target_domain",
                    "intersection",
                )
                if isinstance(novelty.get(scope), bool)
            }
            if isinstance(novelty, Mapping)
            else {}
        ),
        "critical_concerns": _string_list(
            raw.get("critical_concerns")
            if raw.get("critical_concerns") is not None
            else raw.get("disqualifying_reasons")
        ),
        "recommended_action": str(
            raw.get("recommended_action", "")
        ).strip(),
    }


def _scientific_route(
    proposer_output: Mapping[str, Any],
) -> dict[str, Any]:
    raw = proposer_output.get("scientific_route")
    if not isinstance(raw, Mapping):
        return {}
    description = raw.get("description")
    rationale = raw.get("rationale")
    package_ids = raw.get("domain_package_ids_used")
    return {
        "description": (
            description.strip()
            if isinstance(description, str)
            else ""
        ),
        "domain_package_ids_used": _string_list(package_ids),
        "rationale": (
            rationale.strip()
            if isinstance(rationale, str)
            else ""
        ),
    }


def _reviewer_benchmark(payload: Mapping[str, Any]) -> dict[str, Any]:
    benchmark = payload.get("reviewer_benchmark")
    if not isinstance(benchmark, Mapping):
        return {}
    comparison = benchmark.get("comparison")
    alternative = benchmark.get("same_direction_alternative")
    preserves_direction = benchmark.get("preserves_proposer_direction")
    return {
        "comparison": (
            comparison.strip()
            if isinstance(comparison, str)
            else ""
        ),
        "same_direction_alternative": (
            alternative.strip()
            if isinstance(alternative, str)
            else ""
        ),
        "preserves_proposer_direction": (
            preserves_direction
            if isinstance(preserves_direction, bool)
            else None
        ),
    }


def _batch_request(
    repository: RunRepository,
    run_id: str,
) -> BatchRequest:
    try:
        return decode_batch_request(repository.read_spec(run_id).semantic_input)
    except Exception as exc:
        raise SystemExit(
            f"run {run_id!r} does not contain a valid proposer-reviewer "
            "BatchRequest"
        ) from exc


def _loop_context(loop: LoopSpec) -> dict[str, Any]:
    if not isinstance(loop.context, Mapping):
        raise SystemExit(f"loop {loop.loop_id!r} has no caller context")
    return dict(loop.context)


def _marking_scheme(
    context: Mapping[str, Any],
    loop_id: str,
) -> Mapping[str, Any]:
    scheme = context.get("marking_scheme")
    if not isinstance(scheme, Mapping):
        raise SystemExit(
            f"loop {loop_id!r} has no public marking_scheme in its caller context"
        )
    try:
        score_fields(scheme)
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(
            f"loop {loop_id!r} has an invalid public marking_scheme"
        ) from exc
    return scheme


def _run_user_intent(
    contexts: Mapping[str, Mapping[str, Any]],
) -> str:
    for context in contexts.values():
        intent = context.get("user_intent")
        if isinstance(intent, str) and intent.strip():
            return intent.strip()
    return ""


def _generation_mode(
    contexts: Mapping[str, Mapping[str, Any]],
) -> str:
    modes = {
        str(context.get("generation_mode", "")).strip()
        for context in contexts.values()
        if str(context.get("generation_mode", "")).strip()
    }
    if modes == {"model_selected_route"}:
        return "model_selected_route"
    if len(modes) == 1:
        return next(iter(modes))
    return "legacy_or_mixed"


def _representative_marking_scheme(
    contexts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    for context in contexts.values():
        scheme = context.get("marking_scheme")
        if isinstance(scheme, Mapping):
            return dict(scheme)
    return {}


def _exclusion_reason(loop_inspection: Any) -> str:
    if loop_inspection.lifecycle == "integrity_error":
        return "loop_integrity_error"
    if loop_inspection.lifecycle in {"pending", "running", "paused"}:
        return "loop_is_incomplete"
    return f"loop_lifecycle_{loop_inspection.lifecycle}"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    ]


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _reviewer_limitations(payload: Mapping[str, Any]) -> list[str]:
    limitations: list[str] = []
    for assessment_name, fields in (
        (
            "idea_assessment",
            (
                "blocking_feasibility_failures",
                "manageable_feasibility_risks",
            ),
        ),
        (
            "cross_domain_assessment",
            (
                "blocking_compatibility_failures",
                "manageable_compatibility_risks",
                "critical_concerns",
                "disqualifying_reasons",
            ),
        ),
    ):
        assessment = payload.get(assessment_name)
        if not isinstance(assessment, Mapping):
            continue
        for field in fields:
            limitations.extend(_string_list(assessment.get(field)))
    return list(dict.fromkeys(limitations))


def _safe_artifact_ref(ref: Any) -> dict[str, Any]:
    return {
        "artifact_id": ref.artifact_id,
        "sha256": ref.sha256,
        "size_bytes": ref.size_bytes,
        "media_type": ref.media_type,
    }
