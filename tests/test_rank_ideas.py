from __future__ import annotations

import importlib.util
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from arc_jobs import ResumeReason, RunEngine, RunRepository, RunSpec, RunStatus
from arc_llm import InvalidRequestError, LLMCompleted, LLMFailed, LLMPaused
from arc_proposer_reviewer import (
    BatchFailurePolicy,
    BatchRequest,
    LoopSpec,
    ProposerReviewerHandler,
    ProposerReviewerService,
    WorkerSpec,
)
from arc_proposer_reviewer.models import BATCH_SCHEMA_VERSION
from arc_proposer_reviewer.protocol import encode_batch_request


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/arc/skills/arc/scripts/rank-ideas.py"
PROPOSAL_SCHEMA = {"type": "object", "additionalProperties": True}


def _load_rank_module() -> Any:
    spec = importlib.util.spec_from_file_location("rank_ideas", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    old_dont_write_bytecode = sys.dont_write_bytecode
    old_path = list(sys.path)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        sys.path[:] = old_path
    return module


def _single_scheme() -> dict[str, Any]:
    marks = [
        ("user_intent_relevance", "Intent Relevance"),
        ("novelty", "Novelty"),
        ("confidence_of_novelty", "Confidence"),
        ("scientific_value", "Scientific Value"),
        ("planning", "Planning"),
        ("problem_well_definedness", "Well-definedness"),
    ]
    return {
        "schema_version": "arc.workflow.ideas.marking_scheme.v1",
        "marks": [
            {"field": field, "label": label, "minimum": 0, "maximum": 20}
            for field, label in marks
        ],
        "total_score": {"field": "total_score", "label": "Total"},
        "tie_break_order": ["total_score", "novelty"],
    }


def _taste_scheme() -> dict[str, Any]:
    path = (
        ROOT
        / "plugins"
        / "arc"
        / "skills"
        / "arc"
        / "workflows"
        / "json"
        / "ideas-marking-scheme.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_formal_normalization_rejects_missing_or_non_object_marks() -> None:
    old_dont_write_bytecode = sys.dont_write_bytecode
    old_path = list(sys.path)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        from _arc_workflows.ideas_ranking import normalized_review_marks
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        sys.path[:] = old_path

    assert normalized_review_marks({"payload": {}}, _single_scheme()) is None
    assert (
        normalized_review_marks(
            {"payload": {"marks": []}},
            _single_scheme(),
        )
        is None
    )
    missing_field = _single_marks(80)
    del missing_field["novelty"]
    assert (
        normalized_review_marks(
            {"payload": {"marks": missing_field}},
            _single_scheme(),
        )
        is None
    )
    untyped = {**_single_marks(80), "novelty": "12"}
    assert (
        normalized_review_marks(
            {"payload": {"marks": untyped}},
            _single_scheme(),
        )
        is None
    )
    non_finite = {**_single_marks(80), "novelty": float("nan")}
    assert (
        normalized_review_marks(
            {"payload": {"marks": non_finite}},
            _single_scheme(),
        )
        is None
    )


def test_taste_tie_breaks_prefer_novelty_then_simplicity_and_generality() -> None:
    _load_rank_module()
    marking_module = sys.modules["_arc_workflows.ideas_marking"]
    scheme = _taste_scheme()
    baseline = _taste_marks()

    more_novel = {**baseline, "novelty": 13, "simplicity": 7}
    simpler = {**baseline, "novelty": 12, "simplicity": 9}
    assert marking_module.rank_key_from_marks(
        more_novel,
        scheme=scheme,
    ) > marking_module.rank_key_from_marks(
        simpler,
        scheme=scheme,
    )

    simpler = {**baseline, "simplicity": 9, "generality": 7, "planning": 8}
    more_general = {
        **baseline,
        "simplicity": 8,
        "generality": 9,
        "planning": 7,
    }
    assert marking_module.rank_key_from_marks(
        simpler,
        scheme=scheme,
    ) > marking_module.rank_key_from_marks(
        more_general,
        scheme=scheme,
    )

    general = {**baseline, "generality": 9, "planning": 7}
    narrow = {**baseline, "generality": 7, "planning": 9}
    assert marking_module.rank_key_from_marks(
        general,
        scheme=scheme,
    ) > marking_module.rank_key_from_marks(
        narrow,
        scheme=scheme,
    )


def test_report_normalizes_latex_delimiters_without_mangling_commands() -> None:
    _load_rank_module()
    report_module = sys.modules["_arc_workflows.ideas_report"]

    rendered = report_module._math_markdown_text(
        r"Use \(\beta<2\gamma\) and \(\dot\pi_c\)."
    )

    assert rendered == r"Use $\beta<2\gamma$ and $\dot\pi_c$."
    assert r"\$" not in rendered


def test_scientific_readiness_uses_four_non_gating_states() -> None:
    _load_rank_module()
    policy = sys.modules["_arc_workflows.ideas_policy"]

    readiness, warnings, _classification = (
        policy.scientific_readiness(None)
    )
    assert readiness == "unassessed"
    assert warnings == ["missing_idea_assessment"]

    readiness, warnings, _classification = (
        policy.scientific_readiness(_single_assessment())
    )
    assert readiness == "ready"
    assert warnings == []

    readiness, warnings, _classification = (
        policy.scientific_readiness(
            _single_assessment(
                mathematical_well_definedness="partially_defined",
                manageable_risks=["A regulator dependence must be bounded."],
                external_method_status="uncertain",
            )
        )
    )
    assert readiness == "ready_with_risk"
    assert "mathematical_problem_is_partially_defined" in warnings
    assert (
        "manageable_feasibility_risk: "
        "A regulator dependence must be bounded."
    ) in warnings
    assert "external_method_status_is_uncertain" in warnings

    readiness, warnings, _classification = (
        policy.scientific_readiness(
            _single_assessment(
                blocking_failures=["The observable is not defined."],
            )
        )
    )
    assert readiness == "not_ready"
    assert (
        "blocking_feasibility_failure: The observable is not defined."
        in warnings
    )



@pytest.mark.parametrize(
    ("durable", "lifecycles", "trace_verified", "expected"),
    (
        ("paused", ("running",), True, "paused"),
        ("failed", ("succeeded",), True, "failed"),
        ("succeeded", ("succeeded", "succeeded"), True, "succeeded"),
        ("succeeded", ("succeeded", "failed"), True, "degraded"),
        ("succeeded", ("failed",), True, "failed"),
        ("succeeded", ("succeeded",), False, "failed"),
    ),
)
def test_scientific_status_is_separate_from_durable_lifecycle(
    durable: str,
    lifecycles: tuple[str, ...],
    trace_verified: bool,
    expected: str,
) -> None:
    old_dont_write_bytecode = sys.dont_write_bytecode
    old_path = list(sys.path)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        from _arc_workflows.ideas_policy import scientific_run_status
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        sys.path[:] = old_path

    assert (
        scientific_run_status(
            durable,
            lifecycles,
            trace_verified=trace_verified,
        )
        == expected
    )


def _single_marks(total: int) -> dict[str, int]:
    return {
        "user_intent_relevance": 15,
        "novelty": 12,
        "confidence_of_novelty": 12,
        "scientific_value": 15,
        "planning": 15,
        "problem_well_definedness": 15,
        "total_score": total,
    }


def _taste_marks() -> dict[str, int]:
    return {
        "user_intent_relevance": 8,
        "novelty": 12,
        "confidence_of_novelty": 12,
        "scientific_value": 13,
        "planning": 8,
        "problem_well_definedness": 13,
        "simplicity": 8,
        "generality": 8,
        "total_score": 82,
    }


def _reviewer_benchmark() -> dict[str, Any]:
    return {
        "same_direction_alternative": (
            "Compute the same invariant in the minimal controlled model."
        ),
        "preserves_proposer_direction": True,
        "comparison": (
            "The proposal has one coherent core and its machinery serves that claim."
        ),
    }


def _single_assessment(
    *,
    feasibility_status: str = "feasible",
    bounded_first_calculation_ready: bool = True,
    mathematical_well_definedness: str = "well_defined",
    blocking_failures: list[str] | None = None,
    manageable_risks: list[str] | None = None,
    external_method_status: str = "not_used",
) -> dict[str, Any]:
    return {
        "problem_importance": "substantive",
        "importance_rationale": "The result changes a concrete target-domain prediction.",
        "mathematical_well_definedness": mathematical_well_definedness,
        "feasibility_status": feasibility_status,
        "bounded_first_calculation_ready": bounded_first_calculation_ready,
        "blocking_feasibility_failures": blocking_failures or [],
        "manageable_feasibility_risks": manageable_risks or [],
        "external_method_status": external_method_status,
        "external_method_rationale": (
            "The optional transfer has not yet been checked."
            if external_method_status == "uncertain"
            else ""
        ),
    }


def _legacy_cross_assessment() -> dict[str, Any]:
    return {
        "source_field_id": "source-a",
        "target_field_id": "target-b",
        "transfer_status": "partial",
        "target_contribution_status": "substantial",
        "source_ingredient_validity": "valid",
        "target_adaptation_validity": "uncertain",
        "resulting_new_capability": "A target-domain consistency check.",
        "feasibility_status": "feasible_with_named_risk",
        "blocking_compatibility_failures": ["Match the boundary data."],
        "manageable_compatibility_risks": ["Control the regulator."],
        "novelty_coverage": {
            "source_domain": True,
            "target_domain": False,
            "intersection": True,
        },
        "critical_concerns": ["The map may be non-invertible."],
        "recommended_action": "refine_current",
    }


def _single_loop(
    loop_id: str,
    *,
    max_rounds: int = 2,
    requires_assessment: bool = True,
    scheme: Mapping[str, Any] | None = None,
) -> LoopSpec:
    context = {
        "user_intent": "Find a well-defined theoretical-physics research direction.",
        "variant_id": "general",
        "generation_mode": "model_selected_route",
        "marking_scheme": dict(scheme or _single_scheme()),
    }
    reviewer_schema = {
        "type": "object",
        "required": (
            ["marks", "idea_assessment"]
            if requires_assessment
            else ["marks"]
        ),
        "additionalProperties": True,
    }
    return LoopSpec(
        loop_id=loop_id,
        context=context,
        proposers=(WorkerSpec(f"{loop_id}-p", "Propose an idea.", PROPOSAL_SCHEMA),),
        reviewer=WorkerSpec(f"{loop_id}-r", "Review the idea.", reviewer_schema),
        max_rounds=max_rounds,
        allow_early_stop=False,
    )


class _ScriptedLLM:
    def __init__(
        self,
        *,
        proposals: Mapping[tuple[str, int], Mapping[str, Any]],
        reviews: Mapping[tuple[str, int], Mapping[str, Any]],
        failed_loops: set[str] | None = None,
    ) -> None:
        self.proposals = proposals
        self.reviews = reviews
        self.failed_loops = failed_loops or set()

    def execute(self, context, request, *, options):
        task = _round_task(request.prompt)
        loop_id = task["loop_id"]
        round_number = task["round"]
        if "one proposer" in request.prompt:
            if loop_id in self.failed_loops:
                return LLMFailed(InvalidRequestError("deliberate proposer failure"))
            return LLMCompleted(
                self.proposals[(loop_id, round_number)],
                "fake",
                "fake-model",
                None,
                None,
            )
        feedback_schema = request.output.schema["properties"]["feedback"]
        feedback = {
            worker_id: "Recompute from the published evidence."
            for worker_id in feedback_schema["required"]
        }
        return LLMCompleted(
            {
                "schema_version": "arc.proposer_reviewer.review.v1",
                "action": "stop",
                "reason": "The scripted assessment is complete.",
                "feedback": feedback,
                "payload": self.reviews[(loop_id, round_number)],
            },
            "fake",
            "fake-model",
            None,
            None,
        )


class _PauseAfterFirstRoundLLM(_ScriptedLLM):
    def execute(self, context, request, *, options):
        task = _round_task(request.prompt)
        if "one proposer" in request.prompt and task["round"] == 2:
            return LLMPaused(
                ResumeReason.EXTERNAL_CONDITION,
                "provider-wait",
                {"code": "provider_unavailable"},
            )
        return super().execute(context, request, options=options)


def _round_task(prompt: str) -> dict[str, Any]:
    return json.loads(prompt.rsplit("## Round task\n", 1)[1])


def _execute(
    root: Path,
    request: BatchRequest,
    llm: _ScriptedLLM,
    *,
    run_id: str = "ideas-run",
    expected_status: RunStatus = RunStatus.SUCCEEDED,
) -> RunRepository:
    repository = RunRepository(root)
    handler = ProposerReviewerHandler(ProposerReviewerService(llm))  # type: ignore[arg-type]
    snapshot = RunEngine(repository).execute(
        RunSpec(run_id, handler.name, encode_batch_request(request)), handler
    )
    assert snapshot.status is expected_status
    return repository


def _request(*loops: LoopSpec) -> BatchRequest:
    return BatchRequest(
        BATCH_SCHEMA_VERSION,
        "ideas-ranking",
        loops,
        BatchFailurePolicy.COLLECT,
    )


def _proposal(
    title: str,
    *,
    route: str = "Direct controlled calculation",
    package_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "scientific_route": {
            "description": route,
            "domain_package_ids_used": package_ids or ["domain-a"],
            "rationale": "This is the shortest sufficient formulation.",
        },
        "idea_summary": "A concrete, evidence-aware proposal.",
        "calculation_plan": "Evaluate the bounded leading-order calculation.",
    }


def test_ranker_uses_committed_review_payload_and_best_completed_round(
    tmp_path: Path,
) -> None:
    ranker = _load_rank_module()
    loop = _single_loop("idea-a")
    neighborhood_evidence = (
        "baseline arXiv:1503.08043; total_citer_count=792; "
        "scanned_count=792; scan_complete=true; matched arXiv:2401.00001 "
        "excluded because it studies a different observable"
    )
    neighborhood_queries = [
        "arc-paper get-citer-count 1503.08043",
        (
            "arc-paper search-citers 1503.08043 --term 'ultra slow roll' "
            "--term 'non-attractor' --scan-limit 1000 --limit 50"
        ),
    ]
    repository = _execute(
        tmp_path / "runs",
        _request(loop),
        _ScriptedLLM(
            proposals={
                ("idea-a", 1): _proposal("Best first-round idea"),
                ("idea-a", 2): _proposal("Lower-scoring final idea"),
            },
            reviews={
                ("idea-a", 1): {
                    "marks": _single_marks(91),
                    "idea_assessment": _single_assessment(),
                    "reviewer_benchmark": _reviewer_benchmark(),
                    "evidence_checked": [neighborhood_evidence],
                    "tool_queries_used": neighborhood_queries,
                },
                ("idea-a", 2): {
                    "marks": _single_marks(72),
                    "idea_assessment": _single_assessment(),
                },
            },
        ),
    )

    payload = ranker.rank_run(repository.root, "ideas-run")

    assert payload["schema_version"] == "arc.ideas.selected_rounds.v8"
    assert payload["status"] == "succeeded"
    assert payload["durable_lifecycle"] == "succeeded"
    assert "run_lifecycle" not in payload
    assert payload["ranking"][0]["title"] == "Best first-round idea"
    assert payload["ranking"][0]["round"] == 1
    assert [round_entry["round"] for round_entry in payload["ranking"][0]["rounds"]] == [1, 2]
    selected = payload["ranking"][0]
    assert selected["proposer_artifact"]["artifact_id"].startswith("proposer-reviewer/")
    assert len(selected["proposer_artifact"]["sha256"]) == 64
    assert len(selected["review_artifact"]["sha256"]) == 64
    assert selected["reviewer_benchmark"] == _reviewer_benchmark()
    assert selected["rounds"][0]["evidence_checked"] == [
        neighborhood_evidence
    ]
    assert selected["rounds"][0]["tool_queries_used"] == neighborhood_queries
    rendered = json.dumps(payload)
    assert "relative_path" not in rendered
    assert str(repository.run_directory("ideas-run")) not in rendered
    markdown = ranker.markdown_table(payload)
    assert "# Ideas" in markdown
    assert "Proposer artifact:" in markdown
    assert "Review artifact:" in markdown
    assert "#### Scientific Taste Review" in markdown
    assert "The proposal has one coherent core" in markdown
    assert "Compute the same invariant in the minimal controlled model." in markdown
    assert neighborhood_evidence in markdown
    assert all(query in markdown for query in neighborhood_queries)


def test_single_report_uses_embedded_scheme_for_new_and_legacy_columns(
    tmp_path: Path,
) -> None:
    ranker = _load_rank_module()
    taste_loop = _single_loop(
        "taste",
        max_rounds=1,
        scheme=_taste_scheme(),
    )
    taste_repository = _execute(
        tmp_path / "taste-runs",
        _request(taste_loop),
        _ScriptedLLM(
            proposals={("taste", 1): _proposal("Taste-aware idea")},
            reviews={
                ("taste", 1): {
                    "marks": _taste_marks(),
                    "idea_assessment": _single_assessment(),
                    "reviewer_benchmark": _reviewer_benchmark(),
                }
            },
        ),
    )

    taste_report = ranker.markdown_table(
        ranker.rank_run(taste_repository.root, "ideas-run")
    )
    assert "SI=simplicity, GE=generality" in taste_report
    assert "| Round | IR | N | CN | SV | PL | WD | SI | GE | T |" in taste_report

    legacy_loop = _single_loop("legacy", max_rounds=1)
    legacy_repository = _execute(
        tmp_path / "legacy-runs",
        _request(legacy_loop),
        _ScriptedLLM(
            proposals={("legacy", 1): _proposal("Legacy idea")},
            reviews={
                ("legacy", 1): {
                    "marks": _single_marks(80),
                    "idea_assessment": _single_assessment(),
                }
            },
        ),
    )

    legacy_report = ranker.markdown_table(
        ranker.rank_run(legacy_repository.root, "ideas-run")
    )
    assert "SI=simplicity" not in legacy_report
    assert "GE=generality" not in legacy_report
    assert "#### Scientific Taste Review" not in legacy_report


def test_mixed_routes_share_one_ranking_and_route_caveats_do_not_gate(
    tmp_path: Path,
) -> None:
    ranker = _load_rank_module()
    direct = _single_loop("direct", max_rounds=1)
    transfer = _single_loop("transfer", max_rounds=1)
    repository = _execute(
        tmp_path / "runs",
        _request(direct, transfer),
        _ScriptedLLM(
            proposals={
                ("direct", 1): _proposal("Direct higher score"),
                ("transfer", 1): _proposal(
                    "Transfer lower score",
                    route="Cross-domain transfer",
                    package_ids=["domain-a", "domain-b"],
                ),
            },
            reviews={
                ("direct", 1): {
                    "marks": _single_marks(92),
                    "idea_assessment": _single_assessment(
                        manageable_risks=[
                            "The direct approximation needs one bounded check."
                        ]
                    ),
                },
                ("transfer", 1): {
                    "marks": _single_marks(78),
                    "idea_assessment": _single_assessment(
                        external_method_status="uncertain",
                    ),
                },
            },
        ),
    )

    payload = ranker.rank_run(repository.root, "ideas-run")

    assert payload["schema_version"] == "arc.ideas.selected_rounds.v8"
    assert payload["generation_mode"] == "model_selected_route"
    assert [entry["title"] for entry in payload["ranking"]] == [
        "Direct higher score",
        "Transfer lower score",
    ]
    assert "unqualified" not in payload
    assert "portfolio_excluded" not in payload
    assert payload["ranking"][0]["scientific_route"]["description"] == (
        "Direct controlled calculation"
    )
    assert payload["ranking"][1]["scientific_route"][
        "domain_package_ids_used"
    ] == ["domain-a", "domain-b"]
    diagnostics = payload["diagnostics"]
    assert diagnostics["schema_version"] == "arc.ideas.diagnostics.v3"
    assert diagnostics["candidate_count"] == 2
    assert diagnostics["ready_with_risk_count"] == 2
    report = ranker.markdown_table(payload)
    assert "Scientific readiness: `ready_with_risk`" in report
    assert "Scientific route: Direct controlled calculation" in report
    assert "Scientific route: Cross-domain transfer" in report
    assert "Unqualified" not in report
    assert "Portfolio-Excluded" not in report


def test_single_scientific_not_ready_round_can_win_on_score(
    tmp_path: Path,
) -> None:
    ranker = _load_rank_module()
    loop = _single_loop("single-gate")
    repository = _execute(
        tmp_path / "runs",
        _request(loop),
        _ScriptedLLM(
            proposals={
                ("single-gate", 1): _proposal("High-score blocked idea"),
                ("single-gate", 2): _proposal("Feasible lower-score idea"),
            },
            reviews={
                ("single-gate", 1): {
                    "marks": _single_marks(96),
                    "idea_assessment": _single_assessment(
                        feasibility_status="infeasible",
                        bounded_first_calculation_ready=False,
                    ),
                },
                ("single-gate", 2): {
                    "marks": _single_marks(78),
                    "idea_assessment": _single_assessment(),
                },
            },
        ),
    )

    payload = ranker.rank_run(repository.root, "ideas-run")

    assert payload["schema_version"] == "arc.ideas.selected_rounds.v8"
    selected = payload["ranking"][0]
    assert selected["title"] == "High-score blocked idea"
    assert selected["scientific_readiness"] == "not_ready"
    assert "first_calculation_is_not_feasible" in selected[
        "scientific_warnings"
    ]
    assert "bounded_first_calculation_is_not_ready" in selected[
        "scientific_warnings"
    ]
    assert selected["rounds"][1]["scientific_readiness"] == "ready"
    assert "unqualified" not in payload
    assert payload["top_three"] == [selected]
    diagnostics = payload["diagnostics"]
    assert diagnostics["schema_version"] == "arc.ideas.diagnostics.v3"
    assert diagnostics["candidate_count"] == 1
    assert diagnostics["not_ready_count"] == 1
    assert diagnostics["candidates"][0]["feasibility_classification"][
        "bounded_first_calculation_ready"
    ] is False
    report = ranker.markdown_table(payload)
    assert "Scientific readiness: `not_ready`" in report
    assert "first_calculation_is_not_feasible" in report


def test_no_assessment_report_uses_canonical_ranking(tmp_path: Path) -> None:
    ranker = _load_rank_module()
    lower = _single_loop("lower", max_rounds=1, requires_assessment=False)
    higher = _single_loop("higher", max_rounds=1, requires_assessment=False)
    repository = _execute(
        tmp_path / "runs",
        _request(lower, higher),
        _ScriptedLLM(
            proposals={
                ("lower", 1): _proposal("Lower no-assessment idea"),
                ("higher", 1): _proposal("Higher no-assessment idea"),
            },
            reviews={
                ("lower", 1): {"marks": _single_marks(61)},
                ("higher", 1): {"marks": _single_marks(84)},
            },
        ),
    )

    payload = ranker.rank_run(repository.root, "ideas-run")

    assert [entry["title"] for entry in payload["ranking"]] == [
        "Higher no-assessment idea",
        "Lower no-assessment idea",
    ]
    assert [entry["title"] for entry in payload["top_three"]] == [
        "Higher no-assessment idea",
        "Lower no-assessment idea",
    ]
    assert all(
        entry["scientific_readiness"] == "unassessed"
        for entry in payload["ranking"]
    )
    assert all(
        entry["scientific_warnings"] == ["missing_idea_assessment"]
        for entry in payload["ranking"]
    )
    assert "summary_order" not in payload
    assert any(
        "legacy reviews do not contain idea_assessment" in warning
        for warning in payload["warnings"]
    )
    assert any(
        warning.startswith("WARNING: PORTFOLIO ASSESSMENT MISSING")
        for warning in payload["warnings"]
    )
    report = ranker.markdown_table(payload)
    assert "#### Focused Novelty Audit" in report
    assert "not an exhaustive proof of novelty" in report
    assert "Evidence checked:" in report
    assert "Tool queries used:" in report
    assert "Unresolved reviewer limitations:" in report


def test_legacy_cross_caveats_remain_visible_without_affecting_rank(
    tmp_path: Path,
) -> None:
    ranker = _load_rank_module()
    loop = _single_loop(
        "legacy-cross",
        max_rounds=1,
        requires_assessment=False,
    )
    repository = _execute(
        tmp_path / "runs",
        _request(loop),
        _ScriptedLLM(
            proposals={
                ("legacy-cross", 1): _proposal("Historical bridge idea")
            },
            reviews={
                ("legacy-cross", 1): {
                    "marks": _single_marks(83),
                    "cross_domain_assessment": _legacy_cross_assessment(),
                }
            },
        ),
    )

    payload = ranker.rank_run(repository.root, "ideas-run")

    selected = payload["ranking"][0]
    assert selected["marks"]["total_score"] == 83
    assert selected["scientific_readiness"] == "unassessed"
    context = selected["legacy_scientific_context"]
    assert context["source_domain"] == "source-a"
    assert context["target_domain"] == "target-b"
    assert context["novelty_coverage"]["target_domain"] is False
    assert payload["diagnostics"]["candidates"][0][
        "legacy_scientific_context"
    ] == context
    report = ranker.markdown_table(payload)
    assert "Historical cross-domain review context (advisory)" in report
    assert "Transfer status: partial" in report
    assert "target_domain=not checked" in report
    assert "Blocking compatibility failure: Match the boundary data." in report
    assert "Critical concern: The map may be non-invertible." in report


def test_portfolio_assessment_is_attached_and_rendered_without_reranking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ranker = _load_rank_module()
    lower = _single_loop("lower", max_rounds=1, requires_assessment=False)
    higher = _single_loop("higher", max_rounds=1, requires_assessment=False)
    repository = _execute(
        tmp_path / "runs",
        _request(lower, higher),
        _ScriptedLLM(
            proposals={
                ("lower", 1): _proposal("Lower idea"),
                ("higher", 1): _proposal("Higher idea"),
            },
            reviews={
                ("lower", 1): {"marks": _single_marks(60)},
                ("higher", 1): {"marks": _single_marks(90)},
            },
        ),
    )
    ranking_module = sys.modules["_arc_workflows.ideas_ranking"]
    monkeypatch.setattr(
        ranking_module,
        "load_portfolio_assessment",
        lambda _root, payload: {
            "status": "available",
            "input_digest": "digest",
            "ref": {"artifact_id": "portfolio"},
            "content": {
                "schema_version": (
                    "arc.ideas.portfolio_assessment.v1"
                ),
                "overall_assessment": (
                    "The simpler direct calculation is underweighted."
                ),
                "cross_candidate_findings": [
                    {
                        "topic": "minimality",
                        "finding": "Both ideas share one direct core.",
                        "candidate_ids": ["lower", "higher"],
                    }
                ],
                "candidate_notes": [
                    {
                        "candidate_id": "lower",
                        "note": "It has the shortest dependency chain.",
                    }
                ],
                "missing_or_underrepresented_directions": [
                    {
                        "direction": "Direct controlled baseline",
                        "rationale": "It isolates the common core.",
                        "minimal_first_calculation": (
                            "Compute the leading correlator directly."
                        ),
                        "assessment_status": (
                            "unranked_novelty_unassessed"
                        ),
                    }
                ],
                "research_strategy": ["Calculate the baseline first."],
                "limitations": ["No new literature search was performed."],
            },
        },
    )

    payload = ranker.rank_run(repository.root, "ideas-run")

    assert [entry["title"] for entry in payload["ranking"]] == [
        "Higher idea",
        "Lower idea",
    ]
    assert payload["portfolio_assessment"]["status"] == "available"
    report = ranker.markdown_table(payload)
    assert report.startswith("# Ideas\n")
    assert "## Global Scientific Assessment (Advisory)" in report
    assert "Direct controlled baseline" in report
    assert "Compute the leading correlator directly." in report
    assert report.index("## Global Scientific Assessment") < report.index(
        "Abbreviations:"
    )


def test_ranker_excludes_failed_and_incomplete_lifecycle_states(tmp_path: Path) -> None:
    ranker = _load_rank_module()
    usable = _single_loop("usable", max_rounds=1)
    failed = _single_loop("failed", max_rounds=1)
    repository = _execute(
        tmp_path / "completed-runs",
        _request(usable, failed),
        _ScriptedLLM(
            proposals={("usable", 1): _proposal("Usable idea")},
            reviews={
                ("usable", 1): {
                    "marks": _single_marks(80),
                    "idea_assessment": _single_assessment(),
                }
            },
            failed_loops={"failed"},
        ),
    )

    completed_payload = ranker.rank_run(repository.root, "ideas-run")
    assert completed_payload["schema_version"] == "arc.ideas.selected_rounds.v8"
    assert completed_payload["status"] == "degraded"
    assert completed_payload["durable_lifecycle"] == "succeeded"
    assert "run_lifecycle" not in completed_payload
    assert [entry["loop_id"] for entry in completed_payload["ranking"]] == ["usable"]
    assert completed_payload["excluded_loops"] == [
        {
            "loop_id": "failed",
            "status": "failed",
            "reason": "loop_lifecycle_failed",
        }
    ]

    pending_root = tmp_path / "pending-runs"
    pending_repository = RunRepository(pending_root)
    pending_repository.create(
        RunSpec(
            "pending-run",
            ProposerReviewerHandler.name,
            encode_batch_request(_request(_single_loop("pending", max_rounds=1))),
        )
    )
    pending_payload = ranker.rank_run(pending_root, "pending-run")
    assert pending_payload["status"] == "failed"
    assert pending_payload["durable_lifecycle"] == "pending"
    assert pending_payload["ranking"] == []
    assert pending_payload["excluded_loops"] == [
        {
            "loop_id": "pending",
            "status": "pending",
            "reason": "loop_is_incomplete",
        }
    ]
    pending_report = ranker.markdown_table(pending_payload)
    assert "SI=simplicity" not in pending_report
    assert "GE=generality" not in pending_report


def test_partial_ranker_uses_complete_rounds_from_paused_loops(
    tmp_path: Path,
) -> None:
    ranker = _load_rank_module()
    paused_loop = _single_loop("paused-loop")
    repository = _execute(
        tmp_path / "paused-runs",
        _request(paused_loop),
        _PauseAfterFirstRoundLLM(
            proposals={
                ("paused-loop", 1): _proposal("Partial idea"),
            },
            reviews={
                ("paused-loop", 1): {
                    "marks": _single_marks(82),
                    "idea_assessment": _single_assessment(),
                },
            },
        ),
        expected_status=RunStatus.PAUSED,
    )

    formal = ranker.rank_run(repository.root, "ideas-run")
    partial = ranker.rank_run(repository.root, "ideas-run", mode="partial")

    assert formal["ranking"] == []
    assert partial["schema_version"] == "arc.ideas.partial_selected_rounds.v4"
    assert partial["status"] == "provisional"
    assert partial["formal"] is False
    assert partial["provisional"] is True
    assert partial["ranking_kind"] == "non_formal_provisional"
    assert [entry["title"] for entry in partial["ranking"]] == [
        "Partial idea"
    ]
    selected = partial["ranking"][0]
    assert selected["provisional_rank"] == 1
    assert selected["loop_lifecycle"] == "paused"
    assert selected["committed_round_count"] == 1
    assert selected["pause_reason"] == (
        "external_condition:provider_unavailable"
    )

    blocked_repository = _execute(
        tmp_path / "blocked-paused-runs",
        _request(_single_loop("blocked-paused")),
        _PauseAfterFirstRoundLLM(
            proposals={
                ("blocked-paused", 1): _proposal("Blocked partial idea"),
            },
            reviews={
                ("blocked-paused", 1): {
                    "marks": _single_marks(94),
                    "idea_assessment": _single_assessment(
                        feasibility_status="infeasible",
                        bounded_first_calculation_ready=False,
                    ),
                },
            },
        ),
        expected_status=RunStatus.PAUSED,
    )
    blocked_partial = ranker.rank_run(
        blocked_repository.root,
        "ideas-run",
        mode="partial",
    )
    assert blocked_partial["ranking"][0]["title"] == "Blocked partial idea"
    assert blocked_partial["ranking"][0][
        "scientific_readiness"
    ] == "not_ready"
    assert "first_calculation_is_not_feasible" in blocked_partial["ranking"][
        0
    ][
        "scientific_warnings"
    ]
    assert "unqualified" not in blocked_partial
    report = ranker.markdown_table(partial)
    assert report.startswith(
        "# Partial Ideas — Non-Formal Provisional Report"
    )
    assert "Loop lifecycle: `paused`" in report
    assert "Complete committed rounds: `1`" in report


def test_partial_ranker_refuses_batch_without_complete_round(
    tmp_path: Path,
) -> None:
    ranker = _load_rank_module()
    repository = RunRepository(tmp_path / "pending-partial")
    repository.create(
        RunSpec(
            "pending-run",
            ProposerReviewerHandler.name,
            encode_batch_request(
                _request(_single_loop("pending", max_rounds=1))
            ),
        )
    )

    with pytest.raises(SystemExit, match="no complete valid committed"):
        ranker.rank_run(
            repository.root,
            "pending-run",
            mode="partial",
        )


@pytest.mark.parametrize(
    ("reader_name", "public_message"),
    (
        (
            "inspect_batch",
            "cannot rank ideas because batch inspection is unavailable",
        ),
        (
            "read_batch_trace",
            "cannot rank ideas because the committed proposer-reviewer trace "
            "is unavailable",
        ),
    ),
)
def test_ranker_sanitizes_public_read_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader_name: str,
    public_message: str,
) -> None:
    ranker = _load_rank_module()
    loop = _single_loop("safe-read", max_rounds=1)
    repository = _execute(
        tmp_path / "runs",
        _request(loop),
        _ScriptedLLM(
            proposals={("safe-read", 1): _proposal("Safe idea")},
            reviews={
                ("safe-read", 1): {
                    "marks": _single_marks(80),
                    "idea_assessment": _single_assessment(),
                }
            },
        ),
    )
    ranking_module = sys.modules["_arc_workflows.ideas_ranking"]

    def fail_read(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("/private/sensitive/research-path")

    monkeypatch.setattr(ranking_module, reader_name, fail_read)
    with pytest.raises(SystemExit) as caught:
        ranker.rank_run(repository.root, "ideas-run")

    assert str(caught.value) == public_message
    assert "/private/sensitive" not in str(caught.value)


def test_ranker_skips_unreadable_round_and_uses_best_valid_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ranker = _load_rank_module()
    loop = _single_loop("artifact-fallback")
    repository = _execute(
        tmp_path / "runs",
        _request(loop),
        _ScriptedLLM(
            proposals={
                ("artifact-fallback", 1): _proposal("Readable round"),
                ("artifact-fallback", 2): _proposal("Unreadable round"),
            },
            reviews={
                ("artifact-fallback", 1): {
                    "marks": _single_marks(70),
                    "idea_assessment": _single_assessment(),
                },
                ("artifact-fallback", 2): {
                    "marks": _single_marks(99),
                    "idea_assessment": _single_assessment(),
                },
            },
        ),
    )
    ranking_module = sys.modules["_arc_workflows.ideas_ranking"]
    original_read = ranking_module.read_batch_round

    def selectively_fail(
        repository: RunRepository,
        run_id: str,
        loop_id: str,
        round_number: int,
    ) -> Any:
        if round_number == 2:
            raise OSError("/private/sensitive/research-path")
        return original_read(repository, run_id, loop_id, round_number)

    monkeypatch.setattr(
        ranking_module,
        "read_batch_round",
        selectively_fail,
    )
    payload = ranker.rank_run(repository.root, "ideas-run")

    assert payload["ranking"][0]["title"] == "Readable round"
    assert payload["excluded_rounds"] == [
        {
            "loop_id": "artifact-fallback",
            "round": 2,
            "reason": "committed_round_artifact_unavailable",
        }
    ]
    assert "/private/sensitive" not in json.dumps(payload)


def test_ranker_skips_round_with_untyped_marks(tmp_path: Path) -> None:
    ranker = _load_rank_module()
    loop = _single_loop("typed-fallback")
    invalid_marks = {
        **_single_marks(99),
        "novelty": "not-a-number",
    }
    repository = _execute(
        tmp_path / "runs",
        _request(loop),
        _ScriptedLLM(
            proposals={
                ("typed-fallback", 1): _proposal("Typed marks"),
                ("typed-fallback", 2): _proposal("Untyped marks"),
            },
            reviews={
                ("typed-fallback", 1): {
                    "marks": _single_marks(70),
                    "idea_assessment": _single_assessment(),
                },
                ("typed-fallback", 2): {
                    "marks": invalid_marks,
                    "idea_assessment": _single_assessment(),
                },
            },
        ),
    )

    payload = ranker.rank_run(repository.root, "ideas-run")

    assert payload["ranking"][0]["title"] == "Typed marks"
    assert payload["excluded_rounds"] == [
        {
            "loop_id": "typed-fallback",
            "round": 2,
            "reason": "reviewer_marks_are_not_typed",
        }
    ]


def test_ranker_has_no_legacy_layout_reader_and_cli_uses_durable_identifiers(
    tmp_path: Path,
) -> None:
    ranker = _load_rank_module()
    source = inspect.getsource(ranker)
    for legacy_layout_token in (
        "state.json",
        "transcript.jsonl",
        "proposer_outputs",
        "review_path",
        ".iterdir(",
        ".glob(",
    ):
        assert legacy_layout_token not in source

    loop = _single_loop("cli", max_rounds=1)
    repository = _execute(
        tmp_path / "runs",
        _request(loop),
        _ScriptedLLM(
            proposals={("cli", 1): _proposal("CLI idea")},
            reviews={
                ("cli", 1): {
                    "marks": _single_marks(80),
                    "idea_assessment": _single_assessment(),
                }
            },
        ),
    )
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "ARC_REQUIRE_REPO_ROOT": str(ROOT),
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--run-root",
            str(repository.root),
            "--run-id",
            "ideas-run",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["ranking"][0]["title"] == "CLI idea"

    help_result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert help_result.returncode == 0
    assert "--run-root RUN_ROOT" in help_result.stdout
    assert "--run-id RUN_ID" in help_result.stdout
