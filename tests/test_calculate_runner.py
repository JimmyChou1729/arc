from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from arc_proposer_reviewer.models import BATCH_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/arc/skills/arc"
WORKFLOW_JSON = SKILL / "workflows/json"
SCRIPTS = SKILL / "scripts"
SCRIPT = SCRIPTS / "run-calculate.py"
CALCULATE_MODULES = SCRIPTS / "_arc_workflows"


def load_calculate_modules():
    spec = importlib.util.spec_from_file_location("run_calculate", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_calculate"] = module
    assert spec.loader is not None
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
        sys.dont_write_bytecode = old_dont_write_bytecode
    return SimpleNamespace(
        entry=module,
        config=importlib.import_module("_arc_workflows.calculate_config"),
        consensus=importlib.import_module("_arc_workflows.calculate_consensus"),
        prompts=importlib.import_module("_arc_workflows.calculate_prompts"),
        runner=importlib.import_module("_arc_workflows.calculate_runner"),
        proposer_protocol=importlib.import_module("arc_proposer_reviewer.protocol"),
    )


def minimal_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "arc.workflow.calculate.config.v2",
        "run_id": "calc_001",
        "run_dir": str(tmp_path / "execute"),
        "workflow_json_dir": str(WORKFLOW_JSON),
        "steps": [{"step_id": "step_001", "prompt": "derive x"}],
    }
    payload.update(overrides)
    return payload


