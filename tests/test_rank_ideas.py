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
        / "ideas-domain-marking-scheme.json"
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


def test_compatibility_classification_requires_current_fields() -> None:
    old_dont_write_bytecode = sys.dont_write_bytecode
    old_path = list(sys.path)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        from _arc_workflows.ideas_policy import (
            compatibility_classification,
        )
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        sys.path[:] = old_path

    with pytest.raises(
        ValueError,
        match="requires blocking_compatibility_failures",
    ):
        compatibility_classification(
            {
                "compatibility_failures": ["old field"],
                "feasibility_status": "feasible",
            }
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


def _cross_scheme() -> dict[str, Any]:
    marks = [
        ("user_intent_relevance", "Intent Relevance"),
        ("cross_domain_transfer_quality", "Transfer"),
        ("substantive_target_contribution", "Target Contribution"),
        ("novelty", "Novelty"),
        ("confidence_of_novelty", "Confidence"),
        ("scientific_value", "Scientific Value"),
        ("calculation_feasibility", "Feasibility"),
        ("problem_well_definedness", "Well-definedness"),
    ]
    return {
        "schema_version": "arc.workflow.ideas.marking_scheme.v1",
        "marks": [
            {"field": field, "label": label, "minimum": 0, "maximum": 20}
            for field, label in marks
        ],
        "total_score": {"field": "total_score", "label": "Total"},
        "tie_break_order": ["total_score", "substantive_target_contribution"],
    }


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
) -> dict[str, Any]:
    return {
        "problem_importance": "substantive",
        "importance_rationale": "The result changes a concrete target-domain prediction.",
        "mathematical_well_definedness": "well_defined",
        "feasibility_status": feasibility_status,
        "bounded_first_calculation_ready": bounded_first_calculation_ready,
        "blocking_feasibility_failures": [],
        "manageable_feasibility_risks": [],
        "external_method_status": "not_used",
    }


def _cross_marks(total: int) -> dict[str, int]:
    return {
        "user_intent_relevance": 12,
        "cross_domain_transfer_quality": 12,
        "substantive_target_contribution": 16,
        "novelty": 8,
        "confidence_of_novelty": 7,
        "scientific_value": 8,
        "calculation_feasibility": 7,
        "problem_well_definedness": 7,
        "total_score": total,
    }


def _cross_assessment(*, transfer_status: str = "genuine") -> dict[str, Any]:
    return {
        "source_field_id": "field-a",
        "target_field_id": "field-b",
        "transfer_status": transfer_status,
        "target_contribution_status": "substantial",
        "source_ingredient_validity": "valid",
        "target_adaptation_validity": "valid",
        "feasibility_status": "feasible",
        "blocking_compatibility_failures": [],
        "manageable_compatibility_risks": [],
        "novelty_coverage": {
            "source_domain": True,
            "target_domain": True,
            "intersection": True,
        },
        "disqualifying_reasons": [],
        "transfer_signature": {
            "direction": "field-a to field-b",
            "transferred_ingredient": "a controlled asymptotic expansion",
            "target_result": "a target-domain bound",
            "first_calculation": "evaluate the leading coefficient",
        },
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
        "variant_id": "domain",
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


def _cross_loop(loop_id: str) -> LoopSpec:
    context = {
        "user_intent": "Find a substantive cross-domain theoretical-physics direction.",
        "variant_id": "cross_domain",
        "generation_mode": "cross_domain",
        "domain_cards": [{"field_id": "field-a"}, {"field_id": "field-b"}],
        "marking_scheme": _cross_scheme(),
    }
    reviewer_schema = {
        "type": "object",
        "required": ["marks", "cross_domain_assessment"],
        "additionalProperties": True,
    }
    return LoopSpec(
        loop_id=loop_id,
        context=context,
        proposers=(WorkerSpec(f"{loop_id}-p", "Propose a bridge.", PROPOSAL_SCHEMA),),
        reviewer=WorkerSpec(f"{loop_id}-r", "Review the bridge.", reviewer_schema),
        max_rounds=1,
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


def _proposal(title: str, *, cross: bool = False) -> dict[str, Any]:
    proposal = {
        "title": title,
        "idea_summary": "A concrete, evidence-aware proposal.",
        "calculation_plan": "Evaluate the bounded leading-order calculation.",
    }
    if cross:
        proposal["domain_roles"] = {
            "source_field_id": "field-a",
            "target_field_id": "field-b",
        }
    return proposal


def test_ranker_uses_committed_review_payload_and_best_completed_round(
    tmp_path: Path,
) -> None:
    ranker = _load_rank_module()
    loop = _single_loop("idea-a")
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
                },
                ("idea-a", 2): {
                    "marks": _single_marks(72),
                    "idea_assessment": _single_assessment(),
                },
            },
        ),
    )

    payload = ranker.rank_run(repository.root, "ideas-run")

    assert payload["schema_version"] == "arc.ideas.selected_rounds.v6"
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


