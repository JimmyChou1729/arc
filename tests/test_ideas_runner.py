from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from arc_jobs import ImmutableArtifactStore, RunEngine, canonical_json_bytes
from arc_llm import HostAuthority, LLMCompleted, LLMExecutionOptions
from arc_proposer_reviewer.models import BATCH_SCHEMA_VERSION
from arc_proposer_reviewer.protocol import decode_batch_request


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_JSON = ROOT / "plugins/arc/skills/arc/workflows/json"
SCRIPTS = ROOT / "plugins/arc/skills/arc/scripts"
RUNNER = SCRIPTS / "run-ideas.py"
IDEAS_MODULES = SCRIPTS / "_arc_workflows"


def _load_runner_module():
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("run_ideas", RUNNER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_ideas"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        sys.path.remove(str(SCRIPTS))


def _config(tmp_path: Path) -> dict[str, Any]:
    project = tmp_path / "project"
    state = project / ".arc" / "domain"
    packages = state / "packages"
    packages.mkdir(parents=True)
    (packages / "brief.md").write_text("# Brief\n", encoding="utf-8")
    seed = "arXiv:2401.00001"
    provenance = {
        "schema_version": "arc.workflow.domain_seed_provenance.v1",
        "requested_seed_mappings": [{
            "requested_seed": seed,
            "build_seed": seed,
            "domain_id": "single",
            "resolution": "explicit_seed",
        }],
        "build_origins": [{
            "domain_id": "single",
            "build_seed": seed,
            "origin_selection": {"mode": "explicit_seed", "requested_seed": seed},
        }],
        "deduplications": [],
    }
    (state / "seed-provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    groups = [{
        "field_id": "field-single",
        "domain_package_ids": ["single"],
        "field_card": {
            "seed_papers": [seed],
            "summary_json_paths": [".arc/domain/packages/single.json"],
            "summary_markdown_paths": [".arc/domain/packages/brief.md"],
            "paper_json_pack_paths": [".arc/domain/packages/single-papers.json"],
            "task_focus": {},
            "methodology": [],
        },
    }]
    (state / "field-grouping.json").write_text(
        json.dumps({"schema_version": "arc.workflow.domain_field_grouping.v1", "field_groups": groups}),
        encoding="utf-8",
    )
    (state / "domain-manifest.json").write_text(
        json.dumps({
            "schema_version": "arc.workflow.domain_manifest.v3",
            "research_scope": "single_domain",
            "requested_seed_papers": [seed],
            "seed_provenance_artifact": {
                "path": ".arc/domain/seed-provenance.json",
                "sha256": hashlib.sha256(canonical_json_bytes(provenance)).hexdigest(),
                "schema_version": "arc.workflow.domain_seed_provenance.v1",
            },
            "package_count": 1,
            "field_count": 1,
            "grouping_artifact": ".arc/domain/field-grouping.json",
            "domain_packages": [{
                "domain_package_id": "single",
                "seed_paper": seed,
                "summary_json_path": ".arc/domain/packages/single.json",
            }],
            "field_groups": groups,
            "grouping_warnings": [],
        }),
        encoding="utf-8",
    )
    (packages / "single.json").write_text(
        json.dumps(_domain_summary(seed)),
        encoding="utf-8",
    )
    return {
        "schema_version": "arc.workflow.ideas.config.v2",
        "run_id": "ideas-test",
        "run_dir": str(project / ".arc" / "ideas"),
        "project_dir": str(project),
        "user_intent": "Find a controlled calculation.",
        "variant_config_dir": str(WORKFLOW_JSON),
        "variant_glob": "ideas-domain.variant.json",
        "loops_per_variant": 1,
    }


def _domain_summary(seed: str) -> dict[str, Any]:
    axes = [
        {
            "axis": f"Open axis {index}",
            "guidance": f"Test route {index}.",
            "example_variations": [f"Variation {index}"],
            "papers": [seed],
        }
        for index in range(1, 7)
    ]
    return {
        "schema_version": "arc.domain_summary.v5",
        "domain_title": "Single test domain",
        "brief_introduction": "A validated summary.",
        "task_focus": {
            "user_intent": "Find a controlled calculation.",
            "research_scope": "The test domain.",
            "priority_rules": [],
        },
        "foundation_paper": {
            "paper_id": seed,
            "title": "Foundation",
            "reason": "Anchor",
        },
        "best_reference_paper": {
            "paper_id": seed,
            "title": "Reference",
            "reason": "Entry point",
        },
        "methodology": [],
        "mathematical_opportunities": {"well_defined_problems": []},
        "known_solved_cases": [],
        "open_axes_for_new_work": axes,
        "warnings": [],
    }


