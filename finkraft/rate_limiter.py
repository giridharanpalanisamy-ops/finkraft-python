"""One permit every 1/rate seconds. Spacing permits evenly means no one-second window ever sees
more than the rate, unlike 'rate permits per second' which can burst at window edges."""
from __future__ import annotations

import threading

from .clock import Clock


class RateLimiter:
    def __init__(self, rate_per_second: float, clock: Clock):
        self._interval = 1.0 / rate_per_second
        self._clock = clock
        self._next_free = 0.0
        self._lock = threading.Lock()

    def acquire(self, max_wait: float) -> bool:
        """Waits for a permit; False (without waiting) if none is free within max_wait."""
        with self._lock:
            now = self._clock.now()
            slot = max(now, self._next_free)
            wait = slot - now
            if wait > max_wait:
                return False
            self._next_free = slot + self._interval
        self._clock.sleep(wait)
        return True


class Unlimited:
    def acquire(self, max_wait: float) -> bool:
        return True
