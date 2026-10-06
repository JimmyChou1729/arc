from __future__ import annotations

import json
import sys
from pathlib import Path

from ac_jobs import RunRepository
from ac_llm import HostCoordinator, HostTaskService, LLMClient, LLMExecutionOptions, LLMPaused

from tests.test_domain_manifest import _write_domain, publish
from tests.test_ideas_runner import _config, _FakeLLM, _load_runner_module, _portfolio_value


def submit(service, location, task, output):
    contract = task["request"]["output_contract"]
    if contract["schema"].get("properties", {}).get("schema_version", {}).get("const") == "ac.llm.host_turn.v1":
        output = {"schema_version": "ac.llm.host_turn.v1", "state": "complete", "result": output, "host_request": None}
    return service.submit(**location, response={"schema_version": "ac.llm.host_response.v1",
        "task_id": task["task_id"], "request_sha256": task["request_sha256"],
        "actor": {"actor_id": "offline", "kind": "fake"}, "output": output})


def host_assessment(request, root, *, options):
    client = LLMClient()
    result = client.generate(request, run_root=root, options=options)
    if isinstance(result.outcome, LLMPaused) and RunRepository(root).inspect(result.snapshot.run_id).stop_request is None:
        result = client.resume(run_root=root, run_id=result.snapshot.run_id, options=options)
    return result


def relationship_project(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_LLM_HOST_COORDINATOR", json.dumps(HostCoordinator("offline").to_document()))
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(json.dumps({"user_intent": "bridge", "seed_paper_list": ["seed:a", "seed:b"]}))
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")
    return project


def test_relationships_pause_submit_resume_preserves_domain_cards(tmp_path, monkeypatch):
    project = relationship_project(tmp_path, monkeypatch)
    destination = publish.write_domain_manifest(project)
    first = json.loads(destination.read_text())
    assert first["domain_relationships"]["status"] == "paused"
    assert first["package_count"] == 2
    resume = first["domain_relationships"]["awaiting"]
    location = {key: resume[key] for key in ("run_root", "run_id")}
    service = HostTaskService()
    task = service.export(**location, task_id=resume["host_tasks"][0]["task_id"])
    submit(service, location, task, {"pairs": [{"package_a": "domain-a", "package_b": "domain-b",
        "classification": "uncertain", "confidence": 0.5, "reason": "offline fixture",
        "evidence": {"semantic": "fixture", "paper_overlap": "fixture", "citation_overlap": "fixture"}}]})
    second = json.loads(publish.write_domain_manifest(project).read_text())
    assert second["domain_relationships"]["status"] == "available"
    assert second["domain_packages"] == first["domain_packages"]
    assert len(service.pending(**location, include_completed=True)) == 1


def test_relationship_stop_survives_workflow_rerun(tmp_path, monkeypatch):
    project = relationship_project(tmp_path, monkeypatch)
    destination = publish.write_domain_manifest(project)
    first = json.loads(destination.read_text())["domain_relationships"]["awaiting"]
    repository = RunRepository(first["run_root"])
    repository.request_stop(first["run_id"], reason="fixture stop")
    attempt = repository.inspect(first["run_id"]).snapshot.attempt
    second = json.loads(publish.write_domain_manifest(project).read_text())
    assert second["domain_relationships"]["awaiting"]["stop_requested"] is True
    assert repository.inspect(first["run_id"]).snapshot.attempt == attempt


def test_portfolio_host_pause_is_exposed_and_reuses_research_batch(tmp_path):
    runner = _load_runner_module()
    config = _config(tmp_path)
    scientific = _FakeLLM()
    opts = LLMExecutionOptions(host_coordinator=HostCoordinator("offline"))

    first = runner.run_ideas(config, llm_service=scientific, portfolio_assessment_runner=host_assessment, llm_options=opts)
    assert first["status"] == "awaiting_host"
    assert first["research_status"] == "succeeded"
    resume = first["portfolio_assessment"]["resume"]
    location = {key: resume[key] for key in ("run_root", "run_id")}
    service = HostTaskService()
    task = service.export(**location, task_id=resume["host_tasks"][0]["task_id"])
    calls = list(scientific.requests)
    submit(service, location, task, _portfolio_value())
    second = runner.run_ideas(config, llm_service=scientific, portfolio_assessment_runner=host_assessment, llm_options=opts)
    assert second["status"] == "succeeded"
    assert second["portfolio_assessment"]["status"] == "available"
    assert scientific.requests == calls


def test_stopped_portfolio_is_paused_and_not_automatically_resumed(tmp_path, monkeypatch):
    runner = _load_runner_module()
    monkeypatch.setattr(sys.modules["_arc_workflows.ideas_portfolio_assessment"], "LLMClient", lambda **kwargs: LLMClient())
    config = _config(tmp_path)
    scientific = _FakeLLM()
    opts = LLMExecutionOptions(host_coordinator=HostCoordinator("offline"))
    first = runner.run_ideas(config, llm_service=scientific, llm_options=opts)
    location = {key: first["portfolio_assessment"]["resume"][key] for key in ("run_root", "run_id")}
    RunRepository(location["run_root"]).request_stop(location["run_id"], reason="test stop")
    second = runner.run_ideas(config, llm_service=scientific, llm_options=opts)
    assert second["status"] == "paused"
    assert second["research_status"] == "succeeded"
    assert second["portfolio_assessment"]["resume"]["stop_requested"] is True


def test_ideas_exposes_paused_domain_dependency_without_starting_batch(tmp_path):
    runner = _load_runner_module()
    config = _config(tmp_path, package_count=2)
    path = Path(config["project_dir"]) / ".arc/domain/domain-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["schema_version"] = "arc.workflow.domain_manifest.v5"
    manifest["domain_relationships"].update(status="paused", pair_classifications=[], awaiting={
        "run_root": "fixture", "run_id": "fixture", "details": {"code": "awaiting_host"},
        "stop_requested": False, "host_tasks": [{"task_id": "fixture"}],
    })
    path.write_text(json.dumps(manifest))
    scientific = _FakeLLM()
    result = runner.run_ideas(config, llm_service=scientific)
    assert result["status"] == "awaiting_host"
    assert result["pending_stages"] == ["domain_relationships"]
    assert result["resume"] == manifest["domain_relationships"]["awaiting"]
    assert scientific.requests == []
