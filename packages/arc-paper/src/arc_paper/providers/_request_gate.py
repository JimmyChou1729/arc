"""Small synchronous per-host request gates for polite remote acquisition."""

from __future__ import annotations

from _thread import allocate_lock
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import sleep, time
from typing import Iterator

import httpx
from ac_jobs import atomic_write_bytes, file_lease



class HostRequestGate:
    """Serialize a host's requests and enforce a minimum start interval.

    The caller supplies the entire request callback so only one connection is
    active for the host at a time.  ``Retry-After`` extends the next eligible
    request start without changing cached-read behavior.
    """

    def __init__(
        self,
        *,
        minimum_interval: float = 15.0,
        clock: Callable[[], float] = time,
        sleeper: Callable[[float], None] = sleep,
        state_path: str | Path | None = None,
        lock_path: str | Path | None = None,
    ) -> None:
        if minimum_interval < 0:
            raise ValueError("minimum_interval must be non-negative")
        if (state_path is None) != (lock_path is None):
            raise ValueError("state_path and lock_path must be supplied together")
        self.minimum_interval = minimum_interval
        self._clock = clock
        self._sleeper = sleeper
        self._lock = allocate_lock()
        self._next_start = 0.0
        self._state_path = Path(state_path) if state_path is not None else None
        self._lock_path = Path(lock_path) if lock_path is not None else None

    def request(self, fetch: Callable[[], httpx.Response]) -> httpx.Response:
        """Run one request after the host's next permitted start time."""

        with self._request_lock():
            next_start = self._read_next_start()
            delay = next_start - self._clock()
            if delay > 0:
                self._sleeper(delay)
            started = self._clock()
            next_start = started + self.minimum_interval
            self._write_next_start(next_start)
            response = fetch()
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                next_start = max(next_start, self._clock() + retry_after)
                self._write_next_start(next_start)
            return response

    @contextmanager
    def _request_lock(self) -> Iterator[None]:
        if self._lock_path is None:
            with self._lock:
                yield
            return
        with file_lease(self._lock_path, blocking=True):
            yield

    def _read_next_start(self) -> float:
        if self._state_path is None:
            return self._next_start
        try:
            return float(self._state_path.read_text(encoding="ascii"))
        except (OSError, UnicodeError, ValueError):
            return 0.0

    def _write_next_start(self, value: float) -> None:
        if self._state_path is None:
            self._next_start = value
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            self._state_path,
            f"{value:.9f}\n".encode("ascii"),
        )


_SHARED_GATES: dict[tuple[str, str], HostRequestGate] = {}
_SHARED_GATES_LOCK = allocate_lock()


def shared_host_gate(
    cache_root: str | Path,
    host: str,
    *,
    minimum_interval: float = 15.0,
) -> HostRequestGate:
    """Return the cache-root-scoped gate shared by providers and processes."""

    root = Path(cache_root).resolve()
    key = (str(root), host)
    with _SHARED_GATES_LOCK:
        gate = _SHARED_GATES.get(key)
        if gate is None:
            state_dir = root / "remote-request-cache" / "v2" / "host-gates"
            safe_host = "".join(
                character if character.isalnum() or character in ".-" else "_"
                for character in host
            )
            gate = HostRequestGate(
                minimum_interval=minimum_interval,
                state_path=state_dir / f"{safe_host}.next-start",
                lock_path=state_dir / f"{safe_host}.lock",
            )
            _SHARED_GATES[key] = gate
        return gate


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, seconds)


__all__ = ["HostRequestGate", "shared_host_gate"]