class _FakeLLM:
    def __init__(self) -> None:
        self.options: list[LLMExecutionOptions] = []
        self.requests = []

    def execute(self, _context, request, *, options):
        self.options.append(options)
        self.requests.append(request)
        if "one proposer" in request.prompt:
            return LLMCompleted(
                {
                    "title": "controlled idea",
                    "idea_summary": "summary",
                    "motivation": "motivation",
                    "novelty_checks": ["arXiv source: result"],
                    "calculation_plan": "bounded calculation",
                    "validation_checks": ["known limit"],
                    "risks": ["named risk"],
                },
                "fake", "fake-model", None, None,
            )
        active_ids = request.output.schema["properties"]["feedback"]["required"]
        return LLMCompleted(
            {
                "schema_version": "arc.proposer_reviewer.review.v1",
                "action": "stop",
                "reason": "sufficient",
                "feedback": {worker_id: "sharpen the calculation" for worker_id in active_ids},
                "payload": {
                    "evidence_checked": ["arXiv source: result"],
                    "tool_queries_used": ["focused ARC query"],
                    "marks": {
                        "user_intent_relevance": 10,
                        "novelty": 10,
                        "confidence_of_novelty": 10,
                        "scientific_value": 15,
                        "planning": 10,
                        "problem_well_definedness": 15,
                        "simplicity": 8,
                        "generality": 7,
                        "total_score": 85,
                    },
                    "reviewer_benchmark": {
                        "same_direction_alternative": "Keep the same core calculation.",
                        "preserves_proposer_direction": True,
                        "comparison": "One coherent idea with a natural range of cases.",
                    },
                },
            },
            "fake", "fake-model", None, None,
        )


def test_dry_run_has_closed_workers_and_direct_research_policy(tmp_path: Path) -> None:
    runner = _load_runner_module()
    result = runner.run_ideas(_config(tmp_path), dry_run=True)
    worker = result["batch_request"]["loops"][0]["proposers"][0]
    assert result["status"] == "dry_run"
    assert result["batch_request"]["schema_version"] == BATCH_SCHEMA_VERSION
    assert result["batch_request"]["inputs"] == []
    assert result["workspace_inputs"] == [
        {"input_id": "domain-markdown-001", "media_type": "text/markdown"}
    ]
    assert "# Brief" not in json.dumps(result["batch_request"])
    assert not (tmp_path / "project" / ".arc" / "ideas" / "runs").exists()
    assert set(worker) == {"worker_id", "instructions", "output_schema", "model"}
    assert "shared paper cache" in worker["instructions"]
    assert "resolver" not in worker["instructions"].lower()
    assert result["batch_request"]["loops"][0]["context"][
        "exploration_profile"
    ]["profile_id"] == "domain_axis_001"


