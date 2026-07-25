from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest


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
    )


def minimal_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "arc.workflow.calculate.config.v1",
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
    source_discrepancies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    agreed_ids = agreed or []
    consensus = {
        "status": status,
        "accepted_result": {"result": "x"} if status == "all_agree" else None,
        "agreed_proposer_ids": agreed_ids,
        "likely_wrong_proposer_ids": likely_wrong or [],
        "recalculate_proposer_ids": recalculate or [],
        "validity_scope": "declared scope",
        "analysis": "review analysis",
        "best_written_proposer_id": best_written
        if best_written is not None
        else (agreed_ids[0] if status in {"all_agree", "reference_disagrees"} and agreed_ids else None),
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
            "special_limit_only": False,
        },
        "workflow_action": {
            "action": action or ("continue" if status == "all_agree" else "retry"),
            "requires_human": requires_human,
            "issue_type": "none" if status == "all_agree" else "calculation_disagreement",
            "proposed_revision": None,
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
        [review("all_agree", agreed=["proposer_001", "proposer_002"])],
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
    assert request.schema_version == "arc.proposer_reviewer.batch.v1"
    assert request.failure_policy is modules.prompts.BatchFailurePolicy.COLLECT
    assert modules.runner.encode_batch_request(request)["batch_id"] == request.batch_id
    assert loop.max_rounds == 1
    assert loop.allow_early_stop is False
    assert batch_root.name == "attempt-batches"
    assert run_id == "calculate_calc_001_blind_ref_eq_001_attempt_001"
    assert "reviewer_reference_claim" not in json.dumps(loop.context)
    assert "reviewer_reference_claim" in loop.reviewer.instructions
    assert all(worker.capabilities.internet is False for worker in loop.proposers)
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
            review("all_agree", agreed=["proposer_001", "proposer_002"]),
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


def test_cli_adapter_preserves_arguments_and_json_envelope(
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
    ) -> dict[str, Any]:
        calls["config"] = config
        calls["dry_run"] = dry_run
        return expected

    monkeypatch.setattr(modules.entry, "_read_json", fake_read)
    monkeypatch.setattr(modules.entry, "run_calculation", fake_run)

    status = modules.entry.main(
        ["--config", str(config_path), "--json", "--dry-run"]
    )

    assert status == 0
    assert calls == {
        "path": config_path,
        "config": payload,
        "dry_run": True,
    }
    assert json.loads(capsys.readouterr().out) == expected


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

    class FakeEngine:
        def __init__(self, repository: Any) -> None:
            calls["repository"] = repository

        def execute(self, spec: Any, handler: Any) -> Any:
            calls["spec"] = spec
            calls["handler"] = handler
            return SimpleNamespace(
                status=modules.runner.RunStatus.SUCCEEDED,
                error=None,
            )

    handler = SimpleNamespace(name="arc.proposer_reviewer.batch.v1")
    monkeypatch.setattr(
        modules.runner,
        "RunRepository",
        lambda root: {"root": root},
    )
    monkeypatch.setattr(modules.runner, "RunEngine", FakeEngine)
    monkeypatch.setattr(modules.runner, "LLMTaskService", lambda: object())
    monkeypatch.setattr(
        modules.runner,
        "ProposerReviewerService",
        lambda llm: {"llm": llm},
    )
    monkeypatch.setattr(
        modules.runner,
        "ProposerReviewerHandler",
        lambda service: handler,
    )
    monkeypatch.setattr(
        modules.runner,
        "read_batch_round",
        lambda *args: expected,
    )

    result = modules.runner._execute_public_batch(  # noqa: SLF001
        request, tmp_path / "batches", "calculate_calc_001_step_001_attempt_001"
    )

    assert result is expected
    assert calls["spec"].handler == "arc.proposer_reviewer.batch.v1"
    assert calls["spec"].semantic_input == modules.runner.encode_batch_request(
        request
    )
    assert calls["handler"] is handler


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


def test_config_parsing_and_model_selection_errors_are_typed(tmp_path: Path) -> None:
    modules = load_calculate_modules()
    config = modules.config.load_calculation_config(
        minimal_config(
            tmp_path,
            human_gate={"enabled": "false"},
            artifact_options={"save_prompts": "false"},
        )
    )

    assert config.human_gate["enabled"] is False
    assert config.artifact_options["save_prompts"] is False
    with pytest.raises(modules.config.ConfigError, match="explicit provider"):
        modules.config.load_calculation_config(
            minimal_config(tmp_path, defaults={"model": "exact-model"})
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
