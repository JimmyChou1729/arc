from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from arc_jobs import canonical_json_bytes
from arc_llm import (
    InteractionRequest,
    InteractionResponse,
    InvalidRequestError,
    LLMCompleted,
    LLMFailed,
)
from arc_proposer_reviewer import ProposerReviewerHandler
from arc_proposer_reviewer.models import BATCH_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_JSON = ROOT / "plugins/arc/skills/arc/workflows/json"
SCRIPTS = ROOT / "plugins/arc/skills/arc/scripts"
RUNNER = SCRIPTS / "run-ideas.py"

sys.path.insert(0, str(SCRIPTS))
try:
    from _arc_workflows.evidence import EVIDENCE_OPERATION_NAMES
    from _arc_workflows.ideas_config import (
        ConfigError as IdeasConfigError,
        load_ideas_config,
    )
    from _arc_workflows.ideas_templates import read_json as read_ideas_json
finally:
    sys.path.remove(str(SCRIPTS))


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


def _load_ranker_module():
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPTS))
    try:
        path = SCRIPTS / "rank-ideas.py"
        spec = importlib.util.spec_from_file_location("rank_ideas", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["rank_ideas"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        sys.path.remove(str(SCRIPTS))


def _single_domain_config(tmp_path: Path, *, loops: int = 1) -> dict[str, Any]:
    project = tmp_path / "project"
    (project / "domain").mkdir(parents=True)
    (project / "domain" / "brief.md").write_text("# Brief\n", encoding="utf-8")
    _write_single_domain_manifest(project)
    return {
        "schema_version": "arc.workflow.ideas.config.v2",
        "run_id": "ideas-test",
        "run_dir": str(project / "ideas"),
        "project_dir": str(project),
        "user_intent": "Find a controlled calculation.",
        "variant_config_dir": str(WORKFLOW_JSON),
        "variant_glob": "ideas-domain.variant.json",
        "loops_per_variant": loops,
    }


def _write_single_domain_manifest(project: Path) -> None:
    domain = project / "domain"
    seed = "arXiv:2401.00001"
    package_id = "single"
    provenance = {
        "schema_version": "arc.workflow.domain_seed_provenance.v1",
        "requested_seed_mappings": [
            {
                "requested_seed": seed,
                "build_seed": seed,
                "domain_id": package_id,
                "resolution": "explicit_seed",
            }
        ],
        "build_origins": [
            {
                "domain_id": package_id,
                "build_seed": seed,
                "origin_selection": {
                    "mode": "explicit_seed",
                    "requested_seed": seed,
                },
            }
        ],
        "deduplications": [],
    }
    (domain / "seed-provenance.json").write_text(
        json.dumps(provenance), encoding="utf-8"
    )
    field_card = {
        "seed_papers": [seed],
        "summary_json_paths": ["domain/single.json"],
        "summary_markdown_paths": ["domain/brief.md"],
        "paper_json_pack_paths": ["domain/single-papers.json"],
        "task_focus": {},
        "methodology": [],
    }
    groups = [
        {
            "field_id": "field-single",
            "domain_package_ids": [package_id],
            "field_card": field_card,
        }
    ]
    (domain / "field-grouping.json").write_text(
        json.dumps(
            {
                "schema_version": (
                    "arc.workflow.domain_field_grouping.v1"
                ),
                "field_groups": groups,
            }
        ),
        encoding="utf-8",
    )
    (domain / "domain-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "arc.workflow.domain_manifest.v3",
                "research_scope": "single_domain",
                "requested_seed_papers": [seed],
                "seed_provenance_artifact": {
                    "path": "domain/seed-provenance.json",
                    "sha256": hashlib.sha256(
                        canonical_json_bytes(provenance)
                    ).hexdigest(),
                    "schema_version": (
                        "arc.workflow.domain_seed_provenance.v1"
                    ),
                },
                "package_count": 1,
                "field_count": 1,
                "grouping_artifact": "domain/field-grouping.json",
                "domain_packages": [
                    {
                        "domain_package_id": package_id,
                        "seed_paper": seed,
                        "summary_json_path": "domain/single.json",
                    }
                ],
                "field_groups": groups,
                "grouping_warnings": [],
            }
        ),
        encoding="utf-8",
    )


class _ThreeRoundLLM:
    def __init__(self, *, interaction: bool = False) -> None:
        self.calls: list[Any] = []
        self.interaction = interaction

    def execute(self, _context: Any, request: Any, *, options: Any) -> LLMCompleted:
        self.calls.append((request, options))
        if "one proposer" in request.prompt:
            if self.interaction:
                resolver = options.interaction_resolver
                assert resolver is not None
                resolver.resolve(
                    InteractionRequest(
                        request_id=f"evidence-{len(self.calls)}",
                        operation="get-arxiv-table-of-contents",
                        arguments={"arxiv_id": "arXiv:0911.3380"},
                    )
                )
            return LLMCompleted(
                {
                    "title": f"controlled idea {len(self.calls)}",
                    "idea_summary": "summary",
                    "motivation": "motivation",
                    "novelty_checks": [],
                    "calculation_plan": "plan",
                    "validation_checks": [],
                    "risks": [],
                },
                "fake",
                "fake-model",
                None,
                None,
            )
        feedback = request.output.result_schema["properties"]["feedback"]["required"]
        return LLMCompleted(
            {
                "schema_version": "arc.proposer_reviewer.review.v1",
                "action": "continue",
                "reason": "Refine the proposal.",
                "feedback": {worker_id: "Make the first calculation sharper." for worker_id in feedback},
                "payload": {
                    "evidence_checked": [
                        "arXiv:0911.3380 table of contents"
                    ],
                    "tool_queries_used": [
                        "get-arxiv-table-of-contents arXiv:0911.3380"
                    ],
                    "marks": {
                        "user_intent_relevance": 20,
                        "novelty": 10,
                        "confidence_of_novelty": 10,
                        "scientific_value": 15,
                        "planning": 15,
                        "problem_well_definedness": 15,
                        "total_score": 75 + len(self.calls) // 2,
                    }
                },
            },
            "fake",
            "fake-model",
            None,
            None,
        )


