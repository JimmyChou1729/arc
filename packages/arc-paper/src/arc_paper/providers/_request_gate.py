"""Small synchronous per-host request gates for polite remote acquisition."""

from __future__ import annotations

from _thread import allocate_lock
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic, sleep

import httpx


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
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if minimum_interval < 0:
            raise ValueError("minimum_interval must be non-negative")
        self.minimum_interval = minimum_interval
        self._clock = clock
        self._sleeper = sleeper
        self._lock = allocate_lock()
        self._next_start = 0.0

    def request(self, fetch: Callable[[], httpx.Response]) -> httpx.Response:
        """Run one request after the host's next permitted start time."""

        with self._lock:
            delay = self._next_start - self._clock()
            if delay > 0:
                self._sleeper(delay)
            started = self._clock()
            self._next_start = started + self.minimum_interval
            response = fetch()
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                self._next_start = max(
                    self._next_start, self._clock() + retry_after
                )
            return response


_SHARED_GATES: dict[tuple[str, str], HostRequestGate] = {}
_SHARED_GATES_LOCK = allocate_lock()


def shared_host_gate(cache_root: str | Path, host: str) -> HostRequestGate:
    """Return the process-local gate shared by providers for one cache/host."""

    key = (str(Path(cache_root).resolve()), host)
    with _SHARED_GATES_LOCK:
        gate = _SHARED_GATES.get(key)
        if gate is None:
            gate = HostRequestGate()
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
