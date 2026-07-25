from __future__ import annotations

import importlib.util
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from arc_jobs import RunEngine, RunRepository, RunSpec, RunStatus
from arc_llm import InvalidRequestError, LLMCompleted, LLMFailed
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


def test_formal_normalization_fills_missing_or_non_object_marks_with_zero() -> None:
    old_path = list(sys.path)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        from _arc_workflows.ideas_ranking import normalized_review_marks
    finally:
        sys.path[:] = old_path

    scheme = _single_scheme()
    expected = {
        "user_intent_relevance": 0,
        "novelty": 0,
        "confidence_of_novelty": 0,
        "scientific_value": 0,
        "planning": 0,
        "problem_well_definedness": 0,
        "total_score": 0,
    }

    assert normalized_review_marks(
        {"payload": {}},
        scheme,
        fill_missing_total=True,
    ) == expected
    assert normalized_review_marks(
        {"payload": {"marks": []}},
        scheme,
        fill_missing_total=True,
    ) == expected


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
) -> LoopSpec:
    context = {
        "user_intent": "Find a well-defined theoretical-physics research direction.",
        "variant_id": "domain",
        "marking_scheme": _single_scheme(),
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


def _round_task(prompt: str) -> dict[str, Any]:
    return json.loads(prompt.rsplit("## Round task\n", 1)[1])


def _execute(
    root: Path,
    request: BatchRequest,
    llm: _ScriptedLLM,
    *,
    run_id: str = "ideas-run",
) -> RunRepository:
    repository = RunRepository(root)
    handler = ProposerReviewerHandler(ProposerReviewerService(llm))  # type: ignore[arg-type]
    snapshot = RunEngine(repository).execute(
        RunSpec(run_id, handler.name, encode_batch_request(request)), handler
    )
    assert snapshot.status is RunStatus.SUCCEEDED
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
                },
                ("idea-a", 2): {
                    "marks": _single_marks(72),
                    "idea_assessment": _single_assessment(),
                },
            },
        ),
    )

    payload = ranker.rank_run(repository.root, "ideas-run")

    assert payload["ranking"][0]["title"] == "Best first-round idea"
    assert payload["ranking"][0]["round"] == 1
    assert [round_entry["round"] for round_entry in payload["ranking"][0]["rounds"]] == [1, 2]
    selected = payload["ranking"][0]
    assert selected["proposer_artifact"]["artifact_id"].startswith("proposer-reviewer/")
    assert len(selected["proposer_artifact"]["sha256"]) == 64
    assert len(selected["review_artifact"]["sha256"]) == 64
    rendered = json.dumps(payload)
    assert "relative_path" not in rendered
    assert str(repository.run_directory("ideas-run")) not in rendered
    markdown = ranker.markdown_table(payload)
    assert "# Ideas" in markdown
    assert "Proposer artifact:" in markdown
    assert "Review artifact:" in markdown


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

    assert payload["schema_version"] == "arc.ideas.selected_rounds.v2"
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

    assert payload["schema_version"] == "arc.ideas.selected_rounds.v3"
    assert payload["ranking"][0]["title"] == "Feasible lower-score idea"
    blocked = next(
        entry
        for entry in payload["ranking"][0]["rounds"]
        if entry["round"] == 1
    )
    assert "first_calculation_is_not_feasible" in blocked["qualification_reasons"]
    assert "bounded_first_calculation_is_not_ready" in blocked["qualification_reasons"]


def test_no_assessment_summary_order_follows_ranking(tmp_path: Path) -> None:
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
    assert payload["summary_order"] == payload["ranking"]
    assert "no_assessment policy" in payload["warnings"][0]


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
    assert pending_payload["ranking"] == []
    assert pending_payload["excluded_loops"] == [
        {
            "loop_id": "pending",
            "status": "pending",
            "reason": "loop_is_incomplete",
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
