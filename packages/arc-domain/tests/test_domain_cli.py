from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc_domain import cli
from arc_domain.catalog import publish_domain_result
from arc_domain.contracts import DomainBuildResult, decode_domain_build_result, encode_domain_build_result
from arc_domain.paths import DomainPaths
from arc_llm import ResumeAction, ResumeInput, resume_input_to_document
from arc_jobs import (
    Awaiting,
    CancelledError,
    CommandResult,
    CommandStatus,
    ImmutableArtifactStore,
    Paused,
    ResumeReason,
    RunEngine,
    RunRepository,
    RunSpec,
    RunStatus,
    Succeeded,
    command_result_json,
)


POLICY = {
    "schema_version": "arc.domain_build_policy.v1",
    "as_of_date": "2026-07-24",
    "recent_window_days": 365,
    "citer_pool_limit": 10,
    "ranked_paper_limit": 2,
    "graph_node_limit": 5,
}


class _SucceededDomainHandler:
    name = "arc.domain.build.v1"

    def __init__(self, domain_id: str) -> None:
        self.domain_id = domain_id

    def execute(self, context):
        artifacts = context.artifacts
        result = DomainBuildResult(
            domain_id=self.domain_id,
            foundation_selection=artifacts.publish_json("foundation", {"id": "foundation"}),
            graph=artifacts.publish_json(
                "graph", {"schema_version": "arc.domain_graph.v1", "nodes": []}
            ),
            network_html=artifacts.publish_bytes(
                "network-html", b"<html>graph</html>\n", media_type="text/html"
            ),
            paper_json_pack=artifacts.publish_json("paper-pack", {"papers": []}),
            evidence_pack=artifacts.publish_json("evidence-pack", {"evidence": []}),
            summary=artifacts.publish_json(
                "summary", {"schema_version": "arc.domain_summary.v1", "title": "Summary"}
            ),
            summary_markdown=artifacts.publish_bytes(
                "summary-markdown", b"# Summary\n", media_type="text/markdown"
            ),
        )
        result_ref = artifacts.publish_json("result", encode_domain_build_result(result))
        return Succeeded(result_ref)


class _PausedDomainHandler:
    name = "arc.domain.build.v1"

    def execute(self, context):
        request = context.artifacts.publish_json("resume-request", {"resume": True})
        return Paused(
            Awaiting(
                ResumeReason.EXTERNAL_CONDITION,
                "resume-domain-build",
                True,
                request,
                "arc.domain.resume.v1",
            )
        )


class _CancelledDomainHandler:
    name = "arc.domain.build.v1"

    def execute(self, _context):
        raise CancelledError("cancelled for CLI test")


def _succeeded_snapshot(
    repository: RunRepository, *, run_id: str, domain_id: str = "domain-cli"
):
    snapshot = RunEngine(repository).execute(
        RunSpec(run_id, _SucceededDomainHandler.name, {"request": run_id}),
        _SucceededDomainHandler(domain_id),
    )
    assert snapshot.status is RunStatus.SUCCEEDED
    return snapshot


def _snapshot_with_status(
    repository: RunRepository, *, run_id: str, status: RunStatus
):
    handler = (
        _PausedDomainHandler()
        if status is RunStatus.PAUSED
        else _CancelledDomainHandler()
    )
    snapshot = RunEngine(repository).execute(
        RunSpec(run_id, handler.name, {"request": run_id}), handler
    )
    assert snapshot.status is status
    return snapshot


def _result(repository: RunRepository, run_id: str) -> DomainBuildResult:
    snapshot = repository.inspect(run_id).snapshot
    assert snapshot.result_ref is not None
    store = ImmutableArtifactStore(
        repository.run_directory(run_id), repository_root=repository.root
    )
    return decode_domain_build_result(
        json.loads(store.read_bytes(snapshot.result_ref).decode("utf-8"))
    )