def review(
    status: str,
    *,
    agreed: list[str] | None = None,
    likely_wrong: list[str] | None = None,
    recalculate: list[str] | None = None,
    best_written: str | None = None,
    target_quantity_match: bool = True,
    convention_match: bool = True,
    declared_scope_match: bool = True,
    agreement_covers_full_target: bool = True,
    accepted_by_reviewer_judgment: bool | None = None,
    action: str | None = None,
    requires_human: bool = False,
    proposed_revision: str | None = None,
    reference_claim_status: str | None = None,
    source_discrepancies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    agreed_ids = agreed or []
    selected_proposer_id = (
        best_written
        if best_written is not None
        else (
            agreed_ids[0]
            if status in {"all_agree", "reference_disagrees"} and agreed_ids
            else None
        )
    )
    accepted_result = (
        {
            "summary": "accepted calculation",
            "final_result": "x",
            "derivation": "derive x",
            "validity_scope": "declared scope",
            "selected_proposer_id": selected_proposer_id,
            "reference_claim_status": reference_claim_status
            or (
                "disagrees"
                if status == "reference_disagrees"
                else "not_applicable"
            ),
            "source_proposer_id": selected_proposer_id,
        }
        if status in {"all_agree", "reference_disagrees"}
        else None
    )
    consensus = {
        "status": status,
        "accepted_result": accepted_result,
        "agreed_proposer_ids": agreed_ids,
        "likely_wrong_proposer_ids": likely_wrong or [],
        "recalculate_proposer_ids": recalculate or [],
        "validity_scope": "declared scope",
        "analysis": "review analysis",
        "best_written_proposer_id": selected_proposer_id,
        "best_written_selection_reason": "clearest derivation"
        if status in {"all_agree", "reference_disagrees"}
        else "",
        "agreement_assessment": {
            "target_quantity_match": target_quantity_match,
            "convention_match": convention_match,
            "declared_scope_match": declared_scope_match,
            "agreement_covers_full_target": agreement_covers_full_target,
            "comparison_summary": "explicit algebraic comparison",
            "accepted_by_reviewer_judgment": (
                status == "all_agree"
                if accepted_by_reviewer_judgment is None
                else accepted_by_reviewer_judgment
            ),
            "tool_checks": [],
            "sanity_checks": [],
            "special_limit_only": False,
            "notes": "",
        },
        "workflow_action": {
            "action": action or ("continue" if status == "all_agree" else "retry"),
            "requires_human": requires_human,
            "issue_type": "none" if status == "all_agree" else "calculation_disagreement",
            "proposed_revision": proposed_revision,
            "reason": "test",
            "expert_question": "What should ARC do next?" if status != "all_agree" else "",
        },
        "source_discrepancies": source_discrepancies or [],
    }
    return {
        "schema_version": "arc.proposer_reviewer.review.v1",
        "action": "continue",
        "reason": "review complete",
        "feedback": {},
        "payload": {"consensus": consensus},
    }


class FakeBatchExecutor:
    def __init__(self, module: Any, reviews: list[dict[str, Any]]) -> None:
        self.module = module
        self.reviews = list(reviews)
        self.calls: list[tuple[Any, Path, str]] = []

    def __call__(self, request: Any, run_root: Path, run_id: str) -> Any:
        self.calls.append((request, run_root, run_id))
        loop = request.loops[0]
        document = self.reviews.pop(0)
        document["feedback"] = {
            worker.worker_id: f"feedback for {worker.worker_id}" for worker in loop.proposers
        }
        proposals = {
            worker.worker_id: {
                "proposer_id": worker.worker_id,
                "final_result": worker.worker_id,
            }
            for worker in loop.proposers
        }
        return self.module.CommittedRound(
            loop_id=loop.loop_id,
            round_number=1,
            proposals=proposals,
            review=document,
            proposal_refs={},
            review_ref=None,
            transcript_refs=(),
        )


def test_calculate_builds_public_batch_and_hides_blind_reference(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [
            review(
                "all_agree",
                agreed=["proposer_001", "proposer_002"],
                reference_claim_status="agrees",
            )
        ],
    )
    reference_claim = {"id": "ref_eq_001", "latex": "x = y + z"}

    result = modules.runner.run_calculation(
        minimal_config(
            tmp_path,
            steps=[
                {
                    "step_id": "blind_ref_eq_001",
                    "prompt": "derive x",
                    "reviewer_reference_claim": reference_claim,
                }
            ],
        ),
        batch_executor=fake,
    )

    request, batch_root, run_id = fake.calls[0]
    loop = request.loops[0]
    assert result["status"] == "completed"
    assert request.schema_version == BATCH_SCHEMA_VERSION
    assert request.failure_policy is modules.prompts.BatchFailurePolicy.COLLECT
    assert modules.proposer_protocol.encode_batch_request(request)["batch_id"] == request.batch_id
    assert loop.max_rounds == 1
    assert loop.allow_early_stop is False
    assert batch_root.name == "attempt-batches"
    assert run_id == "calculate_calc_001_blind_ref_eq_001_attempt_001"
    assert "reviewer_reference_claim" not in json.dumps(loop.context)
    assert "reviewer_reference_claim" in loop.reviewer.instructions
    assert all("blind-reference check" in worker.instructions for worker in loop.proposers)
    assert all("reviewer_reference_claim" not in worker.instructions for worker in loop.proposers)
    assert "review_path" not in result["steps"][0]["attempts"][0]
    assert "proposer_output_paths" not in result["steps"][0]["attempts"][0]


def test_two_agree_locks_outputs_and_recalculates_only_one_proposer(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [
            review(
                "two_agree",
                agreed=["proposer_001", "proposer_002"],
                likely_wrong=["proposer_003"],
                recalculate=["proposer_003"],
            ),
            review("all_agree", agreed=["proposer_003"], best_written="proposer_001"),
        ],
    )

    result = modules.runner.run_calculation(
        minimal_config(tmp_path, proposer_count=3), batch_executor=fake
    )

    first, second = (entry[0].loops[0] for entry in fake.calls)
    assert result["status"] == "completed"
    assert [worker.worker_id for worker in first.proposers] == [
        "proposer_001",
        "proposer_002",
        "proposer_003",
    ]
    assert [worker.worker_id for worker in second.proposers] == ["proposer_003"]
    assert sorted(second.context["locked_outputs"]) == ["proposer_001", "proposer_002"]
    assert second.context["retry_feedback"][0]["proposer_feedback"]["proposer_003"] == {
        "message": "feedback for proposer_003"
    }


def test_reference_disagreement_blocks_after_recalculation_budget(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [
            review(
                "reference_disagrees",
                agreed=["proposer_001", "proposer_002"],
                target_quantity_match=False,
                accepted_by_reviewer_judgment=False,
            )
        ],
    )

    result = modules.runner.run_calculation(
        minimal_config(
            tmp_path,
            max_recalculations=0,
            steps=[
                {
                    "step_id": "blind_ref_eq_001",
                    "prompt": "derive x",
                    "reviewer_reference_claim": {"id": "target", "latex": "x"},
                }
            ],
        ),
        batch_executor=fake,
    )

    assert result["status"] == "blocked_for_user"
    assert result["steps"][0]["blocked_output"]["trigger_status"] == "reference_disagrees"


def test_blind_reference_retry_never_passes_reviewer_material_to_proposers(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [
            review(
                "reference_disagrees",
                agreed=["proposer_001", "proposer_002"],
                target_quantity_match=False,
                accepted_by_reviewer_judgment=False,
            ),
            review(
                "all_agree",
                agreed=["proposer_001", "proposer_002"],
                reference_claim_status="agrees",
            ),
        ],
    )
    claim = {"id": "secret_reference", "latex": "x = y + z"}

    result = modules.runner.run_calculation(
        minimal_config(
            tmp_path,
            steps=[
                {
                    "step_id": "blind_retry_001",
                    "prompt": "derive x",
                    "reviewer_reference_claim": claim,
                }
            ],
        ),
        batch_executor=fake,
    )

    retry_context = fake.calls[1][0].loops[0].context
    assert result["status"] == "completed"
    assert retry_context["retry_feedback"][0]["status"] == "retry_required"
    assert retry_context["retry_feedback"][0]["proposer_feedback"] == {}
    assert "secret_reference" not in json.dumps(retry_context)
    assert "x = y + z" not in json.dumps(retry_context)


def test_human_gate_preserves_nonhuman_revision_handoff(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [
            review(
                "unresolved",
                action="revise_plan",
                requires_human=False,
                proposed_revision="Split step_001 into two independently checkable steps.",
            )
        ],
    )

    result = modules.runner.run_calculation(
        minimal_config(
            tmp_path,
            max_recalculations=0,
            human_gate={"enabled": True, "pause_on_statuses": ["unresolved"]},
        ),
        batch_executor=fake,
    )

    step = result["steps"][0]
    assert result["status"] == "blocked_for_revision"
    assert step["blocked_output"]["workflow_action"]["action"] == "revise_plan"
    assert step["blocked_output"]["requires_human"] is False


def test_all_agree_requires_exact_active_ids_closed_source_and_continue_action(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    active = ["proposer_001", "proposer_002"]

    missing_id = review("all_agree", agreed=["proposer_001"])
    with pytest.raises(ValueError, match="exactly match active proposer ids"):
        modules.consensus._review_consensus(  # noqa: SLF001
            missing_id,
            active_proposer_ids=active,
        )

    duplicate_id = review(
        "all_agree",
        agreed=["proposer_001", "proposer_001"],
    )
    with pytest.raises(ValueError, match="unique"):
        modules.consensus._review_consensus(  # noqa: SLF001
            duplicate_id,
            active_proposer_ids=active,
        )

    missing_result = review("all_agree", agreed=active)
    missing_result["payload"]["consensus"]["accepted_result"] = None
    with pytest.raises(ValueError, match="accepted_result must be an object"):
        modules.consensus._review_consensus(  # noqa: SLF001
            missing_result,
            active_proposer_ids=active,
        )

    wrong_source = review("all_agree", agreed=active)
    wrong_source["payload"]["consensus"]["accepted_result"][
        "source_proposer_id"
    ] = "proposer_999"
    with pytest.raises(ValueError, match="source_proposer_id"):
        modules.consensus._review_consensus(  # noqa: SLF001
            wrong_source,
            active_proposer_ids=active,
        )

    paused = review(
        "all_agree",
        agreed=active,
        action="pause_for_human",
        requires_human=True,
    )
    fake = FakeBatchExecutor(modules.runner, [paused])
    result = modules.runner.run_calculation(
        minimal_config(tmp_path),
        batch_executor=fake,
    )
    assert result["status"] == "blocked_for_user"
    assert result["steps"][0]["accepted_output"] is None


def test_accepted_result_reference_status_is_bound_to_step_mode() -> None:
    modules = load_calculate_modules()
    active = ["proposer_001", "proposer_002"]

    ordinary = review(
        "all_agree",
        agreed=active,
        reference_claim_status="agrees",
    )
    with pytest.raises(
        ValueError,
        match="reference_claim_status must be not_applicable",
    ):
        modules.consensus._review_consensus(  # noqa: SLF001
            ordinary,
            active_proposer_ids=active,
        )

    blind = review("all_agree", agreed=active)
    with pytest.raises(
        ValueError,
        match="reference_claim_status must be agrees",
    ):
        modules.consensus._review_consensus(  # noqa: SLF001
            blind,
            active_proposer_ids=active,
            reviewer_reference_claim={"claim_id": "claim_001"},
        )

    disagrees = review(
        "reference_disagrees",
        agreed=active,
        target_quantity_match=False,
        accepted_by_reviewer_judgment=False,
        reference_claim_status="agrees",
    )
    with pytest.raises(
        ValueError,
        match="reference_claim_status must be disagrees",
    ):
        modules.consensus._review_consensus(  # noqa: SLF001
            disagrees,
            active_proposer_ids=active,
            reviewer_reference_claim={"claim_id": "claim_001"},
        )


def test_blind_reference_requires_two_proposers_and_lists_every_active_id(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    blind_step = {
        "step_id": "blind_001",
        "prompt": "derive x",
        "reviewer_reference_claim": {"claim_id": "claim_001", "statement": "x"},
    }

    with pytest.raises(
        modules.config.ConfigError,
        match="blind reference checks require at least two proposers",
    ):
        modules.config.load_calculation_config(
            minimal_config(tmp_path, proposer_count=1, steps=[blind_step])
        )

    config = modules.config.load_calculation_config(
        minimal_config(tmp_path, proposer_count=3, steps=[blind_step])
    )
    request = modules.prompts._attempt_batch_request(  # noqa: SLF001
        config,
        config.steps[0],
        attempt_number=1,
        active_proposer_ids=[
            "proposer_001",
            "proposer_002",
            "proposer_003",
        ],
        locked_outputs={},
        retry_feedback=[],
        accepted_step_outputs={},
    )

    instructions = request.loops[0].reviewer.instructions
    assert "proposer_001, proposer_002, proposer_003" in instructions
    assert "every active proposer" in instructions


def test_invalid_nonhuman_revision_never_becomes_revision_handoff(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [
            review(
                "unresolved",
                action="revise_plan",
                requires_human=False,
                proposed_revision=None,
            )
        ],
    )

    result = modules.runner.run_calculation(
        minimal_config(
            tmp_path,
            max_recalculations=0,
            human_gate={"enabled": True, "pause_on_statuses": ["unresolved"]},
        ),
        batch_executor=fake,
    )

    assert result["status"] == "failed"
    assert result["steps"][0]["status"] == "failed"
    assert "proposed_revision" in result["steps"][0]["error"]


def test_source_discrepancy_stays_human_gated(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    discrepancy = {
        "item_id": "eq_7",
        "status": "likely_source_error",
        "source_claim": "source result",
        "derived_result": "blind result",
        "confidence_reason": "blind derivations agree but conventions may differ",
        "reviewer_says_no_human_convention_choice_needed": False,
        "decision_question": "Which convention should govern the work note?",
    }
    fake = FakeBatchExecutor(
        modules.runner,
        [
            review(
                "all_agree",
                agreed=["proposer_001", "proposer_002"],
                source_discrepancies=[discrepancy],
            )
        ],
    )

    result = modules.runner.run_calculation(
        minimal_config(tmp_path),
        batch_executor=fake,
    )

    blocked = result["steps"][0]["blocked_output"]
    assert result["status"] == "blocked_for_user"
    assert blocked["reason"] == "source_discrepancy_requires_human"
    assert blocked["source_discrepancies"] == [discrepancy]


def test_dry_run_does_not_invoke_batch_executor(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(modules.runner, [])

    result = modules.runner.run_calculation(
        minimal_config(tmp_path), batch_executor=fake, dry_run=True
    )

    assert result["status"] == "dry_run"
    assert fake.calls == []


def test_cli_adapter_preserves_arguments_and_emits_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    modules = load_calculate_modules()
    config_path = tmp_path / "config.json"
    payload = {"config": "payload"}
    expected = {"schema_version": "result", "status": "dry_run"}
    calls: dict[str, Any] = {}

    def fake_read(path: Path) -> dict[str, Any]:
        calls["path"] = path
        return payload

    def fake_run(
        config: dict[str, Any],
        *,
        dry_run: bool,
        llm_options: Any,
    ) -> dict[str, Any]:
        calls["config"] = config
        calls["dry_run"] = dry_run
        calls["authority"] = llm_options.host_authority.value
        return expected

    monkeypatch.setattr(modules.entry, "_read_json", fake_read)
    monkeypatch.setattr(modules.entry, "run_calculation", fake_run)

    status = modules.entry.main([
        "--config", str(config_path), "--dry-run", "--host-authority", "restricted"
    ])

    assert status == 0
    assert calls == {
        "path": config_path,
        "config": payload,
        "dry_run": True,
        "authority": "restricted",
    }
    assert json.loads(capsys.readouterr().out) == expected


def test_cli_rejects_obsolete_json_flag() -> None:
    modules = load_calculate_modules()

    with pytest.raises(SystemExit) as caught:
        modules.entry.main(["--config", "unused.json", "--json"])

    assert caught.value.code == 2


def test_default_executor_uses_public_engine_and_committed_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modules = load_calculate_modules()
    config = modules.config.load_calculation_config(minimal_config(tmp_path))
    request = modules.prompts._attempt_batch_request(  # noqa: SLF001
        config,
        config.steps[0],
        attempt_number=1,
        active_proposer_ids=["proposer_001", "proposer_002"],
        locked_outputs={},
        retry_feedback=[],
        accepted_step_outputs={},
    )
    expected = modules.runner.CommittedRound(
        loop_id=request.loops[0].loop_id,
        round_number=1,
        proposals={},
        review=review("all_agree", agreed=["proposer_001", "proposer_002"]),
        proposal_refs={},
        review_ref=None,
        transcript_refs=(),
    )
    calls: dict[str, Any] = {}

    class FakeProjection:
        def inspect(self) -> Any:
            calls["inspect"] = True
            return SimpleNamespace(
                durable_lifecycle="succeeded",
                run_revision=2,
                loops=(
                    SimpleNamespace(
                        loop_id=request.loops[0].loop_id,
                        rounds_completed=1,
                    ),
                ),
            )

        def read_round(self, loop_id: str, round_number: int) -> Any:
            calls["round"] = (loop_id, round_number)
            return expected

    class FakeRunner:
        def run(
            self,
            passed_request: Any,
            run_root: Path,
            run_id: str,
            *,
            options: Any,
        ) -> Any:
            calls["run"] = (passed_request, run_root, run_id, options)
            return SimpleNamespace(
                status=modules.runner.RunStatus.SUCCEEDED,
                error=None,
            )

        def projection(self, run_root: Path, run_id: str) -> FakeProjection:
            calls["projection"] = (run_root, run_id)
            return FakeProjection()

    monkeypatch.setattr(modules.runner, "BatchRunner", FakeRunner)

    result = modules.runner._execute_public_batch(  # noqa: SLF001
        request, tmp_path / "batches", "calculate_calc_001_step_001_attempt_001"
    )

    assert result is expected
    assert calls["run"][:3] == (
        request,
        tmp_path / "batches",
        "calculate_calc_001_step_001_attempt_001",
    )
    assert calls["projection"] == (
        tmp_path / "batches",
        "calculate_calc_001_step_001_attempt_001",
    )
    assert calls["inspect"] is True
    assert calls["round"] == (request.loops[0].loop_id, 1)


def test_default_executor_recovers_committed_frontier_after_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = load_calculate_modules()
    config = modules.config.load_calculation_config(minimal_config(tmp_path))
    request = modules.prompts._attempt_batch_request(  # noqa: SLF001
        config,
        config.steps[0],
        attempt_number=1,
        active_proposer_ids=["proposer_001", "proposer_002"],
        locked_outputs={},
        retry_feedback=[],
        accepted_step_outputs={},
    )
    expected = modules.runner.CommittedRound(
        loop_id=request.loops[0].loop_id,
        round_number=1,
        proposals={},
        review=review("all_agree", agreed=["proposer_001", "proposer_002"]),
        proposal_refs={},
        review_ref=None,
        transcript_refs=(),
    )
    calls: list[str] = []

    class FakeProjection:
        def inspect(self) -> Any:
            calls.append("inspect")
            return SimpleNamespace(
                run_id="calculate_calc_001_step_001_attempt_001",
                durable_lifecycle="running",
                run_revision=3,
                loop_revisions={request.loops[0].loop_id: 2},
                loops=(
                    SimpleNamespace(
                        loop_id=request.loops[0].loop_id,
                        lifecycle="succeeded",
                        phase="completed",
                        current_round=1,
                        rounds_completed=1,
                        revision=2,
                        pause=None,
                        activity=SimpleNamespace(
                            best_effort=True,
                            loop_group_status="succeeded",
                            proposer_pending=0,
                            proposer_succeeded=2,
                            proposer_failed=0,
                        ),
                        integrity_error=None,
                    ),
                ),
                activity_integrity_error=None,
            )

        def read_round(self, loop_id: str, round_number: int) -> Any:
            calls.append(f"read:{loop_id}:{round_number}")
            return expected

    class FakeRunner:
        def run(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("engine boundary failed after commit")

        def projection(self, *args: Any, **kwargs: Any) -> FakeProjection:
            calls.append("projection")
            return FakeProjection()

    monkeypatch.setattr(modules.runner, "BatchRunner", FakeRunner)

    result = modules.runner._execute_public_batch(  # noqa: SLF001
        request,
        tmp_path / "batches",
        "calculate_calc_001_step_001_attempt_001",
    )

    assert result is expected
    assert calls == [
        "projection",
        "inspect",
        f"read:{request.loops[0].loop_id}:1",
    ]


def test_succeeded_batch_projection_failure_reports_durable_frontier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = load_calculate_modules()
    config = modules.config.load_calculation_config(minimal_config(tmp_path))
    request = modules.prompts._attempt_batch_request(  # noqa: SLF001
        config,
        config.steps[0],
        attempt_number=1,
        active_proposer_ids=["proposer_001", "proposer_002"],
        locked_outputs={},
        retry_feedback=[],
        accepted_step_outputs={},
    )

    class CorruptProjection:
        def inspect(self) -> Any:
            return SimpleNamespace(
                run_id="calculate_calc_001_step_001_attempt_001",
                durable_lifecycle="succeeded",
                run_revision=4,
                loop_revisions={request.loops[0].loop_id: 2},
                loops=(
                    SimpleNamespace(
                        loop_id=request.loops[0].loop_id,
                        lifecycle="succeeded",
                        phase="completed",
                        current_round=1,
                        rounds_completed=1,
                        revision=2,
                        pause=None,
                        activity=SimpleNamespace(
                            best_effort=True,
                            loop_group_status="succeeded",
                            proposer_pending=0,
                            proposer_succeeded=2,
                            proposer_failed=0,
                        ),
                        integrity_error=None,
                    ),
                ),
                activity_integrity_error=None,
            )

        def read_round(self, loop_id: str, round_number: int) -> Any:
            raise ValueError("committed round digest mismatch")

    class FakeRunner:
        def run(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                status=modules.runner.RunStatus.SUCCEEDED,
                error=None,
            )

        def projection(self, *args: Any, **kwargs: Any) -> CorruptProjection:
            return CorruptProjection()

    monkeypatch.setattr(modules.runner, "BatchRunner", FakeRunner)

    with pytest.raises(
        modules.runner.BatchExecutionError,
        match="committed round digest mismatch",
    ) as caught:
        modules.runner._execute_public_batch(  # noqa: SLF001
            request,
            tmp_path / "batches",
            "calculate_calc_001_step_001_attempt_001",
        )

    assert caught.value.durable_frontier["run_lifecycle"] == "succeeded"
    assert (
        caught.value.durable_frontier["round_read_error"]
        == "committed round digest mismatch"
    )


def test_reviewer_template_and_schema_use_public_payload_contract(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    config = modules.config.load_calculation_config(minimal_config(tmp_path))
    request = modules.prompts._attempt_batch_request(  # noqa: SLF001
        config,
        config.steps[0],
        attempt_number=1,
        active_proposer_ids=["proposer_001", "proposer_002"],
        locked_outputs={},
        retry_feedback=[],
        accepted_step_outputs={},
    )
    template = (WORKFLOW_JSON / "calculate-reviewer.template.json").read_text(
        encoding="utf-8"
    )
    schema = request.loops[0].reviewer.output_schema

    assert "arc_llm_call_record" not in template
    assert "arc.llm.review_envelope.v1" not in template
    assert "arc.proposer_reviewer.review.v1" in template
    assert schema["required"] == ["consensus"]
    assert "review_payload" not in schema["properties"]
    assert "proposer_messages" not in schema["properties"]
    accepted = schema["properties"]["consensus"]["properties"]["accepted_result"]
    assert "source_proposer_id" in accepted["properties"]
    assert "source_proposer_output_path" not in accepted["properties"]
    assert accepted["type"] == ["object", "null"]
    assert schema["properties"]["consensus"]["allOf"]
    agreed = schema["properties"]["consensus"]["properties"]["agreed_proposer_ids"]
    assert agreed["uniqueItems"] is True

    paused_all_agree = review(
        "all_agree",
        agreed=["proposer_001", "proposer_002"],
        action="pause_for_human",
        requires_human=True,
    )
    Draft202012Validator(schema).validate(paused_all_agree["payload"])

    invalid_nonhuman_revision = review(
        "unresolved",
        action="revise_plan",
        requires_human=False,
        proposed_revision=" ",
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(
            invalid_nonhuman_revision["payload"]
        )


def test_blind_reviewer_schema_binds_reference_claim_status(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    config = modules.config.load_calculation_config(
        minimal_config(
            tmp_path,
            steps=[
                {
                    "step_id": "blind_001",
                    "prompt": "derive x",
                    "reviewer_reference_claim": {
                        "claim_id": "claim_001",
                        "statement": "x",
                    },
                }
            ],
        )
    )
    request = modules.prompts._attempt_batch_request(  # noqa: SLF001
        config,
        config.steps[0],
        attempt_number=1,
        active_proposer_ids=["proposer_001", "proposer_002"],
        locked_outputs={},
        retry_feedback=[],
        accepted_step_outputs={},
    )
    validator = Draft202012Validator(request.loops[0].reviewer.output_schema)
    valid = review(
        "all_agree",
        agreed=["proposer_001", "proposer_002"],
        reference_claim_status="agrees",
    )
    validator.validate(valid["payload"])

    invalid = review(
        "all_agree",
        agreed=["proposer_001", "proposer_002"],
        reference_claim_status="not_applicable",
    )
    with pytest.raises(ValidationError):
        validator.validate(invalid["payload"])


@pytest.mark.parametrize(
    ("result_status", "expected_exit"),
    [
        ("completed", 0),
        ("dry_run", 0),
        ("blocked_for_user", 0),
        ("blocked_for_revision", 0),
        ("failed", 1),
    ],
)
def test_cli_exit_status_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result_status: str,
    expected_exit: int,
) -> None:
    modules = load_calculate_modules()
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(modules.entry, "_read_json", lambda path: minimal_config(tmp_path))
    monkeypatch.setattr(
        modules.entry,
        "run_calculation",
        lambda config, *, dry_run, llm_options: {"status": result_status},
    )

    assert modules.entry.main(["--config", str(config_path)]) == expected_exit
    assert json.loads(capsys.readouterr().out)["status"] == result_status


def test_cli_config_error_uses_usage_exit_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = load_calculate_modules()
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(
        modules.entry,
        "_read_json",
        lambda path: (_ for _ in ()).throw(modules.config.ConfigError("bad config")),
    )

    with pytest.raises(SystemExit) as caught:
        modules.entry.main(["--config", str(config_path)])

    assert caught.value.code == 2


def test_calculate_docs_define_blocked_as_normal_nonterminal_exit() -> None:
    workflow = (SKILL / "workflows/calculate.md").read_text(encoding="utf-8")

    assert "blocked result is a normal nonterminal workflow" in workflow
    assert "exits `1` for a `failed` result" in workflow
    assert "`2` for command usage or invalid" in workflow


def test_config_parsing_and_model_selection_errors_are_typed(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    config = modules.config.load_calculation_config(
        minimal_config(tmp_path, human_gate={"enabled": "false"})
    )

    assert config.human_gate["enabled"] is False
    with pytest.raises(
        modules.config.ConfigError,
        match="calculate config contains unsupported fields: artifact_options",
    ):
        modules.config.load_calculation_config(
            minimal_config(
                tmp_path,
                artifact_options={"save_prompts": True},
            )
        )
    with pytest.raises(modules.config.ConfigError, match="explicit provider"):
        modules.config.load_calculation_config(
            minimal_config(tmp_path, defaults={"model": "exact-model"})
        )
    with pytest.raises(
        modules.config.ConfigError,
        match="defaults contains unsupported fields: tier",
    ):
        modules.config.load_calculation_config(
            minimal_config(tmp_path, defaults={"tier": "high"})
        )


def test_calculate_strict_source_mode_binds_workflow_and_integrity_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = load_calculate_modules()
    other_workflow = tmp_path / "other-workflow"
    other_workflow.mkdir()
    other_integrity = tmp_path / "integrity.md"
    other_integrity.write_text("other", encoding="utf-8")
    monkeypatch.setenv("ARC_REQUIRE_REPO_ROOT", str(ROOT))

    config = modules.config.load_calculation_config(minimal_config(tmp_path))
    assert config.workflow_json_dir == WORKFLOW_JSON

    with pytest.raises(
        modules.config.ConfigError,
        match="strict ARC source mode requires workflow_json_dir",
    ):
        modules.config.load_calculation_config(
            minimal_config(
                tmp_path,
                workflow_json_dir=str(other_workflow),
            )
        )
    with pytest.raises(
        modules.config.ConfigError,
        match="strict ARC source mode requires integrity_reference_path",
    ):
        modules.config.load_calculation_config(
            minimal_config(
                tmp_path,
                defaults={"integrity_reference_path": str(other_integrity)},
            )
        )
    with pytest.raises(
        modules.config.ConfigError,
        match="strict ARC source mode cannot resolve integrity_reference_path",
    ):
        modules.config.load_calculation_config(
            minimal_config(
                tmp_path,
                defaults={
                    "integrity_reference_path": str(
                        tmp_path / "missing-integrity.md"
                    )
                },
            )
        )


def test_calculate_config_canonicalizes_owned_paths(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    aliased_run_dir = tmp_path / "outer" / ".." / "execute"
    aliased_workflow = WORKFLOW_JSON / ".." / "json"

    config = modules.config.load_calculation_config(
        minimal_config(
            tmp_path,
            run_dir=str(aliased_run_dir),
            workflow_json_dir=str(aliased_workflow),
        )
    )

    assert config.run_dir == (tmp_path / "execute").resolve()
    assert config.workflow_json_dir == WORKFLOW_JSON.resolve()


def test_calculate_template_and_docs_do_not_offer_retired_options() -> None:
    template = json.loads(
        (WORKFLOW_JSON / "calculate.config.template.json").read_text(
            encoding="utf-8"
        )
    )
    workflow = (SKILL / "workflows/calculate.md").read_text(encoding="utf-8")

    assert "artifact_options" not in template
    assert "runtime" not in template["defaults"]
    assert "save_prompts" not in workflow
    assert "--json" not in workflow


def test_proposer_policy_has_no_retired_runtime_branch() -> None:
    source = (
        CALCULATE_MODULES / "calculate_prompts.py"
    ).read_text(encoding="utf-8")

    assert 'elif step.kind == "new_calculation"' not in source
    assert "def _proposer_runtime" not in source


def test_outer_run_binds_config_and_state_under_project_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [review("all_agree", agreed=["proposer_001", "proposer_002"])],
    )
    lease_calls: list[tuple[Path, bool]] = []

    class FakeLease:
        def __init__(self, path: Path) -> None:
            self.path = path

        def acquire(self, *, blocking: bool = False) -> "FakeLease":
            lease_calls.append((self.path, blocking))
            return self

        def __enter__(self) -> "FakeLease":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(modules.runner, "FileLease", FakeLease)

    result = modules.runner.run_calculation(
        minimal_config(tmp_path),
        batch_executor=fake,
    )

    run_root = tmp_path / "execute" / "calc_001"
    saved_config = json.loads(
        (run_root / "config.json").read_text(encoding="utf-8")
    )
    saved_state = json.loads(
        (run_root / "state.json").read_text(encoding="utf-8")
    )
    digest = result["config_semantic_key_sha256"]
    assert len(digest) == 64
    assert saved_config["semantic_key_sha256"] == digest
    assert saved_state["config_semantic_key_sha256"] == digest
    assert lease_calls == [
        (run_root / ".calculate.lock", True)
    ]

    state_before = (run_root / "state.json").read_bytes()
    changed = minimal_config(
        tmp_path,
        steps=[{"step_id": "step_001", "prompt": "different calculation"}],
    )
    with pytest.raises(
        modules.config.ConfigError,
        match="run_id is already bound to a different calculation config",
    ):
        modules.runner.run_calculation(
            changed,
            batch_executor=FakeBatchExecutor(modules.runner, []),
        )
    assert (run_root / "state.json").read_bytes() == state_before

    saved_state["config_semantic_key_sha256"] = "0" * 64
    (run_root / "state.json").write_text(
        json.dumps(saved_state),
        encoding="utf-8",
    )
    with pytest.raises(
        modules.config.ConfigError,
        match="state is bound to a different config",
    ):
        modules.runner.run_calculation(
            minimal_config(tmp_path),
            batch_executor=FakeBatchExecutor(modules.runner, []),
        )


def test_json_reader_preserves_calculate_error_contract(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    path = tmp_path / "config.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match=f"Expected JSON object at {path}"):
        modules.config._read_json(path)


def test_calculate_modules_have_one_way_dependencies() -> None:
    entry = SCRIPT.read_text(encoding="utf-8")
    config = (CALCULATE_MODULES / "calculate_config.py").read_text(
        encoding="utf-8"
    )
    consensus = (CALCULATE_MODULES / "calculate_consensus.py").read_text(
        encoding="utf-8"
    )
    prompts = (CALCULATE_MODULES / "calculate_prompts.py").read_text(
        encoding="utf-8"
    )
    runner = (CALCULATE_MODULES / "calculate_runner.py").read_text(
        encoding="utf-8"
    )

    assert len(entry.splitlines()) <= 150
    assert "def run_calculation(" not in entry
    assert "_arc_workflows.calculate_runner import" in entry
    assert "_arc_workflows.calculate_" not in config
    assert "_arc_workflows.calculate_config import" in consensus
    assert "_arc_workflows.calculate_prompts" not in consensus
    assert "_arc_workflows.calculate_runner" not in consensus
    assert "_arc_workflows.calculate_config import" in prompts
    assert "_arc_workflows.calculate_consensus import" in prompts
    assert "_arc_workflows.calculate_runner" not in prompts
    assert "_arc_workflows.calculate_config import" in runner
    assert "_arc_workflows.calculate_consensus import" in runner
    assert "_arc_workflows.calculate_prompts import" in runner


def test_default_paths_remain_relative_to_skill_after_split(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    payload = minimal_config(tmp_path)
    payload.pop("workflow_json_dir")

    config = modules.config.load_calculation_config(payload)

    assert config.workflow_json_dir == WORKFLOW_JSON


def test_runner_has_no_retired_llm_or_private_artifact_dependencies() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            SCRIPT,
            *(CALCULATE_MODULES / name for name in (
                "calculate_config.py",
                "calculate_consensus.py",
                "calculate_prompts.py",
                "calculate_runner.py",
            )),
        ]
    )

    assert "arc_llm.proposers_reviewer" not in source
    assert "arc_llm.paper_access_policy" not in source
    assert "RunPaths" not in source
    assert "attempt_paths" not in source
    assert "read_proposer_outputs" not in source