def test_ranker_preserves_cross_domain_qualification_before_score(tmp_path: Path) -> None:
    ranker = _load_rank_module()
    genuine = _cross_loop("genuine")
    decorative = _cross_loop("decorative")
    repository = _execute(
        tmp_path / "runs",
        _request(genuine, decorative),
        _ScriptedLLM(
            proposals={
                ("genuine", 1): _proposal("Genuine lower score", cross=True),
                ("decorative", 1): _proposal("Decorative high score", cross=True),
            },
            reviews={
                ("genuine", 1): {
                    "marks": _cross_marks(76),
                    "cross_domain_assessment": _cross_assessment(),
                },
                ("decorative", 1): {
                    "marks": _cross_marks(96),
                    "cross_domain_assessment": _cross_assessment(
                        transfer_status="decorative"
                    ),
                },
            },
        ),
    )

    payload = ranker.rank_run(repository.root, "ideas-run")

    assert payload["schema_version"] == "arc.ideas.selected_rounds.v6"
    assert [entry["title"] for entry in payload["ranking"]] == ["Genuine lower score"]
    assert payload["unqualified"][0]["title"] == "Decorative high score"
    assert "transfer_status_must_be_genuine" in payload["unqualified"][0][
        "qualification_reasons"
    ]


def test_ranker_preserves_single_domain_feasibility_gate(tmp_path: Path) -> None:
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

    assert payload["schema_version"] == "arc.ideas.selected_rounds.v6"
    assert payload["ranking"][0]["title"] == "Feasible lower-score idea"
    blocked = next(
        entry
        for entry in payload["ranking"][0]["rounds"]
        if entry["round"] == 1
    )
    assert "first_calculation_is_not_feasible" in blocked["qualification_reasons"]
    assert "bounded_first_calculation_is_not_ready" in blocked["qualification_reasons"]


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
    assert "summary_order" not in payload
    assert any(
        "no_assessment policy" in warning
        for warning in payload["warnings"]
    )
    report = ranker.markdown_table(payload)
    assert "#### Focused Novelty Audit" in report
    assert "not an exhaustive proof of novelty" in report
    assert "Evidence checked:" in report
    assert "Tool queries used:" in report
    assert "Unresolved reviewer limitations:" in report


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
    assert completed_payload["schema_version"] == "arc.ideas.selected_rounds.v6"
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
    qualified = _single_loop("qualified-paused")
    repository = _execute(
        tmp_path / "paused-runs",
        _request(qualified),
        _PauseAfterFirstRoundLLM(
            proposals={
                ("qualified-paused", 1): _proposal("Qualified partial idea"),
            },
            reviews={
                ("qualified-paused", 1): {
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
    assert partial["schema_version"] == "arc.ideas.partial_selected_rounds.v2"
    assert partial["status"] == "provisional"
    assert partial["formal"] is False
    assert partial["provisional"] is True
    assert partial["ranking_kind"] == "non_formal_provisional"
    assert [entry["title"] for entry in partial["ranking"]] == [
        "Qualified partial idea"
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
    assert blocked_partial["ranking"] == []
    assert blocked_partial["unqualified"][0]["title"] == "Blocked partial idea"
    assert "first_calculation_is_not_feasible" in blocked_partial[
        "unqualified"
    ][0][
        "qualification_reasons"
    ]
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

    with pytest.raises(SystemExit, match="no complete committed"):
        ranker.rank_run(
            repository.root,
            "pending-run",
            mode="partial",
        )


def test_partial_cross_portfolio_appendix_shows_provisional_metadata() -> None:
    ranker = _load_rank_module()
    report = ranker.markdown_table(
        {
            "mode": "partial",
            "notice": "NON-FORMAL PROVISIONAL REPORT",
            "warnings": [],
            "ranking": [],
            "cross_domain": True,
            "unqualified": [],
            "portfolio_excluded": [
                {
                    "loop_id": "portfolio-loop",
                    "round": 2,
                    "title": "Portfolio-limited idea",
                    "portfolio_exclusion_reason": "mechanism_cap",
                    "loop_lifecycle": "paused",
                    "committed_round_count": 2,
                    "pause_reason": "execution_interrupted",
                    "qualification_reasons": [],
                }
            ],
        }
    )

    assert "Loop lifecycle: `paused`" in report
    assert "Complete committed rounds: `2`" in report
    assert "Pause reason: `execution_interrupted`" in report
    assert "Qualification failures: none" in report


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
        (
            "read_batch_round",
            "cannot rank ideas because a committed round is unavailable",
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