class _CachingResolver:
    def __init__(self) -> None:
        self.request_count = 0
        self.records: list[dict[str, Any]] = []
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.fetch_count = 0
        self._lock = threading.Lock()

    def resolve(self, request: InteractionRequest) -> InteractionResponse:
        with self._lock:
            self.request_count += 1
            request_number = self.request_count
            key = (request.operation, str(request.arguments["arxiv_id"]))
            if key not in self._cache:
                self.fetch_count += 1
                self._cache[key] = {"cached": True}
            result = self._cache[key]
            self.records.append(
                {
                    "request_number": request_number,
                    "operation_id": request.operation,
                    "parameters": dict(request.arguments),
                    "ok": True,
                    "result": result,
                    "error": None,
                }
            )
        return InteractionResponse(request.request_id, result=result)


class _FailingResolver:
    def __init__(self) -> None:
        self.request_count = 0
        self.records: list[dict[str, Any]] = []

    def resolve(self, request: InteractionRequest) -> InteractionResponse:
        self.request_count += 1
        error = {
            "code": "evidence_operation_failed",
            "message": "fixture lookup failed",
        }
        self.records.append(
            {
                "request_number": self.request_count,
                "operation_id": request.operation,
                "parameters": dict(request.arguments),
                "ok": False,
                "result": None,
                "error": error,
            }
        )
        return InteractionResponse(request.request_id, error=error)


class _SelectiveFailureLLM(_ThreeRoundLLM):
    def __init__(self, failed_loops: set[str]) -> None:
        super().__init__()
        self.failed_loops = failed_loops

    def execute(self, context: Any, request: Any, *, options: Any) -> Any:
        task = json.loads(request.prompt.rsplit("## Round task\n", 1)[1])
        if (
            "one proposer" in request.prompt
            and task["loop_id"] in self.failed_loops
        ):
            return LLMFailed(
                InvalidRequestError("deliberate proposer failure")
            )
        return super().execute(context, request, options=options)


def test_dry_run_materializes_public_typed_request_with_three_rounds(tmp_path: Path) -> None:
    runner = _load_runner_module()

    result = runner.run_ideas(_single_domain_config(tmp_path), dry_run=True)

    request = result["batch_request"]
    assert result["status"] == "dry_run"
    assert request["schema_version"] == BATCH_SCHEMA_VERSION
    assert request["loops"][0]["max_rounds"] == 3
    proposer = request["loops"][0]["proposers"][0]
    reviewer = request["loops"][0]["reviewer"]
    assert "max_interaction_turns" not in proposer
    assert "max_interaction_turns" not in reviewer
    assert set(proposer["interaction_operations"]) == set(EVIDENCE_OPERATION_NAMES)
    assert proposer["capabilities"] == {
        "internet": True,
        "inherit_host_config": False,
        "allowed_tools": [],
    }
    assert "review_payload" not in reviewer["output_schema"]["properties"]
    assert {"schema_version", "controller", "proposer_messages"}.isdisjoint(
        reviewer["output_schema"]["properties"]
    )
    assert "marks" in reviewer["output_schema"]["properties"]


def test_reviewer_schema_templates_describe_direct_worker_payloads() -> None:
    legacy_fields = {"schema_version", "controller", "proposer_messages", "review_payload"}

    for name in (
        "ideas-reviewer-output.schema.json",
        "ideas-domain-reviewer-output.schema.json",
        "ideas-cross-domain-reviewer-output.schema.json",
    ):
        schema = json.loads((WORKFLOW_JSON / name).read_text(encoding="utf-8"))
        assert "marks" in schema["properties"]
        assert legacy_fields.isdisjoint(schema["properties"])
        assert "arc.llm.review_envelope.v1" not in json.dumps(schema)


