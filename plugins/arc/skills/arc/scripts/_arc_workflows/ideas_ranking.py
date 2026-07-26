"""Verified-round normalization and formal ranking for ARC ideas."""

from __future__ import annotations

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
    cross_qualification,
    is_cross_domain_context,
    loop_requires_idea_assessment,
    normalized_central_mechanism,
    scientific_run_status,
    single_domain_qualification,
)
from _arc_workflows.ideas_report import (
    cross_diagnostics,
    single_domain_diagnostics,
)

SELECTED_ROUNDS_SCHEMA = "arc.ideas.selected_rounds.v6"
PARTIAL_SELECTED_ROUNDS_SCHEMA = "arc.ideas.partial_selected_rounds.v2"


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
    cross_contexts = {
        loop_id: context
        for loop_id, context in contexts.items()
        if is_cross_domain_context(context)
    }
    cross_domain = bool(cross_contexts)
    single_contexts = (
        {}
        if cross_domain
        else {
            loop.loop_id: {
                **contexts[loop.loop_id],
                "requires_idea_assessment": loop_requires_idea_assessment(loop),
            }
            for loop in request.loops
        }
    )
    single_domain_qualification_enabled = any(
        context.get("requires_idea_assessment") is True
        for context in single_contexts.values()
    )
    single_domain_without_assessment = (
        bool(single_contexts) and not single_domain_qualification_enabled
    )
    selected: list[dict[str, Any]] = []
    unqualified: list[dict[str, Any]] = []
    excluded_loops: list[dict[str, str]] = []
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
                raise SystemExit(
                    "cannot rank ideas because a committed round is unavailable"
                ) from None
            loop_rounds.append(
                _round_entry(
                    loop,
                    committed,
                    scheme=scheme,
                    cross_context=loop_context if cross_domain else None,
                    single_context=(
                        single_contexts.get(loop.loop_id)
                        if single_contexts
                        else None
                    ),
                )
            )
        if not loop_rounds:
            excluded_loops.append(
                {
                    "loop_id": loop.loop_id,
                    "status": lifecycle,
                    "reason": "no_committed_rounds",
                }
            )
            continue
        partial_metadata = (
            _partial_loop_metadata(loop_inspection, len(loop_rounds))
            if mode == "partial"
            else {}
        )
        if cross_domain or single_domain_qualification_enabled:
            qualified_rounds = [
                entry for entry in loop_rounds if entry.get("qualified")
            ]
            if not qualified_rounds:
                best_failed = dict(max(loop_rounds, key=rank_key))
                best_failed["rounds"] = loop_rounds
                best_failed.update(partial_metadata)
                unqualified.append(best_failed)
                continue
            best = dict(max(qualified_rounds, key=rank_key))
        else:
            best = dict(max(loop_rounds, key=rank_key))
        best["rounds"] = loop_rounds
        best["loop_lifecycle"] = lifecycle
        best.update(partial_metadata)
        selected.append(best)

    if mode == "partial" and not selected and not unqualified:
        raise SystemExit(
            "cannot create a partial ideas report because no complete "
            "committed proposer-reviewer round is available"
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
            "WARNING: EXCLUDED NON-USABLE LOOPS — failed or incomplete loops "
            "were not ranked: "
            + ", ".join(
                f"{item['loop_id']} ({item['status']})"
                for item in excluded_loops
            )
        )
    top_three: list[dict[str, Any]] = []
    portfolio_excluded: list[dict[str, Any]] = []
    if cross_domain:
        mechanism_counts: dict[str, int] = {}
        portfolio_ranking: list[dict[str, Any]] = []
        for entry in ranking:
            mechanism = str(entry.get("normalized_central_mechanism", ""))
            if mechanism and mechanism_counts.get(mechanism, 0) >= 2:
                excluded = dict(entry)
                excluded["portfolio_exclusion_reason"] = (
                    "central_mechanism_cap_2"
                )
                portfolio_excluded.append(excluded)
                continue
            portfolio_ranking.append(entry)
            if mechanism:
                mechanism_counts[mechanism] = (
                    mechanism_counts.get(mechanism, 0) + 1
                )
        ranking = portfolio_ranking
        used_signatures: set[str] = set()
        for entry in ranking:
            signature = str(entry.get("normalized_transfer_signature", ""))
            if not signature or signature in used_signatures:
                continue
            top_three.append(entry)
            used_signatures.add(signature)
            if len(top_three) == 3:
                break
        if len(top_three) < 3:
            warnings.append(
                f"WARNING: only {len(top_three)} qualified, transfer-distinct "
                "cross-domain candidates are available; the top three were not "
                "padded with unqualified or duplicate candidates."
            )
        top_ids = {
            (entry["loop_id"], entry["round"]) for entry in top_three
        }
        ranking = [
            *top_three,
            *[
                entry
                for entry in ranking
                if (entry["loop_id"], entry["round"]) not in top_ids
            ],
        ]
    elif single_domain_qualification_enabled:
        top_three = ranking[:3]
        if len(top_three) < 3:
            warnings.append(
                f"WARNING: only {len(top_three)} qualified single-domain "
                "candidates are available; the top three were not padded with "
                "infeasible candidates."
            )
    elif single_domain_without_assessment:
        warnings.append(
            "WARNING: single-domain reviews do not contain idea_assessment; "
            "ranking used the no_assessment policy."
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
        "marking_scheme": _representative_marking_scheme(contexts),
        "ranking": ranking,
        "excluded_loops": excluded_loops,
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
    if cross_domain:
        payload.update(
            {
                "cross_domain": True,
                "top_three": top_three,
                "unqualified": unqualified,
                "portfolio_excluded": portfolio_excluded,
                "diagnostics": cross_diagnostics(
                    run_id,
                    ranking=ranking,
                    top_three=top_three,
                    unqualified=unqualified,
                    portfolio_excluded=portfolio_excluded,
                    warnings=warnings,
                ),
            }
        )
    elif single_domain_qualification_enabled:
        payload.update(
            {
                "single_domain_qualification": True,
                "top_three": top_three,
                "unqualified": unqualified,
                "diagnostics": single_domain_diagnostics(
                    run_id,
                    ranking=ranking,
                    top_three=top_three,
                    unqualified=unqualified,
                    warnings=warnings,
                ),
            }
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
    if not isinstance(marks, Mapping) or "total_score" not in marks:
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
    cross_context: Mapping[str, Any] | None = None,
    single_context: Mapping[str, Any] | None = None,
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
        raise SystemExit(
            f"committed round {committed.round_number} for {loop.loop_id} "
            "has no configured proposer output"
        )
    proposer_output = _json_object(committed.proposals[proposer_id])
    review = _json_object(committed.review)
    payload = review_payload(review)
    marks = normalized_review_marks(review, scheme)
    if marks is None:
        raise SystemExit(
            f"committed round {committed.round_number} for {loop.loop_id} "
            "has no valid reviewer marks"
        )
    title = proposer_output.get("title")
    if not isinstance(title, str) or not title.strip():
        raise SystemExit(
            f"committed round {committed.round_number} for {loop.loop_id} "
            "has no proposal title"
        )

    entry = {
        "loop_id": loop.loop_id,
        "round": committed.round_number,
        "title": title.strip(),
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
        "review_artifact": _safe_artifact_ref(committed.review_ref),
        "transcript_artifacts": [
            _safe_artifact_ref(ref) for ref in committed.transcript_refs
        ],
        "marking_scheme": dict(scheme),
    }
    if cross_context is not None:
        assessment = payload.get("cross_domain_assessment", {})
        qualified, reasons, signature, compatibility = cross_qualification(
            proposer_output,
            assessment,
            marks,
            cross_context=cross_context,
        )
        entry.update(
            {
                "qualified": qualified,
                "qualification_reasons": reasons,
                "cross_domain_assessment": (
                    assessment if isinstance(assessment, dict) else {}
                ),
                "compatibility_classification": compatibility,
                "normalized_transfer_signature": signature,
                "normalized_central_mechanism": normalized_central_mechanism(
                    assessment.get("transfer_signature")
                    if isinstance(assessment, Mapping)
                    else None
                ),
            }
        )
    elif single_context is not None:
        assessment = payload.get("idea_assessment")
        if (
            single_context.get("requires_idea_assessment") is True
            or isinstance(assessment, Mapping)
        ):
            qualified, reasons, feasibility = single_domain_qualification(
                assessment
            )
            entry.update(
                {
                    "qualified": qualified,
                    "qualification_policy": (
                        "single_domain_feasibility_gate"
                    ),
                    "qualification_reasons": reasons,
                    "idea_assessment": (
                        assessment if isinstance(assessment, dict) else {}
                    ),
                    "feasibility_classification": feasibility,
                }
            )
        else:
            entry["qualification_policy"] = "single_domain_no_assessment"
    return entry


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


def _json_object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    ]


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
