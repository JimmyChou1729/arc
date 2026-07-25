"""Progress callback plumbing for the ARC ideas workflow."""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Mapping


ProgressCallback = Callable[[dict[str, Any]], None]


class IdeasProgressEmitter:
    """Serialize best-effort public workflow progress with stable metadata."""

    def __init__(
        self,
        callback: ProgressCallback | None,
        *,
        run_id: str,
    ) -> None:
        self._callback = callback
        self._run_id = run_id
        self._lock = threading.Lock()
        self._sequence = 0
        self._errors: list[str] = []

    @property
    def errors(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._errors)

    def emit(self, event: Mapping[str, Any]) -> None:
        if self._callback is None:
            return
        with self._lock:
            self._sequence += 1
            payload = {
                **dict(event),
                "sequence": self._sequence,
                "emitted_at": datetime.now(timezone.utc).isoformat(),
                "run_id": self._run_id,
            }
            try:
                self._callback(payload)
            except Exception as exc:
                error_type = type(exc).__name__
                if error_type not in self._errors:
                    self._errors.append(error_type)


class IdeasStopController:
    """Bridge process signals to one durable stop request."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._requested = False
        self._closed = False
        self._errors: list[str] = []

    def request(self) -> None:
        with self._condition:
            self._requested = True
            self._condition.notify_all()

    def is_requested(self) -> bool:
        with self._condition:
            return self._requested

    def bridge(self, stop: Callable[[], None]) -> "_StopBridge":
        return _StopBridge(self, stop)

    @property
    def errors(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(self._errors)

    def _wait(self) -> bool:
        with self._condition:
            self._condition.wait_for(
                lambda: self._requested or self._closed
            )
            return self._requested

    def _close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _record_error(self, error: Exception) -> None:
        with self._condition:
            self._errors.append(type(error).__name__)


class _StopBridge:
    def __init__(
        self,
        controller: IdeasStopController,
        stop: Callable[[], None],
    ) -> None:
        self._controller = controller
        self._stop = stop
        self._thread = threading.Thread(
            target=self._run,
            name="arc-ideas-stop-bridge",
            daemon=True,
        )

    def __enter__(self) -> "_StopBridge":
        self._thread.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._controller._close()
        self._thread.join()

    def _run(self) -> None:
        if not self._controller._wait():
            return
        try:
            self._stop()
        except Exception as exc:
            self._controller._record_error(exc)


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
        first_error: Exception | None = None
        for callback in callbacks:
            try:
                callback(dict(event))
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    return emit


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