def _envelope(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert value["schema_version"] == "arc.command_result.v1"
    return value


def test_build_decodes_full_policy_applies_only_four_overrides_and_publishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    repository = RunRepository(tmp_path)
    snapshot = _succeeded_snapshot(repository, run_id="build-run")

    class RecordingRunner:
        request = None
        received = None

        def __init__(self, received_repository: RunRepository) -> None:
            assert received_repository.root == repository.root

        def execute(self, request, *, run_id, max_workers):
            type(self).request = request
            type(self).received = (run_id, max_workers)
            return snapshot

    monkeypatch.setattr(cli, "DomainBuildRunner", RecordingRunner)
    policy = json.dumps(POLICY)

    assert (
        cli.main(
            [
                "build",
                "arXiv:2401.00001",
                "--intent",
                "methods",
                "--policy",
                policy,
                "--recent-window-days",
                "30",
                "--citer-pool-limit",
                "9",
                "--ranked-paper-limit",
                "3",
                "--graph-node-limit",
                "7",
                "--llm-provider",
                "manual",
                "--model-tier",
                "high",
                "--workers",
                "2",
                "--run-id",
                "requested-run-id",
                "--cache-root",
                str(tmp_path),
            ]
        )
        == 0
    )

    envelope = _envelope(capsys)
    assert envelope["status"] == "completed"
    assert envelope["run"]["id"] == "build-run"
    assert envelope["data"]["domain"] == {"id": "domain-cli", "active": True}
    assert RecordingRunner.received == ("requested-run-id", 2)
    assert RecordingRunner.request.policy.recent_window_days == 30
    assert RecordingRunner.request.policy.citer_pool_limit == 9
    assert RecordingRunner.request.policy.ranked_paper_limit == 3
    assert RecordingRunner.request.policy.graph_node_limit == 7
    assert RecordingRunner.request.model.provider == "manual"
    assert RecordingRunner.request.model.tier == "high"
    catalog = cli.read_domain_catalog(DomainPaths(tmp_path), domain_id="domain-cli")
    assert catalog is not None
    assert catalog.active == "build-run"


def test_mode_flags_promote_a_resolved_v1_policy_to_v2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    repository = RunRepository(tmp_path)
    snapshot = _succeeded_snapshot(repository, run_id="build-v2")

    class RecordingRunner:
        request = None

        def __init__(self, received_repository: RunRepository) -> None:
            assert received_repository.root == repository.root

        def execute(self, request, *, run_id, max_workers):
            del run_id, max_workers
            type(self).request = request
            return snapshot

    monkeypatch.setattr(cli, "DomainBuildRunner", RecordingRunner)
    assert (
        cli.main(
            [
                "build",
                "arXiv:2401.00001",
                "--policy",
                json.dumps(POLICY),
                "--foundation-mode",
                "fixed-seed",
                "--citer-selection-mode",
                "strict-window",
                "--cache-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    _envelope(capsys)
    assert RecordingRunner.request.schema_version == "arc.domain_build_request.v2"
    assert RecordingRunner.request.policy.schema_version == "arc.domain_build_policy.v2"
    assert RecordingRunner.request.policy.foundation_mode == "fixed_seed"
    assert RecordingRunner.request.policy.citer_selection_mode == "strict_window"


def test_resume_passes_a_valid_resume_input_to_runner_and_publishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    repository = RunRepository(tmp_path)
    snapshot = _succeeded_snapshot(repository, run_id="resume-run")

    class RecordingRunner:
        resumed = None

        def __init__(self, received_repository: RunRepository) -> None:
            assert received_repository.root == repository.root

        def resume(self, run_id: str, *, input):
            type(self).resumed = (run_id, input)
            return snapshot

    monkeypatch.setattr(cli, "DomainBuildRunner", RecordingRunner)

    resume_input = resume_input_to_document(
        ResumeInput("resume-key", ResumeAction.CANCEL)
    )
    assert (
        cli.main(
            [
                "resume",
                "resume-run",
                "--input",
                json.dumps(resume_input),
                "--cache-root",
                str(tmp_path),
            ]
        )
        == 0
    )

    envelope = _envelope(capsys)
    assert envelope["status"] == "completed"
    assert RecordingRunner.resumed == ("resume-run", resume_input)
    assert envelope["data"]["domain"]["id"] == "domain-cli"


def test_status_uses_explicit_run_or_catalog_latest(tmp_path: Path, capsys) -> None:
    repository = RunRepository(tmp_path)
    first = _succeeded_snapshot(repository, run_id="first")
    newest = _succeeded_snapshot(repository, run_id="newest")
    paths = DomainPaths(tmp_path)
    publish_domain_result(repository, paths, run_id=first.run_id, result=_result(repository, first.run_id))
    publish_domain_result(repository, paths, run_id=newest.run_id, result=_result(repository, newest.run_id))

    assert cli.main(["status", "--domain-id", "domain-cli", "--cache-root", str(tmp_path)]) == 0
    domain_status = _envelope(capsys)
    assert domain_status["status"] == "completed"
    assert domain_status["run"]["id"] == "newest"
    assert domain_status["data"]["domain"] == {
        "id": "domain-cli",
        "latest": "newest",
        "active": "newest",
    }

    assert cli.main(["status", "--run-id", "first", "--cache-root", str(tmp_path)]) == 0
    run_status = _envelope(capsys)
    assert run_status["status"] == "completed"
    assert run_status["run"]["id"] == "first"
    assert "domain" not in run_status["data"]


def test_get_commands_read_only_the_active_export(tmp_path: Path, capsys) -> None:
    repository = RunRepository(tmp_path)
    snapshot = _succeeded_snapshot(repository, run_id="published")
    paths = DomainPaths(tmp_path)
    publish_domain_result(repository, paths, run_id=snapshot.run_id, result=_result(repository, snapshot.run_id))

    assert cli.main(["get-summary", "--domain-id", "domain-cli", "--cache-root", str(tmp_path)]) == 0
    summary = _envelope(capsys)
    assert summary["data"]["domain"] == {"id": "domain-cli", "active": "published"}
    assert summary["data"]["summary"]["title"] == "Summary"

    assert cli.main(["get-graph", "--domain-id", "domain-cli", "--cache-root", str(tmp_path)]) == 0
    graph = _envelope(capsys)
    assert graph["data"]["graph"]["schema_version"] == "arc.domain_graph.v1"


def test_get_rejects_an_active_export_with_a_corrupt_digest(
    tmp_path: Path, capsys
) -> None:
    repository = RunRepository(tmp_path)
    snapshot = _succeeded_snapshot(repository, run_id="published")
    paths = DomainPaths(tmp_path)
    publish_domain_result(repository, paths, run_id=snapshot.run_id, result=_result(repository, snapshot.run_id))
    (paths.export_generation("domain-cli", "published") / "graph.json").write_text(
        '{"schema_version":"tampered"}\n', encoding="utf-8"
    )

    assert cli.main(["get-graph", "--domain-id", "domain-cli", "--cache-root", str(tmp_path)]) == 1
    envelope = _envelope(capsys)
    assert envelope["status"] == "failed"


def test_cancel_and_validate_delegate_to_root_run_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    calls: list[list[str]] = []

    def fake_run_control(argv: list[str]) -> int:
        calls.append(argv)
        print(command_result_json(CommandResult(CommandStatus.COMPLETED)))
        return 0

    monkeypatch.setattr(cli, "run_control_main", fake_run_control)

    assert cli.main(["cancel", "run-1", "--reason", "user", "--cache-root", str(tmp_path)]) == 0
    _envelope(capsys)
    assert cli.main(["validate", "run-2", "--cache-root", str(tmp_path)]) == 0
    _envelope(capsys)
    assert calls == [
        ["cancel", "--run-root", str(tmp_path), "--run-id", "run-1", "--reason", "user"],
        ["validate", "--run-root", str(tmp_path), "--run-id", "run-2"],
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["init"],
        ["status"],
        ["status", "--run-id", "a", "--domain-id", "b"],
        ["get-summary"],
        ["build", "arXiv:2401.00001", "--policy", "not-json"],
    ],
)
def test_invalid_requests_always_emit_shared_envelope(argv: list[str], capsys) -> None:
    assert cli.main(argv) == 2
    envelope = _envelope(capsys)
    assert envelope["status"] == "failed"
    assert envelope["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "model_args",
    [
        ["--model", "exact-model"],
        [
            "--llm-provider",
            "manual",
            "--model",
            "exact-model",
            "--model-tier",
            "high",
        ],
    ],
)
def test_build_rejects_invalid_exact_model_selection(model_args: list[str], capsys) -> None:
    assert cli.main(["build", "arXiv:2401.00001", *model_args]) == 2
    envelope = _envelope(capsys)
    assert envelope["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "policy",
    [
        {
            key: value
            for key, value in POLICY.items()
            if key != "graph_node_limit"
        },
        {**POLICY, "unexpected": True},
    ],
)
def test_build_rejects_incomplete_or_unknown_policy_fields(
    policy: dict, capsys
) -> None:
    assert (
        cli.main(
            ["build", "arXiv:2401.00001", "--policy", json.dumps(policy)]
        )
        == 2
    )
    envelope = _envelope(capsys)
    assert envelope["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "command",
    [
        "init",
        "identify-foundation",
        "llm-identify-foundation",
        "build-network",
        "llm-build-network",
        "build-paper-json-pack",
        "build-evidence",
        "summarize",
        "llm-summarize",
        "llm-build",
    ],
)
def test_retired_stage_commands_and_llm_aliases_are_rejected(command: str, capsys) -> None:
    assert cli.main([command]) == 2
    envelope = _envelope(capsys)
    assert envelope["error"]["code"] == "invalid_request"


def test_build_without_policy_freezes_a_complete_default_policy() -> None:
    args = cli._parser().parse_args(["build", "arXiv:2401.00001"])

    request = cli._request_from_args(args)

    assert request.policy.as_of_date.count("-") == 2
    assert request.policy.recent_window_days == 365
    assert request.policy.citer_pool_limit == 1000
    assert request.policy.ranked_paper_limit == 50
    assert request.policy.graph_node_limit == 90


def test_resume_requires_a_strict_resume_input_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    class Runner:
        def __init__(self, _repository: RunRepository) -> None:
            pass

        def resume(self, _run_id: str, *, input):
            raise AssertionError(f"runner must not receive invalid input: {input!r}")

    monkeypatch.setattr(cli, "DomainBuildRunner", Runner)

    assert cli.main(["resume", "run-1", "--input", "[]", "--cache-root", str(tmp_path)]) == 2
    envelope = _envelope(capsys)
    assert envelope["error"]["code"] == "invalid_request"


def test_completed_run_with_failed_publication_is_command_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    repository = RunRepository(tmp_path)
    snapshot = _succeeded_snapshot(repository, run_id="publication-failure")

    class Runner:
        def __init__(self, _repository: RunRepository) -> None:
            pass

        def execute(self, _request, *, run_id, max_workers):
            assert run_id is None
            assert max_workers == 8
            return snapshot

    def fail_publication(*_args, **_kwargs):
        raise cli.DomainPublicationError("manifest write failed")

    monkeypatch.setattr(cli, "DomainBuildRunner", Runner)
    monkeypatch.setattr(cli, "publish_domain_result", fail_publication)

    assert (
        cli.main(
            [
                "build",
                "arXiv:2401.00001",
                "--policy",
                json.dumps(POLICY),
                "--cache-root",
                str(tmp_path),
            ]
        )
        == 1
    )
    envelope = _envelope(capsys)
    assert envelope["status"] == "failed"
    assert envelope["error"]["code"] == "domain_publication_failed"


@pytest.mark.parametrize(
    ("status", "expected_command_status"),
    [
        (RunStatus.PAUSED, "paused"),
        (RunStatus.CANCELLED, "cancelled"),
    ],
)
def test_noncompleted_build_snapshots_return_success_without_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
    status: RunStatus,
    expected_command_status: str,
) -> None:
    repository = RunRepository(tmp_path)
    snapshot = _snapshot_with_status(repository, run_id=f"{status.value}-run", status=status)

    class Runner:
        def __init__(self, received_repository: RunRepository) -> None:
            assert received_repository.root == repository.root

        def execute(self, _request, *, run_id, max_workers):
            assert run_id is None
            assert max_workers == 8
            return snapshot

    def publication_must_not_run(*_args, **_kwargs):
        raise AssertionError("non-completed snapshots must not be published")

    monkeypatch.setattr(cli, "DomainBuildRunner", Runner)
    monkeypatch.setattr(cli, "publish_domain_result", publication_must_not_run)

    assert (
        cli.main(
            [
                "build",
                "arXiv:2401.00001",
                "--policy",
                json.dumps(POLICY),
                "--cache-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    envelope = _envelope(capsys)
    assert envelope["status"] == expected_command_status