def test_evidence_enabled_idea_prompts_describe_typed_resolver_boundary() -> None:
    names = (
        "ideas-loop.template.json",
        "ideas-cross-domain-loop.template.json",
        "ideas-proposer.template.json",
        "ideas-cross-domain-proposer.template.json",
        "ideas-reviewer.template.json",
        "ideas-domain-reviewer.template.json",
        "ideas-cross-domain-reviewer.template.json",
        "ideas-domain.variant.json",
    )
    text = "\n".join(
        (WORKFLOW_JSON / name).read_text(encoding="utf-8")
        for name in names
    ).lower()

    assert "resolver-supplied" in text
    assert "typed operation" in text
    assert "typed resolver operations" in text
    assert "do not invoke arc clis, shell commands, or mcp tools" in text
    assert "controller-supplied" not in text
    assert "controller resolution" not in text
    assert "controller-mediated" not in text
    assert "arc-paper cli/service" not in text
    assert "search-cached-full-text" in text
    assert "several concrete multiword synonyms" in text
    assert "repeated --term values in one call" in text
    assert "at most 50 paper titles, never summaries or abstracts" in text
    assert "complementary discovery surfaces with no fixed order" in text
    assert "after shortlisting an arc-resolvable paper" in text
    for name in (
        "ideas-loop.template.json",
        "ideas-cross-domain-loop.template.json",
    ):
        notes = json.loads(
            (WORKFLOW_JSON / name).read_text(encoding="utf-8")
        )["caller_context"]["arc_paper_tool_notes"].lower()
        assert "search-cached-full-text" in notes
        assert "several concrete multiword synonyms" in notes
        assert "repeated --term values in one call" in notes
        assert "at most 50 paper titles, never summaries or abstracts" in notes
        assert "complementary discovery surfaces with no fixed order" in notes
        assert "after shortlisting an arc-resolvable paper" in notes
        for operation in (
            "get-metadata",
            "get-arxiv-table-of-contents",
            "get-arxiv-section",
            "search-arxiv-full-text",
            "search-arxiv-equations",
            "get-references",
            "get-citers",
        ):
            assert operation in notes


def test_single_domain_ideas_make_interdisciplinary_transfer_optional() -> None:
    proposer = json.loads(
        (WORKFLOW_JSON / "ideas-proposer.template.json").read_text(
            encoding="utf-8"
        )
    )["prompt"]["template"].lower()
    reviewer = json.loads(
        (WORKFLOW_JSON / "ideas-domain-reviewer.template.json").read_text(
            encoding="utf-8"
        )
    )["prompt"]["template"].lower()
    text = f"{proposer}\n{reviewer}"

    assert "cross-disciplinary transfer is entirely optional" in proposer
    assert "no proposal, loop, or batch is required to include one" in proposer
    assert "there is no interdisciplinary quota" in proposer
    assert "receives no preference or reward" in proposer
    assert "a strong same-domain idea is equally eligible" in proposer
    assert "only when the proposal actually imports a method" in reviewer
    assert "set external_method_status to not_used" in reviewer
    assert "do not request cross-disciplinary evidence" in reviewer
    for forced_wording in (
        "at least one interdisciplinary",
        "must consider an interdisciplinary",
        "must include an interdisciplinary",
        "must propose an interdisciplinary",
    ):
        assert forced_wording not in text


def test_multi_field_variant_remains_explicit_cross_domain_work() -> None:
    proposer = json.loads(
        (WORKFLOW_JSON / "ideas-cross-domain-proposer.template.json").read_text(
            encoding="utf-8"
        )
    )["prompt"]["template"].lower()
    reviewer = json.loads(
        (WORKFLOW_JSON / "ideas-cross-domain-reviewer.template.json").read_text(
            encoding="utf-8"
        )
    )["prompt"]["template"].lower()

    assert "dedicated multi-field variant" in proposer
    assert "cross-domain transfer is explicitly in scope" in proposer
    assert "exactly one directed source-to-target idea" in proposer
    assert "dedicated multi-field variant" in reviewer
    assert "cross-domain transfer is explicitly in scope" in reviewer


def test_execution_uses_public_projection_for_three_committed_rounds_and_scores(tmp_path: Path) -> None:
    runner = _load_runner_module()
    ranker = _load_ranker_module()
    llm = _ThreeRoundLLM()
    observed: dict[str, Any] = {}

    def execute(repository: Any, spec: Any, handler: Any) -> Any:
        observed["spec"] = spec
        observed["handler"] = handler
        return runner.RunEngine(repository).execute(spec, handler)

    result = runner.run_ideas(  # type: ignore[arg-type]
        _single_domain_config(tmp_path),
        llm_service=llm,
        executor=execute,
    )

    assert result["status"] == "succeeded"
    assert observed["spec"].handler == ProposerReviewerHandler.name
    assert observed["handler"].options.max_concurrent_loops == 1
    assert len(llm.calls) == 6
    loop = result["loops"][0]
    assert loop["idea_id"] == "domain/idea_001"
    assert loop["variant_id"] == "domain"
    assert loop["idea_index"] == 1
    assert loop["loop_id"] == "domain_idea_001"
    assert loop["lifecycle"] == "succeeded"
    assert loop["phase"] == "completed"
    assert loop["current_round"] is None
    assert loop["rounds_completed"] == 3
    assert loop["committed_rounds"] == 3
    assert loop["active_workers"] == []
    assert isinstance(loop["last_activity_at"], str)
    assert loop["pause"] is None
    assert loop["failure"] is None
    assert loop["integrity_error"] is None
    assert result["batch"]["loop_lifecycle_counts"]["succeeded"] == 1
    assert sum(result["batch"]["loop_lifecycle_counts"].values()) == 1
    assert result["batch"]["rankable_loop_count"] == 1
    table = result["round_score_table"]
    assert table["source"] == "committed_trace"
    assert table["rows"][0]["total_scores_by_round"] == {"1": 76, "2": 77, "3": 78}
    assert table["rows"][0]["final_title"] == "controlled idea 5"
    assert "loops/" not in json.dumps(result)
    assert result["batch_request_artifact_id"] == "proposer-reviewer/request"
    ranking = ranker.rank_run(Path(result["run_root"]), result["run_id"])
    assert ranking["run_id"] == result["run_id"]
    assert ranking["schema_version"] == "arc.ideas.selected_rounds.v5"
    assert ranking["status"] == "succeeded"
    assert ranking["durable_lifecycle"] == "succeeded"
    assert "run_lifecycle" not in ranking


