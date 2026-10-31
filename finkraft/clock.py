"""Time as a dependency, so timeouts, leases and backoff are testable without waiting."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


DEFAULT_START = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc).timestamp()


class FakeClock:
    """Manual time: sleep() advances it instantly. Starts on 1 Nov 2026 unless told otherwise."""

    def __init__(self, start: float = DEFAULT_START):
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += max(0.0, seconds)
