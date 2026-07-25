from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from arc_llm import InteractionRequest, InteractionResponse, LLMCompleted


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_JSON = ROOT / "plugins/arc/skills/arc/workflows/json"
SCRIPTS = ROOT / "plugins/arc/skills/arc/scripts"
RUNNER = SCRIPTS / "run-ideas.py"

sys.path.insert(0, str(SCRIPTS))
try:
    from _arc_workflows.evidence import EVIDENCE_OPERATION_NAMES
    from _arc_workflows.ideas_config import ConfigError as IdeasConfigError
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
    return {
        "schema_version": "arc.workflow.ideas.config.v1",
        "run_id": "ideas-test",
        "run_dir": str(project / "ideas"),
        "project_dir": str(project),
        "user_intent": "Find a controlled calculation.",
        "variant_config_dir": str(WORKFLOW_JSON),
        "variant_glob": "ideas-domain.variant.json",
        "loops_per_variant": loops,
    }


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
    request_limit = 24

    def __init__(self) -> None:
        self.request_count = 0
        self.records: list[dict[str, Any]] = []
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.fetch_count = 0
        self._lock = threading.Lock()

    def resolve(self, request: InteractionRequest) -> InteractionResponse:
        with self._lock:
            self.request_count += 1
            key = (request.operation, str(request.arguments["arxiv_id"]))
            if key not in self._cache:
                self.fetch_count += 1
                self._cache[key] = {"cached": True}
            self.records.append({"operation_id": request.operation, "parameters": dict(request.arguments)})
            result = self._cache[key]
        return InteractionResponse(request.request_id, result=result)


def test_dry_run_materializes_public_typed_request_with_three_rounds(tmp_path: Path) -> None:
    runner = _load_runner_module()

    result = runner.run_ideas(_single_domain_config(tmp_path), dry_run=True)

    request = result["batch_request"]
    assert result["status"] == "dry_run"
    assert request["schema_version"] == "arc.proposer_reviewer.batch.v1"
    assert request["loops"][0]["max_rounds"] == 3
    proposer = request["loops"][0]["proposers"][0]
    reviewer = request["loops"][0]["reviewer"]
    assert proposer["max_interaction_turns"] == 2
    assert reviewer["max_interaction_turns"] == 2
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
    assert observed["spec"].handler == "arc.proposer_reviewer.batch.v1"
    assert observed["handler"].options.max_concurrent_loops == 1
    assert len(llm.calls) == 6
    assert result["loops"] == [
        {
            "idea_id": "domain/idea_001",
            "variant_id": "domain",
            "idea_index": 1,
            "loop_id": "domain_idea_001",
            "lifecycle": "succeeded",
            "phase": "completed",
            "current_round": None,
            "rounds_completed": 3,
            "committed_rounds": 3,
            "integrity_error": None,
        }
    ]
    table = result["round_score_table"]
    assert table["source"] == "committed_trace"
    assert table["rows"][0]["total_scores_by_round"] == {"1": 76, "2": 77, "3": 78}
    assert table["rows"][0]["final_title"] == "controlled idea 5"
    assert "loops/" not in json.dumps(result)
    assert result["batch_request_artifact_id"] == "proposer-reviewer/request"
    ranking = ranker.rank_run(Path(result["run_root"]), result["run_id"])
    assert ranking["run_id"] == result["run_id"]
    assert ranking["run_lifecycle"] == "succeeded"


def test_no_info_disables_evidence_and_cross_domain_keeps_structured_context(tmp_path: Path) -> None:
    runner = _load_runner_module()
    workflow = tmp_path / "workflow"
    shutil.copytree(WORKFLOW_JSON, workflow)
    no_info = json.loads((workflow / "ideas-no-info.variant.json").read_text(encoding="utf-8"))
    no_info["enabled"] = True
    (workflow / "ideas-no-info.variant.json").write_text(json.dumps(no_info), encoding="utf-8")
    project = tmp_path / "no-info-project"
    no_info_result = runner.run_ideas(
        {
            **_single_domain_config(tmp_path),
            "run_id": "no-info",
            "project_dir": str(project),
            "run_dir": str(project / "ideas"),
            "variant_config_dir": str(workflow),
            "variant_glob": "ideas-no-info.variant.json",
        },
        dry_run=True,
    )
    no_info_loop = no_info_result["batch_request"]["loops"][0]
    assert "interaction_operations" not in no_info_loop["proposers"][0]
    assert "controller_evidence_operations" not in no_info_loop["context"]
    assert "review_payload" not in no_info_loop["reviewer"]["output_schema"]["properties"]

    cross_project = tmp_path / "cross-project"
    _write_cross_domain_manifest(cross_project)
    cross_result = runner.run_ideas(
        {
            "schema_version": "arc.workflow.ideas.config.v1",
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
            "schema_version": "arc.workflow.ideas.config.v1",
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
                "schema_version": "arc.workflow.ideas.config.v1",
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


def test_one_shared_evidence_resolver_enforces_allowlist_budget_and_cache_reuse(tmp_path: Path) -> None:
    runner = _load_runner_module()
    llm = _ThreeRoundLLM(interaction=True)
    resolver = _CachingResolver()

    result = runner.run_ideas(
        _single_domain_config(tmp_path, loops=2),
        llm_service=llm,  # type: ignore[arg-type]
        evidence_resolver=resolver,  # type: ignore[arg-type]
    )

    assert result["status"] == "succeeded"
    assert resolver.request_limit == 24
    assert resolver.request_count == 6
    assert resolver.fetch_count == 1
    assert {record["operation_id"] for record in resolver.records} == {"get-arxiv-table-of-contents"}
    assert result["evidence"]["request_count"] == 6


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
            json.dumps({"schema_version": "arc.domain_summary.v4", "domain_id": domain_id}),
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
    (domain / "domain-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "arc.workflow.domain_manifest.v2",
                "research_scope": "cross_domain",
                "package_count": 2,
                "field_count": 2,
                "grouping_artifact": "field-grouping.json",
                "domain_packages": [
                    {"domain_package_id": "a", "summary_json_path": "domain/a.json"},
                    {"domain_package_id": "b", "summary_json_path": "domain/b.json"},
                ],
                "field_groups": groups,
            }
        ),
        encoding="utf-8",
    )