def test_no_info_disables_evidence_and_cross_domain_keeps_structured_context(tmp_path: Path) -> None:
    runner = _load_runner_module()
    workflow = tmp_path / "workflow"
    shutil.copytree(WORKFLOW_JSON, workflow)
    no_info = json.loads((workflow / "ideas-no-info.variant.json").read_text(encoding="utf-8"))
    no_info["enabled"] = True
    (workflow / "ideas-no-info.variant.json").write_text(json.dumps(no_info), encoding="utf-8")
    single_config = _single_domain_config(tmp_path)
    no_info_result = runner.run_ideas(
        {
            **single_config,
            "run_id": "no-info",
            "variant_config_dir": str(workflow),
            "variant_glob": "ideas-no-info.variant.json",
        },
        dry_run=True,
    )
    no_info_loop = no_info_result["batch_request"]["loops"][0]
    assert no_info_loop["proposers"][0]["interaction_operations"] == {}
    assert "max_interaction_turns" not in no_info_loop["proposers"][0]
    assert "controller_evidence_operations" not in no_info_loop["context"]
    assert "review_payload" not in no_info_loop["reviewer"]["output_schema"]["properties"]

    cross_project = tmp_path / "cross-project"
    _write_cross_domain_manifest(cross_project)
    cross_result = runner.run_ideas(
        {
            "schema_version": "arc.workflow.ideas.config.v2",
            "run_id": "cross",
            "run_dir": str(cross_project / "ideas"),
            "project_dir": str(cross_project),
            "user_intent": "Transfer a useful method.",
            "variant_config_dir": str(WORKFLOW_JSON),
            "variant_glob": "ideas-cross-domain.variant.json",
            "loops_per_variant": 1,
            "exploration_profiles": [{"profile_id": "forward", "mission": "Transfer A to B."}],
        },
        dry_run=True,
    )
    cross_loop = cross_result["batch_request"]["loops"][0]
    assert cross_loop["context"]["generation_mode"] == "cross_domain"
    assert [card["field_id"] for card in cross_loop["context"]["domain_cards"]] == ["field-a", "field-b"]
    assert cross_loop["context"]["exploration_profile"]["profile_id"] == "forward"
    assert set(cross_loop["proposers"][0]["interaction_operations"]) == set(
        EVIDENCE_OPERATION_NAMES
    )
    assert "review_payload" not in cross_loop["reviewer"]["output_schema"]["properties"]


def test_ideas_requires_current_manifest_and_bound_seed_provenance(
    tmp_path: Path,
) -> None:
    project = tmp_path / "cross-project"
    _write_cross_domain_manifest(project)
    config = {
        "schema_version": "arc.workflow.ideas.config.v2",
        "run_id": "current-provenance",
        "run_dir": str(project / "ideas"),
        "project_dir": str(project),
        "user_intent": "Transfer a useful method.",
        "variant_config_dir": str(WORKFLOW_JSON),
        "variant_glob": "ideas-cross-domain.variant.json",
        "loops_per_variant": 1,
        "exploration_profiles": [
            {
                "profile_id": "forward",
                "mission": "Transfer A to B.",
            }
        ],
    }

    loaded = load_ideas_config(config)
    assert loaded.domain_manifest is not None
    custom_manifest = (
        project / "handoffs/nested/current-domain.json"
    )
    custom_manifest.parent.mkdir(parents=True)
    custom_manifest.write_bytes(
        (project / "domain/domain-manifest.json").read_bytes()
    )
    custom_loaded = load_ideas_config(
        {
            **config,
            "domain_manifest_path": str(custom_manifest),
        }
    )
    assert custom_loaded.domain_manifest_path == custom_manifest
    escaped_manifest = json.loads(
        custom_manifest.read_text(encoding="utf-8")
    )
    escaped_manifest["seed_provenance_artifact"]["path"] = (
        "../../outside-provenance.json"
    )
    custom_manifest.write_text(
        json.dumps(escaped_manifest), encoding="utf-8"
    )
    with pytest.raises(
        IdeasConfigError,
        match="must stay inside project_dir",
    ):
        load_ideas_config(
            {
                **config,
                "domain_manifest_path": str(custom_manifest),
            }
        )

    provenance_path = project / "domain/seed-provenance.json"
    manifest_path = project / "domain/domain-manifest.json"
    original_provenance = provenance_path.read_bytes()
    original_manifest = manifest_path.read_bytes()
    provenance = json.loads(
        provenance_path.read_text(encoding="utf-8")
    )
    provenance["deduplications"].append(
        {
            "requested_seed": "changed",
            "kept_build_seed": "a",
            "domain_id": "a",
        }
    )
    provenance_path.write_text(
        json.dumps(provenance), encoding="utf-8"
    )
    with pytest.raises(
        IdeasConfigError,
        match="does not match manifest SHA-256",
    ):
        load_ideas_config(config)

    provenance_path.write_bytes(original_provenance)
    manifest_path.write_bytes(original_manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "arc.workflow.domain_manifest.v2"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(
        IdeasConfigError,
        match=(
            "schema_version must be "
            "arc.workflow.domain_manifest.v3"
        ),
    ):
        load_ideas_config(config)


def test_ideas_requires_manifest_for_default_single_domain_path(
    tmp_path: Path,
) -> None:
    config = _single_domain_config(tmp_path)
    missing_project = tmp_path / "missing-project"

    with pytest.raises(
        IdeasConfigError,
        match="domain_manifest_path does not exist",
    ):
        load_ideas_config(
            {
                **config,
                "project_dir": str(missing_project),
                "run_dir": str(missing_project / "ideas"),
            }
        )


def test_ideas_binds_package_seed_to_provenance_build_origin(
    tmp_path: Path,
) -> None:
    project = tmp_path / "cross-project"
    _write_cross_domain_manifest(project)
    manifest_path = project / "domain/domain-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["domain_packages"][0]["seed_paper"] = "arXiv:2401.00001"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        IdeasConfigError,
        match="must match seed provenance build origins",
    ):
        load_ideas_config(
            {
                "schema_version": "arc.workflow.ideas.config.v2",
                "run_id": "seed-mismatch",
                "run_dir": str(project / "ideas"),
                "project_dir": str(project),
                "user_intent": "Transfer a useful method.",
                "variant_config_dir": str(WORKFLOW_JSON),
                "variant_glob": "ideas-cross-domain.variant.json",
                "loops_per_variant": 1,
                "exploration_profiles": [
                    {
                        "profile_id": "forward",
                        "mission": "Transfer A to B.",
                    }
                ],
            }
        )


