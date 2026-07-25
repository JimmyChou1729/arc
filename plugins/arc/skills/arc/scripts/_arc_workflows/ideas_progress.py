"""Progress callback plumbing for the ARC ideas workflow."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Mapping


ProgressCallback = Callable[[dict[str, Any]], None]


def progress_sidechannel_callback(
    base_env: Mapping[str, str] | None,
) -> ProgressCallback | None:
    environment = base_env if base_env is not None else os.environ
    raw = str(environment.get("ARC_JOB_PROGRESS_FILE", "")).strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    lock = threading.Lock()

    def append_progress(event: dict[str, Any]) -> None:
        payload = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                os.chmod(path, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

    return append_progress


def combined_progress_callback(
    first: ProgressCallback | None,
    second: ProgressCallback | None,
) -> ProgressCallback | None:
    callbacks = tuple(item for item in (first, second) if item is not None)
    if not callbacks:
        return None

    def emit(event: dict[str, Any]) -> None:
        for callback in callbacks:
            callback(dict(event))

    return emit


def emit_progress(
    callback: ProgressCallback | None,
    event: dict[str, Any],
) -> None:
    if callback is not None:
        callback(event)


def foreground_progress_callback() -> ProgressCallback | None:
    if str(os.environ.get("ARC_JOB_PROGRESS_FILE", "")).strip():
        return None

    def emit(event: dict[str, Any]) -> None:
        print(
            json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ),
            file=sys.stderr,
            flush=True,
        )

    return emit
