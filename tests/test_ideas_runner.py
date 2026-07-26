from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from arc_jobs import canonical_json_bytes
from arc_llm import HostAuthority, LLMCompleted, LLMExecutionOptions
from arc_proposer_reviewer.models import BATCH_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_JSON = ROOT / "plugins/arc/skills/arc/workflows/json"
SCRIPTS = ROOT / "plugins/arc/skills/arc/scripts"
RUNNER = SCRIPTS / "run-ideas.py"


def _load_runner_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("run_ideas", RUNNER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_ideas"] = module
        spec.loader.exec_module(module)
        return module
    finally:
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
                        "user_intent_relevance": 20,
                        "novelty": 10,
                        "confidence_of_novelty": 10,
                        "scientific_value": 15,
                        "planning": 15,
                        "problem_well_definedness": 15,
                        "total_score": 85,
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


def test_idea_cli_requires_explicit_authority_value() -> None:
    runner = _load_runner_module()
    parsed = runner._build_parser().parse_args([
        "--config", "config.json", "--host-authority", "restricted"
    ])
    assert parsed.host_authority == "restricted"