def test_cross_domain_cards_accept_v5_summaries_without_domain_id(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    project = tmp_path / "cross-project"
    _write_cross_domain_manifest(project)
    for domain_id in ("a", "b"):
        (project / f"domain/{domain_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": "arc.domain_summary.v5",
                    "mathematical_opportunities": {
                        "well_defined_problems": []
                    },
                }
            ),
            encoding="utf-8",
        )

    result = runner.run_ideas(
        {
            "schema_version": "arc.workflow.ideas.config.v2",
            "run_id": "cross-v5",
            "run_dir": str(project / "ideas"),
            "project_dir": str(project),
            "user_intent": "Transfer a useful method.",
            "variant_config_dir": str(WORKFLOW_JSON),
            "variant_glob": "ideas-cross-domain.variant.json",
            "loops_per_variant": 1,
            "exploration_profiles": [
                {"profile_id": "forward", "mission": "Transfer A to B."}
            ],
        },
        dry_run=True,
    )

    cards = result["batch_request"]["loops"][0]["context"]["domain_cards"]
    assert all(
        card["summary_capabilities"]["mathematical_opportunities"]
        for card in cards
    )


def test_cross_domain_cards_reject_v4_summaries(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    project = tmp_path / "cross-project"
    _write_cross_domain_manifest(project)
    (project / "domain/a.json").write_text(
        json.dumps(
            {
                "schema_version": "arc.domain_summary.v4",
                "domain_id": "a",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        IdeasConfigError,
        match="schema_version must be arc.domain_summary.v5",
    ):
        runner.run_ideas(
            {
                "schema_version": "arc.workflow.ideas.config.v2",
                "run_id": "cross-v4",
                "run_dir": str(project / "ideas"),
                "project_dir": str(project),
                "user_intent": "Transfer a useful method.",
                "variant_config_dir": str(WORKFLOW_JSON),
                "variant_glob": "ideas-cross-domain.variant.json",
                "loops_per_variant": 1,
                "exploration_profiles": [
                    {
                        "profile_id": "forward",
                        "mission": "Transfer A to B.",
                    }
                ],
            },
            dry_run=True,
        )


def test_cross_domain_cards_reject_domain_id_in_closed_v5_summary(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    project = tmp_path / "cross-project"
    _write_cross_domain_manifest(project)
    (project / "domain/a.json").write_text(
        json.dumps(
            {
                "schema_version": "arc.domain_summary.v5",
                "domain_id": "a",
                "mathematical_opportunities": {
                    "well_defined_problems": []
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        IdeasConfigError,
        match="arc.domain_summary.v5 must not contain domain_id",
    ):
        runner.run_ideas(
            {
                "schema_version": "arc.workflow.ideas.config.v2",
                "run_id": "cross-invalid-v5",
                "run_dir": str(project / "ideas"),
                "project_dir": str(project),
                "user_intent": "Transfer a useful method.",
                "variant_config_dir": str(WORKFLOW_JSON),
                "variant_glob": "ideas-cross-domain.variant.json",
                "loops_per_variant": 1,
                "exploration_profiles": [
                    {"profile_id": "forward", "mission": "Transfer A to B."}
                ],
            },
            dry_run=True,
        )


def test_one_shared_evidence_resolver_preserves_cache_reuse_without_cap(tmp_path: Path) -> None:
    runner = _load_runner_module()
    llm = _ThreeRoundLLM(interaction=True)
    resolver = _CachingResolver()

    result = runner.run_ideas(
        _single_domain_config(tmp_path, loops=2),
        llm_service=llm,  # type: ignore[arg-type]
        evidence_resolver=resolver,  # type: ignore[arg-type]
    )

    assert result["status"] == "succeeded"
    assert resolver.request_count == 6
    assert resolver.fetch_count == 1
    assert {record["operation_id"] for record in resolver.records} == {"get-arxiv-table-of-contents"}
    assert result["evidence"]["attempted"] == 6
    assert result["evidence"]["succeeded"] == 6
    assert result["evidence"]["failed"] == 0


def test_evidence_accounting_is_per_loop_without_a_global_cap(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    resolver = _CachingResolver()

    result = runner.run_ideas(
        _single_domain_config(tmp_path, loops=2),
        llm_service=_ThreeRoundLLM(interaction=True),  # type: ignore[arg-type]
        evidence_resolver=resolver,  # type: ignore[arg-type]
    )

    per_loop = result["evidence"]["per_loop"]
    assert result["evidence"]["observation_scope"] == "current_process"
    assert result["evidence"]["attempted"] == 6
    assert result["evidence"]["succeeded"] == 6
    assert result["evidence"]["failed"] == 0
    assert set(per_loop) == {"domain_idea_001", "domain_idea_002"}
    assert all(
        set(item)
        == {
            "attempted",
            "succeeded",
            "failed",
            "errors_by_code",
            "repeated_raw_signature",
        }
        for item in per_loop.values()
    )
    assert sum(item["attempted"] for item in per_loop.values()) == 6
    assert sum(item["succeeded"] for item in per_loop.values()) == 6
    assert sum(item["failed"] for item in per_loop.values()) == 0
    assert sum(item["repeated_raw_signature"] for item in per_loop.values()) == 5
    assert {item["attempted"] for item in per_loop.values()} == {3}


def test_evidence_failures_are_counted_and_warned_without_false_accounting(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()

    result = runner.run_ideas(
        _single_domain_config(tmp_path),
        llm_service=_ThreeRoundLLM(interaction=True),  # type: ignore[arg-type]
        evidence_resolver=_FailingResolver(),  # type: ignore[arg-type]
    )

    assert result["status"] == "succeeded"
    assert result["evidence"]["attempted"] == 3
    assert result["evidence"]["succeeded"] == 0
    assert result["evidence"]["failed"] == 3
    assert result["evidence"]["errors_by_code"] == {
        "evidence_operation_failed": 3
    }
    assert any(
        "NOVELTY SCOUTING INCOMPLETE" in warning
        for warning in result["warnings"]
    )


def test_execution_exception_reports_committed_durable_progress(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()

    def execute_then_raise(repository, spec, handler):
        runner.RunEngine(repository).execute(spec, handler)
        raise RuntimeError("fault after durable completion")

    result = runner.run_ideas(
        _single_domain_config(tmp_path),
        executor=execute_then_raise,
        llm_service=_ThreeRoundLLM(),  # type: ignore[arg-type]
    )

    assert result["status"] == "failed"
    assert result["schema_version"] == "arc.workflow.ideas.result.v3"
    assert result["execution_error"] == {
        "code": "ideas_batch_execution_failed",
        "exception_type": "RuntimeError",
        "message": "proposer-reviewer batch execution failed",
    }
    assert result["batch"]["durable_lifecycle"] == "succeeded"
    assert result["reviewer_call_count"] == 3
    assert "loop_reviewer_call_count" not in result
    assert "max_concurrent_proposal_calls" not in result
    assert result["loops"][0]["committed_rounds"] == 3


def test_partial_loop_failure_is_degraded_and_failed_loop_is_visible(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()

    result = runner.run_ideas(
        _single_domain_config(tmp_path, loops=2),
        llm_service=_SelectiveFailureLLM({"domain_idea_002"}),  # type: ignore[arg-type]
    )

    assert result["status"] == "degraded"
    assert result["batch"]["durable_lifecycle"] == "succeeded"
    assert result["batch"]["loop_lifecycle_counts"]["failed"] == 1
    assert result["batch"]["loop_lifecycle_counts"]["succeeded"] == 1
    assert sum(result["batch"]["loop_lifecycle_counts"].values()) == 2
    assert result["batch"]["rankable_loop_count"] == 1
    failed = next(
        loop for loop in result["loops"] if loop["lifecycle"] == "failed"
    )
    assert failed["failure"]["code"] == "proposer_failed"
    assert failed["failure"]["worker_causes"][0]["code"] == "invalid_request"
    assert any("IDEAS RUN DEGRADED" in item for item in result["warnings"])
    assert any("LOOP FAILURES" in item for item in result["warnings"])


def test_all_loop_failures_make_the_ideas_result_failed(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()

    result = runner.run_ideas(
        _single_domain_config(tmp_path, loops=2),
        llm_service=_SelectiveFailureLLM(  # type: ignore[arg-type]
            {"domain_idea_001", "domain_idea_002"}
        ),
    )

    assert result["status"] == "failed"
    assert result["batch"]["durable_lifecycle"] == "succeeded"
    assert result["batch"]["rankable_loop_count"] == 0
    assert result["batch"]["loop_lifecycle_counts"]["failed"] == 2
    assert sum(result["batch"]["loop_lifecycle_counts"].values()) == 2
    assert any("IDEAS RUN FAILED" in item for item in result["warnings"])


def test_unverified_trace_is_failed_and_has_no_rankable_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()

    def corrupt_trace(_repository: Any, _run_id: str) -> Any:
        raise OSError("sensitive/path/digest mismatch")

    monkeypatch.setattr(runner, "read_batch_trace", corrupt_trace)
    result = runner.run_ideas(
        _single_domain_config(tmp_path),
        llm_service=_ThreeRoundLLM(),  # type: ignore[arg-type]
    )

    assert result["status"] == "failed"
    assert result["batch"]["trace_verified"] is False
    assert result["batch"]["rankable_loop_count"] == 0
    assert any("IDEAS RUN FAILED" in item for item in result["warnings"])
    assert any(
        item.startswith("committed_trace_unavailable:")
        for item in result["warnings"]
    )


def test_unverified_committed_round_is_failed_not_rankable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()
    result_module = sys.modules["_arc_workflows.ideas_result"]

    def corrupt_round(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("sensitive/path/round digest mismatch")

    monkeypatch.setattr(result_module, "read_batch_round", corrupt_round)
    result = runner.run_ideas(
        _single_domain_config(tmp_path),
        llm_service=_ThreeRoundLLM(),  # type: ignore[arg-type]
    )

    assert result["status"] == "failed"
    assert result["batch"]["trace_verified"] is False
    assert result["batch"]["rankable_loop_count"] == 0
    assert result["reviewer_call_count"] == 0
    assert any(
        item.startswith("committed_round_unavailable:")
        for item in result["warnings"]
    )


def test_inspection_failure_is_explicit_not_not_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()

    def fail_inspection(_repository: Any, _run_id: str) -> Any:
        raise ValueError("projection unavailable")

    monkeypatch.setattr(runner, "inspect_batch", fail_inspection)
    result = runner.run_ideas(
        _single_domain_config(tmp_path),
        llm_service=_ThreeRoundLLM(),  # type: ignore[arg-type]
    )

    assert result["status"] == "failed"
    assert result["loops"][0]["lifecycle"] == "unknown"
    assert result["loops"][0]["phase"] == "inspection_failed"
    assert result["inspection_error"] == {
        "code": "ideas_batch_inspection_failed",
        "exception_type": "ValueError",
        "message": "proposer-reviewer batch inspection failed",
    }


def test_foreground_progress_is_ordered_and_filters_interaction_detail(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    events: list[dict[str, Any]] = []

    result = runner.run_ideas(
        _single_domain_config(tmp_path),
        llm_service=_ThreeRoundLLM(interaction=True),  # type: ignore[arg-type]
        evidence_resolver=_CachingResolver(),  # type: ignore[arg-type]
        progress_callback=events.append,
    )

    assert result["status"] == "succeeded"
    assert [item["sequence"] for item in events] == list(
        range(1, len(events) + 1)
    )
    assert all(item["run_id"] == "ideas-test" for item in events)
    assert all(isinstance(item["emitted_at"], str) for item in events)
    names = [item["event"] for item in events]
    assert names[0] == "ideas_batch_started"
    assert names[-1] == "ideas_batch_finished"
    assert "proposer_reviewer_worker_interaction" not in names
    assert set(names[1:-1]) <= runner._FOREGROUND_PROGRESS_EVENTS
    assert "proposer_reviewer_round_committed" in names


def test_progress_metadata_cannot_be_overwritten_by_an_event() -> None:
    runner = _load_runner_module()
    events: list[dict[str, Any]] = []
    emitter = runner.IdeasProgressEmitter(events.append, run_id="trusted-run")

    emitter.emit(
        {
            "event": "fixture",
            "sequence": 999,
            "emitted_at": "forged",
            "run_id": "forged-run",
        }
    )

    assert events[0]["sequence"] == 1
    assert events[0]["emitted_at"] != "forged"
    assert events[0]["run_id"] == "trusted-run"


def test_progress_callback_failure_does_not_fail_durable_execution(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    sidechannel = tmp_path / "progress/events.jsonl"

    def fail_progress(_event: dict[str, Any]) -> None:
        raise RuntimeError("display unavailable")

    result = runner.run_ideas(
        _single_domain_config(tmp_path),
        llm_service=_ThreeRoundLLM(),  # type: ignore[arg-type]
        progress_callback=fail_progress,
        base_env={"ARC_JOB_PROGRESS_FILE": str(sidechannel)},
    )

    assert result["status"] == "succeeded"
    assert (
        "ideas_progress_callback_failed: RuntimeError"
        in result["warnings"]
    )
    persisted = [
        json.loads(line)
        for line in sidechannel.read_text(encoding="utf-8").splitlines()
    ]
    assert persisted[0]["event"] == "ideas_batch_started"
    assert persisted[-1]["event"] == "ideas_batch_finished"


def test_duplicate_enabled_variant_ids_are_rejected(tmp_path: Path) -> None:
    runner = _load_runner_module()
    workflow = tmp_path / "workflow"
    shutil.copytree(WORKFLOW_JSON, workflow)
    source = json.loads(
        (workflow / "ideas-domain.variant.json").read_text(encoding="utf-8")
    )
    (workflow / "duplicate-a.variant.json").write_text(
        json.dumps(source),
        encoding="utf-8",
    )
    (workflow / "duplicate-b.variant.json").write_text(
        json.dumps(source),
        encoding="utf-8",
    )
    config = {
        **_single_domain_config(tmp_path),
        "variant_config_dir": str(workflow),
        "variant_glob": "duplicate-*.variant.json",
    }

    with pytest.raises(IdeasConfigError, match="duplicate enabled variant_id"):
        runner.run_ideas(config, dry_run=True)


@pytest.mark.parametrize("value", [False, True])
def test_artifact_options_is_rejected(
    tmp_path: Path,
    value: bool,
) -> None:
    runner = _load_runner_module()
    config = _single_domain_config(tmp_path)

    with pytest.raises(
        IdeasConfigError,
        match="ideas config contains unsupported fields: artifact_options",
    ):
        runner.run_ideas(
            {
                **config,
                "artifact_options": {"save_prompts": value},
            },
            dry_run=True,
        )


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_process_signal_requests_durable_batch_stop(
    tmp_path: Path,
    signum: signal.Signals,
) -> None:
    config = _single_domain_config(tmp_path)
    config_path = tmp_path / f"signal-{signum.name}.json"
    ready_path = tmp_path / f"signal-{signum.name}.ready"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    child = textwrap.dedent(
        """
        import importlib.util
        import sys
        import time
        from pathlib import Path

        script, config_path, ready_path = map(Path, sys.argv[1:4])
        sys.path.insert(0, str(script.parent))
        spec = importlib.util.spec_from_file_location("run_ideas_signal", script)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)

        def blocking_executor(repository, run_spec, _handler):
            class BlockingHandler:
                name = run_spec.handler

                def execute(self, context):
                    ready_path.write_text("ready", encoding="utf-8")
                    deadline = time.monotonic() + 10
                    while repository.inspect(run_spec.run_id).stop_request is None:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("stop request was not persisted")
                        time.sleep(0.01)
                    context.checkpoint()

            return module.RunEngine(repository).execute(
                run_spec,
                BlockingHandler(),
            )

        module._execute_batch = blocking_executor
        raise SystemExit(
            module.main(["--config", str(config_path), "--json"])
        )
        """
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child,
            str(RUNNER),
            str(config_path),
            str(ready_path),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while not ready_path.is_file() and process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            pytest.fail("signal subprocess did not enter its durable executor")
        time.sleep(0.01)
    os.kill(process.pid, signum)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 1, stderr
    result = json.loads(stdout)
    assert result["status"] == "paused"
    assert result["batch"]["trace_verified"] is True
    from arc_jobs import RunRepository

    view = RunRepository(config["run_dir"]).inspect(config["run_id"])
    assert view.stop_request is not None
    assert view.stop_request.reason == "run-ideas received a process signal"


def test_runner_has_no_retired_evidence_or_private_artifact_dependencies() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "arc_llm.evidence" not in source
    assert "arc_llm.proposers_reviewer" not in source
    assert "transcript.jsonl" not in source
    assert "rounds/" not in source


def test_json_reader_preserves_ideas_error_contract(tmp_path: Path) -> None:
    runner = _load_runner_module()
    path = tmp_path / "config.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(
        IdeasConfigError,
        match=f"JSON file must contain an object: {path}",
    ):
        read_ideas_json(path)


def _write_cross_domain_manifest(project: Path) -> None:
    domain = project / "domain"
    domain.mkdir(parents=True)
    for domain_id in ("a", "b"):
        (domain / f"{domain_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": "arc.domain_summary.v5",
                    "mathematical_opportunities": {
                        "well_defined_problems": []
                    },
                }
            ),
            encoding="utf-8",
        )
    field_cards = {
        "field-a": {
            "seed_papers": ["a"],
            "summary_json_paths": ["domain/a.json"],
            "summary_markdown_paths": ["domain/a.md"],
            "paper_json_pack_paths": ["domain/a-papers.json"],
            "task_focus": {"research_scope": "A"},
            "methodology": [],
        },
        "field-b": {
            "seed_papers": ["b"],
            "summary_json_paths": ["domain/b.json"],
            "summary_markdown_paths": ["domain/b.md"],
            "paper_json_pack_paths": ["domain/b-papers.json"],
            "task_focus": {"research_scope": "B"},
            "methodology": [],
        },
    }
    groups = [
        {"field_id": "field-a", "domain_package_ids": ["a"], "field_card": field_cards["field-a"]},
        {"field_id": "field-b", "domain_package_ids": ["b"], "field_card": field_cards["field-b"]},
    ]
    (domain / "field-grouping.json").write_text(
        json.dumps({"schema_version": "arc.workflow.domain_field_grouping.v1", "field_groups": groups}),
        encoding="utf-8",
    )
    provenance = {
        "schema_version": "arc.workflow.domain_seed_provenance.v1",
        "requested_seed_mappings": [
            {
                "requested_seed": domain_id,
                "build_seed": domain_id,
                "domain_id": domain_id,
                "resolution": "explicit_seed",
            }
            for domain_id in ("a", "b")
        ],
        "build_origins": [
            {
                "domain_id": domain_id,
                "build_seed": domain_id,
                "origin_selection": {
                    "mode": "explicit_seed",
                    "requested_seed": domain_id,
                },
            }
            for domain_id in ("a", "b")
        ],
        "deduplications": [],
    }
    provenance_path = domain / "seed-provenance.json"
    provenance_path.write_text(
        json.dumps(provenance),
        encoding="utf-8",
    )
    provenance_digest = hashlib.sha256(
        canonical_json_bytes(provenance)
    ).hexdigest()
    (domain / "domain-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "arc.workflow.domain_manifest.v3",
                "research_scope": "cross_domain",
                "requested_seed_papers": ["a", "b"],
                "seed_provenance_artifact": {
                    "path": "domain/seed-provenance.json",
                    "sha256": provenance_digest,
                    "schema_version": (
                        "arc.workflow.domain_seed_provenance.v1"
                    ),
                },
                "package_count": 2,
                "field_count": 2,
                "grouping_artifact": "domain/field-grouping.json",
                "domain_packages": [
                    {
                        "domain_package_id": "a",
                        "seed_paper": "a",
                        "summary_json_path": "domain/a.json",
                    },
                    {
                        "domain_package_id": "b",
                        "seed_paper": "b",
                        "summary_json_path": "domain/b.json",
                    },
                ],
                "field_groups": groups,
            }
        ),
        encoding="utf-8",
    )
