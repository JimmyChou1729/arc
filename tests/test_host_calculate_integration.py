from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from ac_llm import HostCoordinator, HostTaskService, LLMExecutionOptions
from ac_proposer_reviewer import BatchRunner

from tests.test_calculate_runner import load_calculate_modules, minimal_config, review


def calculator(worker_id: str) -> dict:
    return {"result_summary": f"independent fixture {worker_id}", "derivation": "Declared fixture derivation.",
            "assumptions": "Fixed offline fixture.", "validity_scope": "Wiring verification only.", "final_result": "$x$",
            "work_note_assessment": {"needs_revision": False, "issue_type": "none", "proposed_revision": None,
                                     "rationale": "Fixture has no required revision.", "can_continue_without_revision": True}}


def submit_task(service, location, task):
    binding = task["request"]["binding"]
    output = calculator(binding["worker_id"]) if binding["role"] == "proposer" else review()
    if binding["role"] == "reviewer":
        assert "independent fixture proposer_001" in task["request"]["task_prompt"]
        assert "independent fixture proposer_002" in task["request"]["task_prompt"]
    if task["request"]["output_contract"]["schema"].get("properties", {}).get("schema_version", {}).get("const") == "ac.llm.host_turn.v1":
        output = {"schema_version": "ac.llm.host_turn.v1", "state": "complete", "result": output, "host_request": None}
    value = {"schema_version": "ac.llm.host_response.v1", "task_id": task["task_id"],
             "request_sha256": task["request_sha256"], "actor": {"actor_id": binding["worker_id"],
             "kind": "fake", "context_id": f"context-{binding['role']}-{binding['worker_id']}"},
             "isolation": "fresh_context", "output": output}
    service.submit(**location, response=value)
    assert service.submit(**location, response=value)["reused"]


def test_two_calculators_and_referee_commit_through_original_paths(tmp_path):
    modules = load_calculate_modules()
    config = minimal_config(tmp_path, defaults={"provider": "host"})
    opts = LLMExecutionOptions(host_coordinator=HostCoordinator("offline", fresh_context=True))
    service = HostTaskService()
    submitted = []
    result = modules.runner.run_calculation(config, llm_options=opts)
    for _ in range(6):
        if result["status"] == "completed":
            break
        assert result["status"] == "awaiting_host", result
        step = result["steps"][0]
        assert len(step["attempts"]) == 1
        location = {key: step["resume"][key] for key in ("run_root", "run_id")}
        tasks = service.pending(**location)
        assert tasks
        for row in tasks:
            if row["status"] != "awaiting_host":
                continue
            task = service.export(**location, task_id=row["task_id"])
            if task["request"]["binding"]["role"] == "reviewer":
                assert len([entry for entry in submitted if entry[1] == "proposer"]) == 2
            submit_task(service, location, task)
            submitted.append((task["task_id"], task["request"]["binding"]["role"]))
        result = modules.runner.run_calculation(config, llm_options=opts)
    assert result["status"] == "completed", result
    assert result["steps"][0]["status"] == "accepted"
    assert len(submitted) == 3 and len({entry[0] for entry in submitted}) == 3
    attempt = result["steps"][0]["attempts"][0]
    root = Path(result["run_root"]) / "attempt-batches"
    projection = BatchRunner().projection(root, attempt["batch_run_id"])
    committed = projection.read_round(attempt["batch_loop_id"], 1)
    assert set(committed.proposals) == {"proposer_001", "proposer_002"}
    assert committed.review["payload"]["workflow_action"]["action"] == "continue"
    before = service.pending(run_root=root, run_id=attempt["batch_run_id"], include_completed=True)
    replay = modules.runner.run_calculation(config, llm_options=opts)
    assert replay["steps"][0]["accepted_output"] == result["steps"][0]["accepted_output"]
    assert service.pending(run_root=root, run_id=attempt["batch_run_id"], include_completed=True) == before
    assert len(before) == 3