def test_single_domain_loops_receive_distinct_stable_summary_profiles(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    config = _config(tmp_path)
    config["loops_per_variant"] = 5

    first = runner.run_ideas(config, dry_run=True)
    second = runner.run_ideas(config, dry_run=True)
    first_profiles = [
        loop["context"]["exploration_profile"]
        for loop in first["batch_request"]["loops"]
    ]
    second_profiles = [
        loop["context"]["exploration_profile"]
        for loop in second["batch_request"]["loops"]
    ]

    assert first_profiles == second_profiles
    assert [profile["profile_id"] for profile in first_profiles] == [
        f"domain_axis_{index:03d}" for index in range(1, 6)
    ]
    assert len({profile["mission"] for profile in first_profiles}) == 5


def test_single_domain_explicit_profiles_require_exact_loop_count(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    config = _config(tmp_path)
    config["loops_per_variant"] = 2
    config["exploration_profiles"] = [
        {"profile_id": "only_one", "mission": "Explore one route."}
    ]

    try:
        runner.run_ideas(config, dry_run=True)
    except ValueError as exc:
        assert "exactly one profile per loop" in str(exc)
    else:
        raise AssertionError("mismatched single-domain profiles must fail")


def test_single_domain_explicit_profiles_are_used_in_order(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    config = _config(tmp_path)
    config["loops_per_variant"] = 2
    config["exploration_profiles"] = [
        {"profile_id": "route_a", "mission": "Explore route A."},
        {"profile_id": "route_b", "mission": "Explore route B."},
    ]

    result = runner.run_ideas(config, dry_run=True)

    assert [
        loop["context"]["exploration_profile"]["profile_id"]
        for loop in result["batch_request"]["loops"]
    ] == ["route_a", "route_b"]


def test_explicit_profiles_reject_duplicate_missions_with_distinct_ids(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    config = _config(tmp_path)
    config["loops_per_variant"] = 2
    config["exploration_profiles"] = [
        {"profile_id": "route_a", "mission": "Explore a controlled limit."},
        {
            "profile_id": "route_b",
            "mission": "  explore   A CONTROLLED limit. ",
        },
    ]

    try:
        runner.run_ideas(config, dry_run=True)
    except ValueError as exc:
        assert "duplicate mission text" in str(exc)
    else:
        raise AssertionError("duplicate explicit profile missions must fail")


def test_single_domain_uses_general_lenses_and_rejects_excess_loops(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    config = _config(tmp_path)
    summary_path = (
        tmp_path
        / "project"
        / ".arc"
        / "domain"
        / "packages"
        / "single.json"
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["open_axes_for_new_work"] = []
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    config["loops_per_variant"] = 5

    result = runner.run_ideas(config, dry_run=True)

    assert [
        loop["context"]["exploration_profile"]["profile_id"]
        for loop in result["batch_request"]["loops"]
    ] == [
        "general_controlled_limit",
        "general_symmetry_consistency",
        "general_observable_discriminator",
        "general_approximation_boundary",
        "general_validation_bridge",
    ]

    config["loops_per_variant"] = 6
    try:
        runner.run_ideas(config, dry_run=True)
    except ValueError as exc:
        assert "Automatic single-domain exploration profiles are insufficient" in str(
            exc
        )
    else:
        raise AssertionError("automatic profiles must not be reused by ID")


def test_dry_run_does_not_read_workspace_input_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = _load_runner_module()
    config = _config(tmp_path)
    source = tmp_path / "project" / ".arc" / "domain" / "packages" / "brief.md"
    original_read_bytes = Path.read_bytes

    def fail_if_workspace_source(path: Path) -> bytes:
        if path == source:
            raise AssertionError("dry run must not read workspace input bytes")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_if_workspace_source)

    result = runner.run_ideas(config, dry_run=True)

    assert result["status"] == "dry_run"


def test_run_uses_one_explicit_runtime_carrier(tmp_path: Path) -> None:
    runner = _load_runner_module()
    fake = _FakeLLM()
    runtime = LLMExecutionOptions(host_authority=HostAuthority.RESTRICTED)
    result = runner.run_ideas(_config(tmp_path), llm_service=fake, llm_options=runtime)
    assert result["status"] == "succeeded"
    assert "evidence" not in result
    assert fake.options and all(item is runtime for item in fake.options)
    assert fake.requests and all(len(request.inputs) == 1 for request in fake.requests)
    source = fake.requests[0].inputs[0].source
    assert source.source_run_id == "ideas-test"
    assert source.source_artifact_id.endswith("domain-markdown-001")
    assert "# Brief" not in fake.requests[0].prompt


def test_custom_executor_receives_persisted_materialized_input(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    fake = _FakeLLM()
    captured: dict[str, object] = {}

    def execute(repository, spec, handler, *, event_sink):
        request = decode_batch_request(spec.semantic_input)
        captured["request"] = request
        captured["content"] = ImmutableArtifactStore(
            repository.run_directory(spec.run_id),
            repository_root=repository.root,
        ).read_source(request.inputs[0].source).content
        captured["event_sink"] = event_sink
        return RunEngine(repository).execute(
            spec,
            handler,
            event_sink=event_sink,
        )

    progress_events: list[dict[str, Any]] = []
    result = runner.run_ideas(
        _config(tmp_path),
        llm_service=fake,
        executor=execute,
        progress_callback=progress_events.append,
    )

    request = captured["request"]
    assert result["status"] == "succeeded"
    assert getattr(request, "inputs")[0].source.source_run_id == "ideas-test"
    assert captured["content"] == b"# Brief\n"
    assert captured["event_sink"] is not None
    assert "proposer_reviewer_worker_started" in {
        event["event"] for event in progress_events
    }


def test_idea_cli_requires_explicit_authority_value() -> None:
    runner = _load_runner_module()
    parsed = runner._build_parser().parse_args([
        "--config", "config.json", "--host-authority", "restricted"
    ])
    assert parsed.host_authority == "restricted"


def test_partial_delivery_is_automatic_for_verified_committed_rounds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = _load_runner_module()
    config = runner.load_ideas_config(_config(tmp_path))
    result = {
        "status": "paused",
        "batch": {
            "trace_verified": True,
            "rankable_loop_count": 0,
        },
        "loops": [{"committed_rounds": 1}],
    }
    warnings: list[str] = []
    monkeypatch.setattr(
        runner,
        "rank_run",
        lambda _root, _run_id, *, mode: {"mode": mode, "ranking": []},
    )
    monkeypatch.setattr(
        runner,
        "publish_ideas_pdf",
        lambda **kwargs: {
            "mode": kwargs["mode"],
            "artifacts": ["archive.pdf", "latest.pdf"],
        },
    )

    runner._maybe_publish_partial(config, result, warnings)

    assert result["status"] == "paused"
    assert result["partial_delivery"]["mode"] == "partial"
    assert warnings == []


def test_partial_delivery_failure_only_adds_warning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = _load_runner_module()
    config = runner.load_ideas_config(_config(tmp_path))
    result = {
        "status": "paused",
        "batch": {
            "trace_verified": True,
            "rankable_loop_count": 0,
        },
        "loops": [{"committed_rounds": 1}],
    }
    warnings: list[str] = []
    monkeypatch.setattr(
        runner,
        "rank_run",
        lambda _root, _run_id, *, mode: {"mode": mode, "ranking": []},
    )

    def fail_publish(**_kwargs):
        raise RuntimeError("renderer unavailable")

    monkeypatch.setattr(runner, "publish_ideas_pdf", fail_publish)

    runner._maybe_publish_partial(config, result, warnings)

    assert result == {
        "status": "paused",
        "batch": {
            "trace_verified": True,
            "rankable_loop_count": 0,
        },
        "loops": [{"committed_rounds": 1}],
    }
    assert warnings == ["partial_ideas_delivery_failed: RuntimeError"]


def test_ideas_template_modules_have_one_way_dependencies() -> None:
    templates = (IDEAS_MODULES / "ideas_templates.py").read_text(
        encoding="utf-8"
    )
    models = (IDEAS_MODULES / "ideas_models.py").read_text(encoding="utf-8")
    template_io = (IDEAS_MODULES / "ideas_template_io.py").read_text(
        encoding="utf-8"
    )
    workers = (IDEAS_MODULES / "ideas_worker_templates.py").read_text(
        encoding="utf-8"
    )
    context = (IDEAS_MODULES / "ideas_context.py").read_text(encoding="utf-8")

    assert len(templates.splitlines()) <= 100
    assert len(models.splitlines()) <= 50
    assert len(template_io.splitlines()) <= 120
    assert len(workers.splitlines()) <= 150
    assert len(context.splitlines()) <= 400
    assert "_arc_workflows.ideas_templates import" not in models
    assert "_arc_workflows.ideas_templates import" not in template_io
    assert "_arc_workflows.ideas_templates import" not in workers
    assert "_arc_workflows.ideas_templates import" not in context
    assert "_arc_workflows.ideas_models import" in workers
    assert "_arc_workflows.ideas_models import" in context
    assert "_arc_workflows.ideas_template_io import" in workers
    assert "_arc_workflows.ideas_template_io import" in context
    assert "_arc_workflows.ideas_worker_templates import" in templates
    assert "_arc_workflows.ideas_context import" in templates
