from __future__ import annotations

from arc_domain import progress
from arc_jobs import (
    EventWriter,
    Failed,
    RunEngine,
    RunError,
    RunRepository,
    RunSpec,
    Succeeded,
)


def _pending(repository: RunRepository, run_id: str = "domain-progress"):
    return repository.create(
        RunSpec(run_id, "arc.domain.build.v2", {"request": run_id})
    )


def test_progress_projects_group_units_without_snapshot_revision_changes(
    tmp_path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    snapshot = _pending(repository)
    writer = EventWriter(
        repository.run_directory(snapshot.run_id) / "events.jsonl",
        run_id=snapshot.run_id,
    )
    writer.emit(
        "domain_operation_started",
        {
            "stage": "network",
            "operation": "network-selected-references",
            "group_id": "network-selected-references",
            "total_units": 3,
        },
    )
    writer.emit("llm_provider_activity", {"event_count": 10})
    writer.emit(
        "llm_message",
        {
            "direction": "response",
            "message_kind": "assistant",
            "preview": "Checking the graph evidence.",
        },
    )
    writer.emit(
        "group_unit_finished",
        {
            "group_id": "network-selected-references",
            "unit_id": "paper-a",
            "status": "succeeded",
        },
    )
    writer.emit(
        "group_unit_finished",
        {
            "group_id": "network-selected-references",
            "unit_id": "paper-b",
            "status": "failed",
        },
    )
    writer.emit(
        "group_unit_finished",
        {
            "group_id": "network-selected-references",
            "unit_id": "paper-a",
            "status": "succeeded",
        },
    )

    projected = progress.project_domain_progress(repository, snapshot)

    assert projected["stage"] == "network"
    assert projected["operation"] == "network-selected-references"
    assert projected["completed_units"] == 2
    assert projected["failed_units"] == 1
    assert projected["total_units"] == 3
    assert projected["schema_version"] == "arc.domain_progress.v1"
    assert projected["event_sequence"] == 6
    assert projected["latest_message_preview"] == "Checking the graph evidence."
    assert projected["last_activity_at"] is not None
    assert projected["diagnostic_code"] is None
    assert repository.inspect(snapshot.run_id).snapshot.revision == snapshot.revision


def test_progress_uses_any_valid_event_for_activity_and_reports_bad_known_shape(
    tmp_path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    snapshot = _pending(repository)
    writer = EventWriter(
        repository.run_directory(snapshot.run_id) / "events.jsonl",
        run_id=snapshot.run_id,
    )
    writer.emit("run_started", {"attempt": 1})
    projected = progress.project_domain_progress(repository, snapshot)
    assert projected["last_activity_at"] is not None
    assert projected["diagnostic_code"] is None

    writer.emit("domain_operation_started", {"stage": "summary"})
    malformed = progress.project_domain_progress(repository, snapshot)
    assert malformed["diagnostic_code"] == "domain_progress_event_malformed"


def test_progress_degrades_for_missing_and_corrupt_event_logs(tmp_path) -> None:
    repository = RunRepository(tmp_path / "runs")
    snapshot = _pending(repository)

    assert progress.project_domain_progress(
        repository, snapshot
    )["diagnostic_code"] == "domain_progress_event_log_missing"

    path = repository.run_directory(snapshot.run_id) / "events.jsonl"
    path.write_text('{"malformed":"event"}\n', encoding="utf-8")
    assert progress.project_domain_progress(
        repository, snapshot
    )["diagnostic_code"] == "domain_progress_integrity_error"

    incomplete = _pending(repository, "incomplete-progress")
    incomplete_path = (
        repository.run_directory(incomplete.run_id) / "events.jsonl"
    )
    writer = EventWriter(incomplete_path, run_id=incomplete.run_id)
    writer.emit(
        "domain_operation_started",
        {
            "stage": "network",
            "operation": "network",
            "total_units": 1,
        },
    )
    with incomplete_path.open("ab") as handle:
        handle.write(b'{"partial":')
    assert progress.project_domain_progress(
        repository, incomplete
    )["diagnostic_code"] == "domain_progress_incomplete_tail"


def test_progress_marks_valid_tail_history_as_truncated(
    tmp_path, monkeypatch
) -> None:
    repository = RunRepository(tmp_path / "runs")
    snapshot = _pending(repository)
    path = repository.run_directory(snapshot.run_id) / "events.jsonl"
    path.touch()

    class TailOnlyEvents:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def validate(self) -> None:
            pass

        def tail(self):
            return (
                {
                    "sequence": 9,
                    "event": "domain_operation_started",
                    "emitted_at": "2026-07-26T00:00:00+00:00",
                    "data": {
                        "stage": "summary",
                        "operation": "domain_summary_llm",
                        "total_units": 1,
                    },
                },
            )

    monkeypatch.setattr(progress, "EventWriter", TailOnlyEvents)

    projected = progress.project_domain_progress(repository, snapshot)

    assert projected["diagnostic_code"] == "domain_progress_history_truncated"
    assert projected["event_sequence"] == 9
    assert projected["stage"] == "summary"


def test_succeeded_lifecycle_completes_latest_synchronous_operation(
    tmp_path,
) -> None:
    repository = RunRepository(tmp_path / "runs")

    class Handler:
        name = "arc.domain.build.v2"

        def execute(self, context):
            context.events.emit(
                "domain_operation_started",
                {
                    "stage": "finalize",
                    "operation": "result",
                    "total_units": 1,
                },
            )
            return Succeeded(context.artifacts.publish_json("result", {}))

    snapshot = RunEngine(repository).execute(
        RunSpec("completed-progress", Handler.name, {}), Handler()
    )
    projected = progress.project_domain_progress(repository, snapshot)

    assert projected["completed_units"] == 1
    assert projected["failed_units"] == 0


def test_failed_lifecycle_completes_and_fails_synchronous_operation(
    tmp_path,
) -> None:
    repository = RunRepository(tmp_path / "runs")

    class Handler:
        name = "arc.domain.build.v2"

        def execute(self, context):
            context.events.emit(
                "domain_operation_started",
                {
                    "stage": "summary",
                    "operation": "domain-summary-llm",
                    "total_units": 1,
                },
            )
            return Failed(RunError("provider_timeout", "timed out"))

    snapshot = RunEngine(repository).execute(
        RunSpec("failed-progress", Handler.name, {}), Handler()
    )
    projected = progress.project_domain_progress(repository, snapshot)

    assert projected["completed_units"] == 1
    assert projected["failed_units"] == 1