def test_host_pause_preserves_outer_attempt_binding(tmp_path):
    modules = load_calculate_modules()
    config = minimal_config(tmp_path, defaults={"provider": "host"})
    opts = LLMExecutionOptions(host_coordinator=HostCoordinator("offline", fresh_context=True))
    first = modules.runner.run_calculation(config, llm_options=opts)
    second = modules.runner.run_calculation(config, llm_options=opts)
    assert first["status"] == second["status"] == "awaiting_host"
    assert first["steps"][0]["attempts"][0]["batch_run_id"] == second["steps"][0]["attempts"][0]["batch_run_id"]
    assert {task["task_id"] for task in first["steps"][0]["resume"]["host_tasks"]} == {
        task["task_id"] for task in second["steps"][0]["resume"]["host_tasks"]}


def test_calculation_does_not_auto_resume_a_stopped_host_batch(tmp_path):
    modules = load_calculate_modules()
    config = minimal_config(tmp_path, defaults={"provider": "host"})
    opts = LLMExecutionOptions(host_coordinator=HostCoordinator("offline", fresh_context=True))
    first = modules.runner.run_calculation(config, llm_options=opts)
    resume = first["steps"][0]["resume"]
    stopped = BatchRunner().stop(resume["run_root"], resume["run_id"], reason="explicit stop")
    next_result = modules.runner.run_calculation(config, llm_options=opts)
    assert next_result["status"] == "paused"
    assert next_result["steps"][0]["resume"]["stop_requested"]
    current = BatchRunner().inspect(resume["run_root"], resume["run_id"])
    assert current.snapshot.attempt == stopped.snapshot.attempt


def test_actual_restart_after_one_calculator_is_accepted(tmp_path):
    modules = load_calculate_modules()
    config = minimal_config(tmp_path, defaults={"provider": "host"})
    opts = LLMExecutionOptions(host_coordinator=HostCoordinator("offline", fresh_context=True))
    result = modules.runner.run_calculation(config, llm_options=opts, max_concurrent_calculators=1)
    location = {key: result["steps"][0]["resume"][key] for key in ("run_root", "run_id")}
    service = HostTaskService()
    first_task = service.export(**location, task_id=service.pending(**location)[0]["task_id"])
    submit_task(service, location, first_task)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    driver = '''
import json, sys, threading
from pathlib import Path
from tests.test_calculate_runner import load_calculate_modules
from ac_llm import HostCoordinator, LLMExecutionOptions
from ac_llm.providers.host import HostAdapter
original = HostAdapter.start
def interrupted(*args, **kwargs):
    print("second-calculator", flush=True)
    threading.Event().wait(30)
    return original(*args, **kwargs)
HostAdapter.start = interrupted
load_calculate_modules().runner.run_calculation(json.loads(Path(sys.argv[1]).read_text()),
    llm_options=LLMExecutionOptions(host_coordinator=HostCoordinator("offline", fresh_context=True)), max_concurrent_calculators=1)
'''
    process = subprocess.Popen([sys.executable, "-c", driver, str(config_path)], cwd=Path(__file__).resolve().parents[1],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    messages = queue.Queue()
    threading.Thread(target=lambda: messages.put(process.stdout.readline()), daemon=True).start()
    try:
        assert messages.get(timeout=10).strip() == "second-calculator"
        process.terminate()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
    assert service.pending(**location, include_completed=True)[0]["status"] == "completed"
    for _ in range(6):
        result = modules.runner.run_calculation(config, llm_options=opts, max_concurrent_calculators=1)
        if result["status"] == "completed":
            break
        assert result["status"] == "awaiting_host", result
        for row in service.pending(**location):
            if row["status"] == "awaiting_host":
                task = service.export(**location, task_id=row["task_id"])
                submit_task(service, location, task)
    assert result["status"] == "completed"
    rows = service.pending(**location, include_completed=True)
    assert len(rows) == 3
    assert first_task["task_id"] in {row["task_id"] for row in rows}
