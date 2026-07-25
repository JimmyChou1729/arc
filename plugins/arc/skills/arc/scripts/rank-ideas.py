#!/usr/bin/env python3
"""Rank the best scored round from each ARC ideas loop."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath

bootstrap_arc_pythonpath()

from arc_jobs import RunRepository
from arc_proposer_reviewer import (
    BatchRequest,
    BatchProjectionIntegrityError,
    LoopSpec,
    inspect_batch,
    read_batch_round,
    read_batch_trace,
)
from arc_proposer_reviewer.protocol import decode_batch_request

from _arc_workflows.ideas_marking import (
    normalized_marks,
    rank_key_from_marks,
    report_columns,
    score_fields,
)

CROSS_REPORT_COLUMNS = [
    ("IR", "user_intent_relevance"),
    ("TR", "cross_domain_transfer_quality"),
    ("TC", "substantive_target_contribution"),
    ("N", "novelty"),
    ("CN", "confidence_of_novelty"),
    ("SV", "scientific_value"),
    ("F", "calculation_feasibility"),
    ("WD", "problem_well_definedness"),
    ("T", "total_score"),
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Select each loop's highest-marked round and rank task-to-be-planned candidates."
    )
    parser.add_argument("--run-root", required=True, type=Path, help="durable ARC run repository root")
    parser.add_argument("--run-id", required=True, help="durable proposer-reviewer run ID")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    args = parser.parse_args()

    payload = rank_run(args.run_root, args.run_id)
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(markdown_table(payload))


def rank_run(run_root: Path, run_id: str) -> dict[str, Any]:
    """Rank verified committed rounds without exposing run-directory layout."""
    repository = RunRepository(run_root)
    request = _batch_request(repository, run_id)
    inspection = inspect_batch(repository, run_id)
    try:
        trace = read_batch_trace(repository, run_id)
    except BatchProjectionIntegrityError as exc:
        raise SystemExit(
            "cannot rank ideas because the committed proposer-reviewer trace failed integrity verification"
        ) from exc

    contexts = {loop.loop_id: _loop_context(loop) for loop in request.loops}
    cross_contexts = {
        loop_id: context
        for loop_id, context in contexts.items()
        if _is_cross_domain_context(context)
    }
    cross_domain = bool(cross_contexts)
    single_contexts = (
        {}
        if cross_domain
        else {
            loop.loop_id: {
                **contexts[loop.loop_id],
                "requires_idea_assessment": _loop_requires_idea_assessment(loop),
            }
            for loop in request.loops
        }
    )
    single_domain_qualification = any(
        context.get("requires_idea_assessment") is True for context in single_contexts.values()
    )
    single_domain_without_assessment = (
        bool(single_contexts) and not single_domain_qualification
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
        if lifecycle != "succeeded":
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
                    repository, run_id, loop.loop_id, round_ref.round_number
                )
            except BatchProjectionIntegrityError as exc:
                raise SystemExit(
                    "cannot rank ideas because a committed round failed integrity verification"
                ) from exc
            loop_rounds.append(
                _round_entry(
                    loop,
                    committed,
                    scheme=scheme,
                    cross_context=loop_context if cross_domain else None,
                    single_context=single_contexts.get(loop.loop_id) if single_contexts else None,
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
        if cross_domain or single_domain_qualification:
            qualified_rounds = [entry for entry in loop_rounds if entry.get("qualified")]
            if not qualified_rounds:
                best_failed = dict(max(loop_rounds, key=_rank_key))
                best_failed["rounds"] = loop_rounds
                unqualified.append(best_failed)
                continue
            best = dict(max(qualified_rounds, key=_rank_key))
        else:
            best = dict(max(loop_rounds, key=_rank_key))
        best["rounds"] = loop_rounds
        best["loop_lifecycle"] = lifecycle
        selected.append(best)

    ranking = sorted(selected, key=_rank_key, reverse=True)
    warnings: list[str] = []
    if excluded_loops:
        warnings.append(
            "WARNING: EXCLUDED NON-USABLE LOOPS — failed or incomplete loops were not ranked: "
            + ", ".join(f"{item['loop_id']} ({item['status']})" for item in excluded_loops)
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
                excluded["portfolio_exclusion_reason"] = "central_mechanism_cap_2"
                portfolio_excluded.append(excluded)
                continue
            portfolio_ranking.append(entry)
            if mechanism:
                mechanism_counts[mechanism] = mechanism_counts.get(mechanism, 0) + 1
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
                f"WARNING: only {len(top_three)} qualified, transfer-distinct cross-domain candidates are available; "
                "the top three were not padded with unqualified or duplicate candidates."
            )
        top_ids = {(entry["loop_id"], entry["round"]) for entry in top_three}
        ranking = [*top_three, *[entry for entry in ranking if (entry["loop_id"], entry["round"]) not in top_ids]]
    elif single_domain_qualification:
        top_three = ranking[:3]
        if len(top_three) < 3:
            warnings.append(
                f"WARNING: only {len(top_three)} qualified single-domain candidates are available; "
                "the top three were not padded with infeasible candidates."
            )
    elif single_domain_without_assessment:
        warnings.append(
            "WARNING: single-domain reviews do not contain idea_assessment; "
            "ranking used the no_assessment policy."
        )
    for index, entry in enumerate(ranking, start=1):
        entry["rank"] = index
    payload = {
        "schema_version": "arc.ideas.selected_rounds.v1",
        "run_id": run_id,
        "run_lifecycle": inspection.run_lifecycle,
        "run_revision": inspection.run_revision,
        "loop_revisions": dict(trace.loop_revisions),
        "user_intent": _run_user_intent(contexts),
        "summary_order": ranking,
        "ranking": ranking,
        "excluded_loops": excluded_loops,
        "warnings": warnings,
    }
    if cross_domain:
        payload.update(
            {
                "schema_version": "arc.ideas.selected_rounds.v2",
                "cross_domain": True,
                "top_three": top_three,
                "unqualified": unqualified,
                "portfolio_excluded": portfolio_excluded,
                "diagnostics": _cross_diagnostics(
                    run_id,
                    ranking=ranking,
                    top_three=top_three,
                    unqualified=unqualified,
                    portfolio_excluded=portfolio_excluded,
                    warnings=warnings,
                ),
            }
        )
    elif single_domain_qualification:
        payload.update(
            {
                "schema_version": "arc.ideas.selected_rounds.v3",
                "single_domain_qualification": True,
                "summary_order": ranking,
                "top_three": top_three,
                "unqualified": unqualified,
                "diagnostics": _single_domain_diagnostics(
                    run_id,
                    ranking=ranking,
                    top_three=top_three,
                    unqualified=unqualified,
                    warnings=warnings,
                ),
            }
        )
    return payload


def _round_entry(
    loop: LoopSpec,
    committed: Any,
    *,
    scheme: Mapping[str, Any],
    cross_context: Mapping[str, Any] | None = None,
    single_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    proposer_id = next(
        (worker.worker_id for worker in loop.proposers if worker.worker_id in committed.proposals),
        None,
    )
    if proposer_id is None:
        raise SystemExit(
            f"committed round {committed.round_number} for {loop.loop_id} has no configured proposer output"
        )
    proposer_output = _json_object(committed.proposals[proposer_id])
    review = _json_object(committed.review)
    review_payload = _json_object(review.get("payload"))
    marks = review_payload.get("marks", {})
    if "total_score" not in marks:
        marks = {field: 0 for field in score_fields(scheme)}
        marks["total_score"] = 0

    entry = {
        "loop_id": loop.loop_id,
        "round": committed.round_number,
        "title": str(proposer_output.get("title") or proposer_output.get("warning") or "Recovered / unstructured idea"),
        "marks": normalized_marks(marks, scheme),
        "proposer_output": proposer_output,
        "proposer_id": proposer_id,
        "proposer_artifact": _safe_artifact_ref(committed.proposal_refs[proposer_id]),
        "review_artifact": _safe_artifact_ref(committed.review_ref),
        "transcript_artifacts": [
            _safe_artifact_ref(ref) for ref in committed.transcript_refs
        ],
        "marking_scheme": dict(scheme),
    }
    if cross_context is not None:
        assessment = review_payload.get("cross_domain_assessment", {})
        qualified, reasons, signature, compatibility = _cross_qualification(
            proposer_output,
            assessment,
            entry["marks"],
            cross_context=cross_context,
        )
        entry.update(
            {
                "qualified": qualified,
                "qualification_reasons": reasons,
                "cross_domain_assessment": assessment if isinstance(assessment, dict) else {},
                "compatibility_classification": compatibility,
                "normalized_transfer_signature": signature,
                "normalized_central_mechanism": _normalized_central_mechanism(
                    assessment.get("transfer_signature") if isinstance(assessment, Mapping) else None
                ),
            }
        )
    elif single_context is not None:
        assessment = review_payload.get("idea_assessment")
        if single_context.get("requires_idea_assessment") is True or isinstance(assessment, Mapping):
            qualified, reasons, feasibility = _single_domain_qualification(assessment)
            entry.update(
                {
                    "qualified": qualified,
                    "qualification_policy": "single_domain_feasibility_gate_v1",
                    "qualification_reasons": reasons,
                    "idea_assessment": assessment if isinstance(assessment, dict) else {},
                    "feasibility_classification": feasibility,
                }
            )
        else:
            entry["qualification_policy"] = "single_domain_no_assessment"
    return entry


def _batch_request(repository: RunRepository, run_id: str) -> BatchRequest:
    try:
        return decode_batch_request(repository.read_spec(run_id).semantic_input)
    except Exception as exc:
        raise SystemExit(
            f"run {run_id!r} does not contain a valid proposer-reviewer BatchRequest"
        ) from exc


def _loop_context(loop: LoopSpec) -> dict[str, Any]:
    if not isinstance(loop.context, Mapping):
        raise SystemExit(f"loop {loop.loop_id!r} has no caller context")
    return dict(loop.context)


def _marking_scheme(context: Mapping[str, Any], loop_id: str) -> Mapping[str, Any]:
    scheme = context.get("marking_scheme")
    if not isinstance(scheme, Mapping):
        raise SystemExit(f"loop {loop_id!r} has no public marking_scheme in its caller context")
    try:
        score_fields(scheme)
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"loop {loop_id!r} has an invalid public marking_scheme") from exc
    return scheme


def _run_user_intent(contexts: Mapping[str, Mapping[str, Any]]) -> str:
    for context in contexts.values():
        intent = context.get("user_intent")
        if isinstance(intent, str) and intent.strip():
            return intent.strip()
    return ""


def _is_cross_domain_context(context: Mapping[str, Any]) -> bool:
    return (
        context.get("generation_mode") == "cross_domain"
        or context.get("variant_id") == "cross_domain"
    )


def _loop_requires_idea_assessment(loop: LoopSpec) -> bool:
    schema = loop.reviewer.output_schema
    if not isinstance(schema, Mapping):
        return False
    required = schema.get("required")
    return isinstance(required, list) and "idea_assessment" in required


def _exclusion_reason(loop_inspection: Any) -> str:
    if loop_inspection.lifecycle == "integrity_error":
        return "loop_integrity_error"
    if loop_inspection.lifecycle in {"pending", "running", "paused"}:
        return "loop_is_incomplete"
    return f"loop_lifecycle_{loop_inspection.lifecycle}"


def _json_object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_artifact_ref(ref: Any) -> dict[str, Any]:
    return {
        "artifact_id": ref.artifact_id,
        "sha256": ref.sha256,
        "size_bytes": ref.size_bytes,
        "media_type": ref.media_type,
    }


def _single_domain_qualification(
    assessment: Any,
) -> tuple[bool, list[str], dict[str, Any]]:
    reasons: list[str] = []
    if not isinstance(assessment, Mapping):
        return False, [*reasons, "missing_idea_assessment"], _empty_single_feasibility_classification()

    feasibility_status = str(assessment.get("feasibility_status", ""))
    well_definedness = str(assessment.get("mathematical_well_definedness", ""))
    external_method_status = str(assessment.get("external_method_status", ""))
    blocking_failures = _string_list(assessment.get("blocking_feasibility_failures"))
    manageable_risks = _string_list(assessment.get("manageable_feasibility_risks"))

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
            "bounded_first_calculation_ready": assessment.get("bounded_first_calculation_ready") is True,
            "blocking_failures": blocking_failures,
            "manageable_risks": manageable_risks,
            "external_method_status": external_method_status,
        },
    )


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


def _cross_qualification(
    proposer: Mapping[str, Any],
    assessment: Any,
    marks: Mapping[str, Any],
    *,
    cross_context: Mapping[str, Any],
) -> tuple[bool, list[str], str, dict[str, Any]]:
    reasons: list[str] = []
    if not isinstance(assessment, Mapping):
        return False, [*reasons, "missing_cross_domain_assessment"], "", _empty_compatibility_classification()

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
    if assessment.get("target_contribution_status") not in {"substantial", "transformative"}:
        reasons.append("target_contribution_must_be_substantial_or_transformative")
    if assessment.get("feasibility_status") not in {"feasible", "feasible_with_named_risk"}:
        reasons.append("first_calculation_is_not_feasible")
    compatibility = _compatibility_classification(assessment)
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
        novelty.get(scope) is True for scope in ("source_domain", "target_domain", "intersection")
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

    signature = _normalized_transfer_signature(assessment.get("transfer_signature"))
    if not signature:
        reasons.append("complete_transfer_signature_is_required")
    return not reasons, reasons, signature, compatibility


def _compatibility_classification(assessment: Mapping[str, Any]) -> dict[str, Any]:
    if "blocking_compatibility_failures" in assessment or "manageable_compatibility_risks" in assessment:
        return {
            "policy": "explicit_blocking_and_manageable_v2",
            "blocking_failures": _string_list(assessment.get("blocking_compatibility_failures")),
            "manageable_risks": _string_list(assessment.get("manageable_compatibility_risks")),
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


def _empty_compatibility_classification() -> dict[str, Any]:
    return {"policy": "missing_assessment", "blocking_failures": [], "manageable_risks": []}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalized_transfer_signature(raw: Any) -> str:
    if not isinstance(raw, Mapping):
        return ""
    fields = ("direction", "transferred_ingredient", "target_result", "first_calculation")
    values = [re.sub(r"\s+", " ", str(raw.get(field, "")).strip().lower()) for field in fields]
    if any(not value for value in values):
        return ""
    return " | ".join(values)


def _normalized_central_mechanism(raw: Any) -> str:
    if not isinstance(raw, Mapping):
        return ""
    values = [
        re.sub(r"\s+", " ", str(raw.get(field, "")).strip().lower())
        for field in ("direction", "transferred_ingredient")
    ]
    if any(not value for value in values):
        return ""
    return " | ".join(values)


def _cross_diagnostics(
    run_id: str,
    *,
    ranking: list[dict[str, Any]],
    top_three: list[dict[str, Any]],
    unqualified: list[dict[str, Any]],
    portfolio_excluded: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    top_keys = {(entry["loop_id"], entry["round"]) for entry in top_three}
    candidates = []
    for qualified, entries in ((True, ranking), (False, unqualified)):
        for entry in entries:
            candidates.append(
                {
                    "loop_id": entry["loop_id"],
                    "round": entry["round"],
                    "title": entry["title"],
                    "qualified": qualified,
                    "qualification_reasons": entry.get("qualification_reasons", []),
                    "compatibility_classification": entry.get("compatibility_classification", {}),
                    "transfer_signature": entry.get("normalized_transfer_signature", ""),
                    "central_mechanism": entry.get("normalized_central_mechanism", ""),
                    "top_three": (entry["loop_id"], entry["round"]) in top_keys,
                    "marks": entry["marks"],
                }
            )
    for entry in portfolio_excluded:
        candidates.append(
            {
                "loop_id": entry["loop_id"],
                "round": entry["round"],
                "title": entry["title"],
                "qualified": True,
                "portfolio_excluded": True,
                "portfolio_exclusion_reason": entry["portfolio_exclusion_reason"],
                "qualification_reasons": entry.get("qualification_reasons", []),
                "transfer_signature": entry.get("normalized_transfer_signature", ""),
                "central_mechanism": entry.get("normalized_central_mechanism", ""),
                "top_three": False,
                "marks": entry["marks"],
            }
        )
    return {
        "schema_version": "arc.ideas.cross_domain_diagnostics.v1",
        "run_id": run_id,
        "qualified_count": len(ranking),
        "unqualified_count": len(unqualified),
        "portfolio_excluded_count": len(portfolio_excluded),
        "top_three_count": len(top_three),
        "distinct_qualified_transfer_signatures": len(
            {entry.get("normalized_transfer_signature", "") for entry in ranking}
        ),
        "warnings": warnings,
        "candidates": candidates,
    }


def _single_domain_diagnostics(
    run_id: str,
    *,
    ranking: list[dict[str, Any]],
    top_three: list[dict[str, Any]],
    unqualified: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    top_keys = {(entry["loop_id"], entry["round"]) for entry in top_three}
    candidates = []
    for qualified, entries in ((True, ranking), (False, unqualified)):
        for entry in entries:
            assessment = entry.get("idea_assessment", {})
            candidates.append(
                {
                    "loop_id": entry["loop_id"],
                    "round": entry["round"],
                    "title": entry["title"],
                    "qualified": qualified,
                    "qualification_policy": entry.get("qualification_policy", ""),
                    "qualification_reasons": entry.get("qualification_reasons", []),
                    "problem_importance": (
                        assessment.get("problem_importance", "") if isinstance(assessment, Mapping) else ""
                    ),
                    "importance_rationale": (
                        assessment.get("importance_rationale", "") if isinstance(assessment, Mapping) else ""
                    ),
                    "feasibility_classification": entry.get("feasibility_classification", {}),
                    "top_three": (entry["loop_id"], entry["round"]) in top_keys,
                    "marks": entry["marks"],
                }
            )
    return {
        "schema_version": "arc.ideas.single_domain_diagnostics.v1",
        "run_id": run_id,
        "qualified_count": len(ranking),
        "unqualified_count": len(unqualified),
        "top_three_count": len(top_three),
        "warnings": warnings,
        "candidates": candidates,
    }


def _rank_key(entry: dict[str, Any]) -> tuple[float, ...]:
    return rank_key_from_marks(
        entry["marks"],
        round_number=entry["round"],
        scheme=entry["marking_scheme"],
    )


def markdown_table(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    if payload.get("warnings"):
        lines.extend(
            [
                "# Ranking Warnings",
                "",
                *[f"> {warning}" for warning in payload["warnings"]],
                "",
            ]
        )
    lines.extend([
        _summary_table(payload),
        "",
        "# Appendix: Idea Details",
    ])
    for entry in payload["ranking"]:
        lines.extend(["", *_appendix_section(entry)])
    if payload.get("cross_domain"):
        lines.extend(["", "# Appendix: Unqualified Cross-Domain Candidates"])
        if not payload.get("unqualified"):
            lines.extend(["", "None."])
        for entry in payload.get("unqualified", []):
            lines.extend(
                [
                    "",
                    f"## `{entry['loop_id']}` — {_heading_text(entry['title'])}",
                    "",
                    f"- Best observed round: `{entry['round']}`",
                    "- Qualification failures:",
                    *[f"  - {reason}" for reason in entry.get("qualification_reasons", [])],
                ]
            )
        lines.extend(["", "# Appendix: Portfolio-Excluded Cross-Domain Candidates"])
        if not payload.get("portfolio_excluded"):
            lines.extend(["", "None."])
        for entry in payload.get("portfolio_excluded", []):
            lines.extend(
                [
                    "",
                    f"## `{entry['loop_id']}` — {_heading_text(entry['title'])}",
                    "",
                    f"- Selected round: `{entry['round']}`",
                    f"- Exclusion: `{entry['portfolio_exclusion_reason']}`",
                ]
            )
    elif payload.get("single_domain_qualification"):
        lines.extend(["", "# Appendix: Unqualified Single-Domain Candidates"])
        if not payload.get("unqualified"):
            lines.extend(["", "None."])
        for entry in payload.get("unqualified", []):
            lines.extend(
                [
                    "",
                    f"## `{entry['loop_id']}` — {_heading_text(entry['title'])}",
                    "",
                    f"- Best observed round: `{entry['round']}`",
                    "- Qualification failures:",
                    *[f"  - {reason}" for reason in entry.get("qualification_reasons", [])],
                ]
            )
    return "\n".join(lines)


def _summary_table(payload: dict[str, Any]) -> str:
    if payload.get("cross_domain"):
        return _cross_summary_table(payload)
    lines = [
        "# Ideas",
        "",
        "Abbreviations:",
        "",
        "IR=intent relevance, N=novelty, CN=confidence of novelty, SV=scientific value, "
        "PL=planning, WD=well-definedness, T=total.",
    ]
    for warning in payload.get("warnings", []):
        lines.extend(["", str(warning)])
    for entry in payload.get("summary_order", payload.get("ranking", [])):
        lines.extend(["", *_round_marks_summary_section(entry)])
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _round_marks_summary_section(entry: dict[str, Any]) -> list[str]:
    return [
        f"## `{entry['loop_id']}`",
        "",
        _heading_text(entry["title"]),
        "",
        _compact_round_marks_table(entry),
    ]


def _compact_round_marks_table(entry: dict[str, Any]) -> str:
    columns = [
        ("IR", "user_intent_relevance"),
        ("N", "novelty"),
        ("CN", "confidence_of_novelty"),
        ("SV", "scientific_value"),
        ("PL", "planning"),
        ("WD", "problem_well_definedness"),
        ("T", "total_score"),
    ]
    lines = [
        "| Round | IR | N | CN | SV | PL | WD | T |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for round_entry in entry.get("rounds", []):
        marks = round_entry["marks"]
        mark_values = " | ".join(_format_mark(marks.get(field)) for _, field in columns)
        lines.append(f"| {round_entry['round']} | {mark_values} |")
    return "\n".join(lines)


def _cross_summary_table(payload: dict[str, Any]) -> str:
    lines = [
        "# Ideas",
        "",
        "Abbreviations:",
        "",
        "IR=intent relevance, TR=transfer quality, TC=target contribution, N=novelty, "
        "CN=confidence of novelty, SV=scientific value, F=feasibility, WD=well-definedness, T=total.",
    ]
    for warning in payload.get("warnings", []):
        lines.extend(["", str(warning)])
    for entry in payload.get("summary_order", payload.get("ranking", [])):
        lines.extend(["", *_round_marks_summary_section_cross(entry)])
    return "\n".join(lines)


def _round_marks_summary_section_cross(entry: dict[str, Any]) -> list[str]:
    return [
        f"## `{entry['loop_id']}`",
        "",
        _heading_text(entry["title"]),
        "",
        _compact_cross_marks_table(entry),
    ]


def _compact_cross_marks_table(entry: dict[str, Any]) -> str:
    headers = " | ".join(label for label, _field in CROSS_REPORT_COLUMNS)
    separators = "|".join("---:" for _ in CROSS_REPORT_COLUMNS)
    lines = [f"| Round | {headers} |", f"|---:|{separators}|"]
    for round_entry in entry.get("rounds", []):
        marks = round_entry["marks"]
        values = " | ".join(_format_mark(marks.get(field)) for _label, field in CROSS_REPORT_COLUMNS)
        lines.append(f"| {round_entry['round']} | {values} |")
    return "\n".join(lines)


def _appendix_section(entry: dict[str, Any]) -> list[str]:
    proposer_artifact = entry["proposer_artifact"]
    review_artifact = entry["review_artifact"]
    return [
        f"### {entry['rank']}. {_heading_text(entry['title'])}",
        "",
        f"- Loop: `{entry['loop_id']}`",
        f"- Selected round: `{entry['round']}`",
        (
            "- Proposer artifact: "
            f"`{proposer_artifact['artifact_id']}` "
            f"(sha256 `{proposer_artifact['sha256']}`)"
        ),
        (
            "- Review artifact: "
            f"`{review_artifact['artifact_id']}` "
            f"(sha256 `{review_artifact['sha256']}`)"
        ),
        "",
        "#### Referee Marks by Round",
        "",
        _round_marks_table(entry),
        "",
        "#### Full Idea Verbatim",
        "",
        _handoff_text(entry.get("proposer_output", {})),
    ]


def _round_marks_table(entry: dict[str, Any]) -> str:
    if "cross_domain_assessment" in entry:
        columns = [{"label": label, "field": field} for label, field in CROSS_REPORT_COLUMNS]
    else:
        columns = report_columns(entry["marking_scheme"])
    mark_headers = " | ".join(column["label"] for column in columns)
    mark_separator = "|".join("---:" for _ in columns)
    lines = [
        f"| Loop | Round | {mark_headers} |",
        f"|---|---:|{mark_separator}|",
    ]
    for round_entry in entry.get("rounds", []):
        marks = round_entry["marks"]
        mark_values = " | ".join(_format_mark(marks.get(column["field"])) for column in columns)
        lines.append(
            "| {loop_id} | {round} | {mark_values} |".format(
                loop_id=round_entry["loop_id"],
                round=round_entry["round"],
                mark_values=mark_values,
            )
        )
    return "\n".join(lines)


def _format_mark(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return ""


def _heading_text(value: Any) -> str:
    text = str(value).replace("\n", " ").strip()
    return text or "Untitled Idea"


def _handoff_text(value: Any) -> str:
    data = value if isinstance(value, dict) else {}
    fields = [
        ("Title", data.get("title", "")),
        ("Idea Summary", data.get("idea_summary", "")),
        ("Calculation Plan", data.get("calculation_plan", "")),
    ]
    lines: list[str] = []
    for label, item in fields:
        text = _math_markdown_text(str(item or "").strip())
        lines.append(f"{label}: {text}")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _math_markdown_text(text: str) -> str:
    text = re.sub(r"`([^`]+)`", _math_markdown_span, text)
    text = _display_math_lines(text)
    return _inline_raw_math_tokens(text)


def _math_markdown_span(match: re.Match[str]) -> str:
    content = match.group(1)
    if _looks_like_math(content):
        return f"${_format_math(content)}$"
    return match.group(0)


def _looks_like_math(text: str) -> bool:
    return bool(re.search(r"[=<>^_∫⟨⟩δΔκγρτλπℓεαβηθΦΣ{}|≈≤≥]", text))


def _inline_raw_math_tokens(text: str) -> str:
    parts = re.split(r"(\$\$.*?\$\$|\$.*?\$)", text, flags=re.DOTALL)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(
            r"(?<![\w$])([A-Za-z]+\^[A-Za-z0-9]+_[A-Za-z0-9+-]+)(?![\w])",
            lambda m: f"${_format_math(m.group(1))}$",
            parts[index],
        )
        parts[index] = re.sub(
            r"(?<![\w$])([A-Za-zαβγδεηθκλρτΦΣΔπℓ]+_[A-Za-z0-9+-]+)(?![\w])",
            lambda m: f"${_format_math(m.group(1))}$",
            parts[index],
        )
    return "".join(parts)


def _display_math_lines(text: str) -> str:
    lines: list[str] = []
    in_display_math = False
    for line in text.splitlines():
        stripped = line.strip().rstrip(",")
        if stripped == "$$":
            lines.append(line)
            in_display_math = not in_display_math
            continue
        if in_display_math:
            lines.append(line)
            continue
        math_span = re.fullmatch(r"\$(.+)\$", stripped)
        if math_span and _looks_like_display_equation(math_span.group(1)):
            lines.extend(["$$", math_span.group(1), "$$"])
        elif _looks_like_display_equation(stripped):
            lines.extend(["$$", _format_math(stripped), "$$"])
        else:
            lines.append(line)
    return "\n".join(lines)


def _looks_like_display_equation(text: str) -> bool:
    if not text or ":" in text[:24]:
        return False
    return bool(re.match(r"^([A-Za-zαβγδεηθκλρτΦΣΔπℓ]+[A-Za-z0-9_]*\(|∫|\\int)", text))


def _format_math(text: str) -> str:
    text = str(text).strip()
    text = re.sub(
        r"\b([A-Za-zαβγδεηθκλρτΦΣΔπℓ]+(?:\^[A-Za-z0-9]+)?)_([A-Za-z0-9+-]+)(?![\w])",
        lambda m: f"{m.group(1)}_{{{m.group(2)}}}",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


if __name__ == "__main__":
    main()
