"""Read-only projection of safe domain-build progress metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arc_jobs import ArcJobsError, EventWriter, RunRepository, RunSnapshot, RunStatus


_ACTIVITY_EVENTS = {
    "domain_operation_started",
    "group_unit_finished",
    "llm_provider_activity",
}


def project_domain_progress(
    repository: RunRepository,
    snapshot: RunSnapshot,
) -> dict[str, Any]:
    """Project event-log activity without changing durable lifecycle state."""

    path = repository.run_directory(snapshot.run_id) / "events.jsonl"
    progress: dict[str, Any] = {
        "stage": None,
        "operation": None,
        "completed_units": 0,
        "total_units": 0,
        "failed_units": 0,
        "last_activity_at": None,
        "event_sequence": 0,
        "diagnostic_code": None,
    }
    if not path.exists():
        progress["diagnostic_code"] = "domain_progress_event_log_missing"
        return progress
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            incomplete_tail = False
            if size:
                handle.seek(size - 1)
                incomplete_tail = handle.read(1) not in {b"\n", b"\r"}
        writer = EventWriter(path, run_id=snapshot.run_id)
        writer.validate()
        events = writer.tail()
    except (ArcJobsError, ValueError):
        progress["diagnostic_code"] = "domain_progress_integrity_error"
        return progress
    except OSError:
        progress["diagnostic_code"] = "domain_progress_unavailable"
        return progress
    if not events:
        progress["diagnostic_code"] = (
            "domain_progress_incomplete_tail"
            if incomplete_tail
            else "domain_progress_event_log_empty"
        )
        return progress
    if incomplete_tail:
        progress["diagnostic_code"] = "domain_progress_incomplete_tail"
    first_sequence = events[0].get("sequence")
    if type(first_sequence) is int and first_sequence != 1:
        progress["diagnostic_code"] = "domain_progress_history_truncated"

    current: Mapping[str, Any] | None = None
    finished: dict[tuple[str, str], str] = {}
    for document in events:
        sequence = document.get("sequence")
        if type(sequence) is int and sequence > progress["event_sequence"]:
            progress["event_sequence"] = sequence
        event = document.get("event")
        data = document.get("data")
        emitted_at = document.get("emitted_at")
        if event not in _ACTIVITY_EVENTS or not isinstance(data, Mapping):
            continue
        if isinstance(emitted_at, str):
            progress["last_activity_at"] = emitted_at
        if event == "domain_operation_started":
            stage = data.get("stage")
            operation = data.get("operation")
            total_units = data.get("total_units")
            if (
                isinstance(stage, str)
                and isinstance(operation, str)
                and type(total_units) is int
                and total_units >= 0
            ):
                current = data
            continue
        if event == "group_unit_finished":
            group_id = data.get("group_id")
            unit_id = data.get("unit_id")
            status = data.get("status")
            if (
                isinstance(group_id, str)
                and isinstance(unit_id, str)
                and status in {"succeeded", "failed"}
            ):
                finished[(group_id, unit_id)] = status

    if current is None:
        return progress
    progress["stage"] = current["stage"]
    progress["operation"] = current["operation"]
    progress["total_units"] = current["total_units"]
    group_id = current.get("group_id")
    if isinstance(group_id, str):
        statuses = [
            status
            for (finished_group, _unit_id), status in finished.items()
            if finished_group == group_id
        ]
        progress["completed_units"] = len(statuses)
        progress["failed_units"] = sum(status == "failed" for status in statuses)
    elif snapshot.status is RunStatus.SUCCEEDED:
        progress["completed_units"] = progress["total_units"]
    elif snapshot.status is RunStatus.FAILED:
        progress["completed_units"] = progress["total_units"]
        progress["failed_units"] = progress["total_units"]
    return progress


__all__ = ["project_domain_progress"]
