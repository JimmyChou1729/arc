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
        context=importlib.import_module("_arc_workflows.calculate_context"),
        prompts=importlib.import_module("_arc_workflows.calculate_prompts"),
        prompt_builders=importlib.import_module(
            "_arc_workflows.calculate_prompt_builders"
        ),
        reviewer_schema=importlib.import_module(
            "_arc_workflows.calculate_reviewer_schema"
        ),
        runner=importlib.import_module("_arc_workflows.calculate_runner"),
        proposer_protocol=importlib.import_module("arc_proposer_reviewer.protocol"),
    )


def minimal_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "arc.workflow.calculate.config.v4",
        "run_id": "calc_001",
        "run_dir": str(tmp_path / "execute"),
        "workflow_json_dir": str(WORKFLOW_JSON),
        "steps": [
            {
                "step_id": "step_001",
                "prompt": "derive x",
                "kind": "new_derivation",
            }
        ],
    }
    payload.update(overrides)
    return payload


def review(
    action: str = "continue",
    *,
    trusted: bool = True,
    supporting_proposer_ids: list[str] | None = None,
    selected_proposer_id: str = "proposer_001",
    calculator_assessments: list[dict[str, Any]] | None = None,
    remarks: list[dict[str, Any]] | None = None,
    proposed_revision: str | None = None,
    expert_question: str | None = None,
) -> dict[str, Any]:
    proposer_ids = supporting_proposer_ids or [
        "proposer_001",
        "proposer_002",
    ]
    trusted_results = (
        [
            {
                "summary": "trusted calculation",
                "final_result": "x",
                "derivation": "derive x",
                "validity_scope": "declared scope",
                "supporting_proposer_ids": proposer_ids,
                "selected_proposer_id": selected_proposer_id,
                "comparison_reasoning": (
                    "The two independent derivations are mathematically "
                    "equivalent under the declared conventions."
                ),
            }
        ]
        if trusted
        else []
    )
    payload = {
        "calculator_assessments": calculator_assessments
        or [
            {
                "proposer_id": proposer_id,
                "assessment": "valid",
                "reason": "valid derivation under the declared prompt",
            }
            for proposer_id in ["proposer_001", "proposer_002"]
        ],
        "review_reasoning": (
            "Independently checked both calculations against the same target."
        ),
        "trusted_results": trusted_results,
        "remarks": remarks or [],
        "workflow_action": {
            "action": action,
            "proposed_revision": proposed_revision,
            "reason": "test",
            "expert_question": expert_question,
        },
    }
    return {
        "schema_version": "arc.proposer_reviewer.review.v1",
        "action": "continue",
        "reason": "review complete",
        "feedback": {
            proposer_id: f"feedback for {proposer_id}"
            for proposer_id in ["proposer_001", "proposer_002"]
        },
        "payload": payload,
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
    fake = FakeBatchExecutor(modules.runner, [review()])
    reference_claim = {"id": "ref_eq_001", "latex": "x = y + z"}

    result = modules.runner.run_calculation(
        minimal_config(
            tmp_path,
            steps=[
                {
                    "step_id": "blind_ref_eq_001",
                    "prompt": "derive x",
                    "kind": "check_known_result",
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


def test_retry_reruns_both_calculators_without_locked_outputs(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [
            review("retry", trusted=False),
            review(),
        ],
    )

    result = modules.runner.run_calculation(
        minimal_config(tmp_path), batch_executor=fake
    )

    first, second = (entry[0].loops[0] for entry in fake.calls)
    assert result["status"] == "completed"
    expected = ["proposer_001", "proposer_002"]
    assert [worker.worker_id for worker in first.proposers] == expected
    assert [worker.worker_id for worker in second.proposers] == expected
    assert "locked_outputs" not in second.context
    retry_packet = second.context["retry_feedback"][0]
    assert retry_packet == {
        "attempt_number": 1,
        "action": "retry",
        "shared_instruction": "test",
    }
    assert "feedback for" not in json.dumps(retry_packet)


def test_reference_disagreement_can_remain_untrusted_beside_trusted_result(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    source_remark = {
        "status": "untrusted",
        "summary": "The source claim differs from the joint derivation.",
        "reason": "The two calculators agree on a different expression.",
        "related_proposer_ids": ["proposer_001", "proposer_002"],
    }
    fake = FakeBatchExecutor(
        modules.runner,
        [review(remarks=[source_remark])],
    )

    result = modules.runner.run_calculation(
        minimal_config(
            tmp_path,
            max_recalculations=0,
            steps=[
                {
                    "step_id": "blind_ref_eq_001",
                    "prompt": "derive x",
                    "kind": "check_known_result",
                    "reviewer_reference_claim": {"id": "target", "latex": "x"},
                }
            ],
        ),
        batch_executor=fake,
    )

    assert result["status"] == "completed"
    assert result["steps"][0]["reviewer_decision"]["remarks"] == [source_remark]


def test_blind_reference_retry_never_passes_reviewer_material_to_proposers(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [
            review("retry", trusted=False),
            review(),
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
                    "kind": "check_known_result",
                    "reviewer_reference_claim": claim,
                }
            ],
        ),
        batch_executor=fake,
    )

    retry_context = fake.calls[1][0].loops[0].context
    assert result["status"] == "completed"
    assert retry_context["retry_feedback"][0]["action"] == "retry"
    assert retry_context["retry_feedback"][0]["shared_instruction"].startswith(
        "Recompute the supplied step independently."
    )
    assert "calculator_feedback" not in retry_context["retry_feedback"][0]
    assert "secret_reference" not in json.dumps(retry_context)
    assert "x = y + z" not in json.dumps(retry_context)


def test_retry_on_final_attempt_is_rejected_as_invalid_action_contract(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [review("retry", trusted=False)],
    )

    result = modules.runner.run_calculation(
        minimal_config(tmp_path, max_recalculations=0),
        batch_executor=fake,
    )

    step = result["steps"][0]
    assert result["status"] == "failed"
    assert step["status"] == "failed"
    assert "final action must be replan or pause_for_human" in step["error"]
    assert (
        step["attempts"][0]["reviewer_decision"]["workflow_action"]["action"]
        == "retry"
    )


def test_partial_trusted_result_is_preserved_for_replan(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [
            review(
                "replan",
                proposed_revision="Split step_001 into two independently checkable steps.",
            )
        ],
    )

    result = modules.runner.run_calculation(
        minimal_config(
            tmp_path,
            max_recalculations=0,
        ),
        batch_executor=fake,
    )

    step = result["steps"][0]
    assert result["status"] == "blocked_for_revision"
    assert step["blocked_output"]["workflow_action"]["action"] == "replan"
    assert step["accepted_output"]["trusted_results"]


def test_referee_judgment_enforces_only_structural_trust_relationships(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    active = ["proposer_001", "proposer_002"]

    missing_id = review(supporting_proposer_ids=["proposer_001"])
    with pytest.raises(ValueError, match="exactly match active proposer ids"):
        modules.consensus._review_decision(  # noqa: SLF001
            missing_id,
            active_proposer_ids=active,
        )

    duplicate_id = review(
        supporting_proposer_ids=["proposer_001", "proposer_001"]
    )
    with pytest.raises(ValueError, match="unique"):
        modules.consensus._review_decision(  # noqa: SLF001
            duplicate_id,
            active_proposer_ids=active,
        )

    retry_with_result = review("retry")
    with pytest.raises(ValueError, match="retry"):
        modules.consensus._review_decision(  # noqa: SLF001
            retry_with_result,
            active_proposer_ids=active,
        )

    wrong_selected = review(selected_proposer_id="proposer_999")
    with pytest.raises(ValueError, match="selected_proposer_id"):
        modules.consensus._review_decision(  # noqa: SLF001
            wrong_selected,
            active_proposer_ids=active,
        )

    differently_written = review()
    differently_written["payload"]["trusted_results"][0][
        "final_result"
    ] = r"\frac{a}{b}"
    assert modules.consensus._review_decision(  # noqa: SLF001
        differently_written,
        active_proposer_ids=active,
    )["trusted_results"][0]["final_result"] == r"\frac{a}{b}"


def test_pause_for_human_requires_question_and_never_accepts_remarks(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    paused = review(
        "pause_for_human",
        trusted=False,
        expert_question="Which boundary condition should govern this step?",
    )
    fake = FakeBatchExecutor(modules.runner, [paused])
    result = modules.runner.run_calculation(
        minimal_config(tmp_path, max_recalculations=0),
        batch_executor=fake,
    )
    assert result["status"] == "blocked_for_user"
    assert result["steps"][0]["accepted_output"] is None


def test_config_has_exactly_two_calculators_and_rejects_count_override(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    with pytest.raises(modules.config.ConfigError, match="unsupported fields"):
        modules.config.load_calculation_config(
            minimal_config(tmp_path, proposer_count=3)
        )

    config = modules.config.load_calculation_config(minimal_config(tmp_path))
    request = modules.prompts._attempt_batch_request(  # noqa: SLF001
        config,
        config.steps[0],
        attempt_number=1,
        retry_feedback=[],
        accepted_step_outputs={},
    )
    assert [worker.worker_id for worker in request.loops[0].proposers] == [
        "proposer_001",
        "proposer_002",
    ]


def test_invalid_replan_never_becomes_revision_handoff(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(
        modules.runner,
        [
            review(
                "replan",
                trusted=False,
                proposed_revision=None,
            )
        ],
    )

    result = modules.runner.run_calculation(
        minimal_config(
            tmp_path,
            max_recalculations=0,
        ),
        batch_executor=fake,
    )

    assert result["status"] == "failed"
    assert result["steps"][0]["status"] == "failed"
    assert "proposed_revision" in result["steps"][0]["error"]


def test_untrusted_remarks_do_not_become_trusted_outputs(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    remark = {
        "status": "untrusted",
        "summary": "An alternative sign remains unresolved.",
        "reason": "Only one calculator supports it.",
        "related_proposer_ids": ["proposer_002"],
    }
    fake = FakeBatchExecutor(
        modules.runner,
        [review(remarks=[remark])],
    )

    result = modules.runner.run_calculation(
        minimal_config(tmp_path),
        batch_executor=fake,
    )

    step = result["steps"][0]
    assert result["status"] == "completed"
    assert step["reviewer_decision"]["remarks"] == [remark]
    assert all(
        "alternative sign" not in json.dumps(trusted)
        for trusted in step["accepted_output"]["trusted_results"]
    )


def test_dry_run_does_not_invoke_batch_executor(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    fake = FakeBatchExecutor(modules.runner, [])

    result = modules.runner.run_calculation(
        minimal_config(tmp_path), batch_executor=fake, dry_run=True
    )

    assert result["status"] == "dry_run"
    assert fake.calls == []


@pytest.mark.parametrize("value", [True, 0, 3, 1.5])
def test_calculate_rejects_invalid_calculator_concurrency(
    tmp_path: Path,
    value: Any,
) -> None:
    modules = load_calculate_modules()

    with pytest.raises(
        modules.config.ConfigError,
        match="max_concurrent_calculators must be an integer between 1 and 2",
    ):
        modules.runner.run_calculation(
            minimal_config(tmp_path),
            max_concurrent_calculators=value,
            dry_run=True,
        )


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
        max_concurrent_calculators: int,
    ) -> dict[str, Any]:
        calls["config"] = config
        calls["dry_run"] = dry_run
        calls["authority"] = llm_options.host_authority.value
        calls["idle_timeout_seconds"] = (
            llm_options.limits.idle_timeout_seconds
        )
        calls["max_concurrent_calculators"] = max_concurrent_calculators
        return expected

    monkeypatch.setattr(modules.entry, "_read_json", fake_read)
    monkeypatch.setattr(modules.entry, "run_calculation", fake_run)

    status = modules.entry.main([
        "--config",
        str(config_path),
        "--dry-run",
        "--host-authority",
        "restricted",
        "--max-concurrent-calculators",
        "1",
        "--idle-timeout-seconds",
        "1800",
    ])

    assert status == 0
    assert calls == {
        "path": config_path,
        "config": payload,
        "dry_run": True,
        "authority": "restricted",
        "idle_timeout_seconds": 1800.0,
        "max_concurrent_calculators": 1,
    }
    assert json.loads(capsys.readouterr().out) == expected


def test_cli_disables_idle_timeout_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = load_calculate_modules()
    config_path = tmp_path / "config.json"
    captured: dict[str, Any] = {}

    monkeypatch.setattr(modules.entry, "_read_json", lambda path: {})

    def fake_run(
        config: dict[str, Any],
        *,
        dry_run: bool,
        llm_options: Any,
        max_concurrent_calculators: int,
    ) -> dict[str, Any]:
        captured["idle_timeout_seconds"] = (
            llm_options.limits.idle_timeout_seconds
        )
        return {"status": "dry_run"}

    monkeypatch.setattr(modules.entry, "run_calculation", fake_run)

    assert modules.entry.main(["--config", str(config_path)]) == 0
    assert captured["idle_timeout_seconds"] is None


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-a-number"])
def test_cli_rejects_invalid_idle_timeout(value: str) -> None:
    modules = load_calculate_modules()

    with pytest.raises(SystemExit) as caught:
        modules.entry.main([
            "--config",
            "unused.json",
            "--idle-timeout-seconds",
            value,
        ])

    assert caught.value.code == 2


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
        retry_feedback=[],
        accepted_step_outputs={},
    )
    expected = modules.runner.CommittedRound(
        loop_id=request.loops[0].loop_id,
        round_number=1,
        proposals={},
        review=review(),
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
    assert calls["run"][3].max_concurrent_workers == 2
    assert calls["projection"] == (
        tmp_path / "batches",
        "calculate_calc_001_step_001_attempt_001",
    )
    assert calls["inspect"] is True
    assert calls["round"] == (request.loops[0].loop_id, 1)

    serial_result = modules.runner._execute_public_batch(  # noqa: SLF001
        request,
        tmp_path / "serial-batches",
        "calculate_calc_001_step_001_attempt_001_serial",
        llm_options=modules.entry.LLMExecutionOptions(
            limits=modules.entry.ExecutionLimits(
                idle_timeout_seconds=1800
            )
        ),
        max_concurrent_calculators=1,
    )

    assert serial_result is expected
    assert calls["run"][3].max_concurrent_workers == 1
    assert calls["run"][3].llm.limits.idle_timeout_seconds == 1800


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
        retry_feedback=[],
        accepted_step_outputs={},
    )
    expected = modules.runner.CommittedRound(
        loop_id=request.loops[0].loop_id,
        round_number=1,
        proposals={},
        review=review(),
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
    assert schema["required"] == [
        "calculator_assessments",
        "review_reasoning",
        "trusted_results",
        "remarks",
        "workflow_action",
    ]
    assert "review_payload" not in schema["properties"]
    assert "proposer_messages" not in schema["properties"]
    trusted = schema["properties"]["trusted_results"]["items"]
    assert trusted["properties"]["supporting_proposer_ids"]["minItems"] == 2
    assert trusted["properties"]["supporting_proposer_ids"]["maxItems"] == 2
    assert "source_proposer_output_path" not in trusted["properties"]
    assert schema["allOf"]

    paused = review(
        "pause_for_human",
        trusted=False,
        expert_question="Which convention should govern the atomic target?",
    )
    Draft202012Validator(schema).validate(paused["payload"])

    invalid_replan = review(
        "replan",
        trusted=False,
        proposed_revision=" ",
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(
            invalid_replan["payload"]
        )


def test_blind_reviewer_keeps_reference_out_of_calculator_contract(
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
                    "kind": "check_known_result",
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
        retry_feedback=[],
        accepted_step_outputs={},
    )
    validator = Draft202012Validator(request.loops[0].reviewer.output_schema)
    valid = review()
    validator.validate(valid["payload"])
    loop = request.loops[0]
    assert "claim_001" in loop.reviewer.instructions
    assert all("claim_001" not in worker.instructions for worker in loop.proposers)
    assert "claim_001" not in json.dumps(loop.context)


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
        lambda config, *, dry_run, llm_options, max_concurrent_calculators: {
            "status": result_status
        },
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
    config = modules.config.load_calculation_config(minimal_config(tmp_path))
    assert config.max_recalculations == 1
    with pytest.raises(
        modules.config.ConfigError,
        match="calculate config contains unsupported fields: human_gate",
    ):
        modules.config.load_calculation_config(
            minimal_config(tmp_path, human_gate={"enabled": False})
        )
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


@pytest.mark.parametrize(
    ("kind", "expected_instruction"),
    [
        ("new_derivation", "without assuming the target expression"),
        ("check_known_result", "Classify agreement"),
        ("formal_setup", "downstream reduction remains"),
    ],
)
def test_calculate_step_kinds_have_distinct_acceptance_semantics(
    tmp_path: Path,
    kind: str,
    expected_instruction: str,
) -> None:
    modules = load_calculate_modules()
    config = modules.config.load_calculation_config(
        minimal_config(
            tmp_path,
            steps=[
                {
                    "step_id": f"step_{kind}",
                    "prompt": "perform the requested work",
                    "kind": kind,
                }
            ],
        )
    )
    context = modules.context.caller_context(
        config,
        config.steps[0],
        attempt_number=1,
        retry_feedback=[],
        accepted_step_outputs={},
    )

    assert context["step_kind"] == kind
    assert expected_instruction in context["step_acceptance_instruction"]


def test_calculate_rejects_retired_step_kind(tmp_path: Path) -> None:
    modules = load_calculate_modules()

    with pytest.raises(modules.config.ConfigError, match="step.kind must be one of"):
        modules.config.load_calculation_config(
            minimal_config(
                tmp_path,
                steps=[
                    {
                        "step_id": "old_step",
                        "prompt": "derive x",
                        "kind": "new_calculation",
                    }
                ],
            )
        )


def test_allowed_context_preserves_nested_scientific_path_fields(
    tmp_path: Path,
) -> None:
    modules = load_calculate_modules()
    allowed_context = {
        "integration": {
            "path": ["saddle", "contour", "endpoint"],
            "source_path": "physical branch through the lower half-plane",
            "cache_path": {"meaning": "trajectory in state space"},
        },
        "source_commands": ["differentiate the action"],
    }
    config = modules.config.load_calculation_config(
        minimal_config(
            tmp_path,
            steps=[
                {
                    "step_id": "path_sensitive",
                    "prompt": "evaluate the contour",
                    "kind": "new_derivation",
                    "allowed_context": allowed_context,
                }
            ],
        )
    )
    context = modules.context.caller_context(
        config,
        config.steps[0],
        attempt_number=1,
        retry_feedback=[],
        accepted_step_outputs={},
    )

    assert context["allowed_context"] == allowed_context
    assert context["allowed_context"] is not allowed_context


def test_calculate_template_and_docs_do_not_offer_retired_options() -> None:
    template = json.loads(
        (WORKFLOW_JSON / "calculate.config.template.json").read_text(
            encoding="utf-8"
        )
    )
    workflow = (SKILL / "workflows/calculate.md").read_text(encoding="utf-8")

    assert "artifact_options" not in template
    assert template["schema_version"] == "arc.workflow.calculate.config.v4"
    assert "proposer_count" not in template
    assert "human_gate" not in template
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
        [review()],
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
        steps=[
            {
                "step_id": "step_001",
                "prompt": "different calculation",
                "kind": "new_derivation",
            }
        ],
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
    consensus_policy_path = CALCULATE_MODULES / "calculate_consensus_policy.py"
    step_results = (
        CALCULATE_MODULES / "calculate_step_results.py"
    ).read_text(encoding="utf-8")
    context = (CALCULATE_MODULES / "calculate_context.py").read_text(
        encoding="utf-8"
    )
    prompts = (CALCULATE_MODULES / "calculate_prompts.py").read_text(
        encoding="utf-8"
    )
    prompt_builders = (
        CALCULATE_MODULES / "calculate_prompt_builders.py"
    ).read_text(encoding="utf-8")
    reviewer_schema = (
        CALCULATE_MODULES / "calculate_reviewer_schema.py"
    ).read_text(encoding="utf-8")
    runner = (CALCULATE_MODULES / "calculate_runner.py").read_text(
        encoding="utf-8"
    )

    assert len(entry.splitlines()) <= 150
    assert "def run_calculation(" not in entry
    assert "_arc_workflows.calculate_runner import" in entry
    assert "_arc_workflows.calculate_" not in config
    assert len(consensus.splitlines()) <= 300
    assert not consensus_policy_path.exists()
    assert "_arc_workflows.calculate_" not in consensus
    assert "_arc_workflows.calculate_config" not in consensus
    assert "_arc_workflows.calculate_prompts" not in consensus
    assert "_arc_workflows.calculate_runner" not in consensus
    assert len(step_results.splitlines()) <= 150
    assert "_arc_workflows.calculate_config import" in step_results
    assert "_arc_workflows.calculate_consensus import" not in step_results
    assert "_arc_workflows.calculate_runner" not in step_results
    assert "_arc_workflows.calculate_config import" in context
    assert "_arc_workflows.calculate_prompts" not in context
    assert "_arc_workflows.calculate_runner" not in context
    assert "_arc_workflows.calculate_config import" in prompts
    assert "_arc_workflows.calculate_context import" in prompts
    assert "_arc_workflows.calculate_prompt_builders import" in prompts
    assert "_arc_workflows.calculate_runner" not in prompts
    assert "_arc_workflows.calculate_config import" in prompt_builders
    assert "_arc_workflows.calculate_reviewer_schema import" in prompt_builders
    assert "_arc_workflows.calculate_runner" not in prompt_builders
    assert "_arc_workflows.calculate_config import" in reviewer_schema
    assert "_arc_workflows.calculate_runner" not in reviewer_schema
    assert "_arc_workflows.calculate_config import" in runner
    assert "_arc_workflows.calculate_consensus import" in runner
    assert "_arc_workflows.calculate_step_results import" in runner
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
                "calculate_step_results.py",
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
