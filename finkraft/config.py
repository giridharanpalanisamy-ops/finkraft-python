"""Settings. Defaults follow the design; every timing is injectable so tests run instantly."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _mock_data_default() -> bool:
    return os.environ.get("FINKRAFT_MOCK_DATA", "1").lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    # Mock data: sample sellers and invoices loaded into the in-memory store at start-up.
    # On by default; set FINKRAFT_MOCK_DATA=0 (or pass mock_data_enabled=False) to start empty.
    mock_data_enabled: bool = field(default_factory=_mock_data_default)

    max_attempts: int = 6
    backoff_base: float = 30.0
    backoff_factor: float = 2.0
    backoff_max: float = 1800.0
    backoff_jitter: float = 0.2
    lease_seconds: float = 120.0
    reconcile_window: float = 1800.0
    poll_first: float = 30.0
    poll_max: float = 900.0
    permit_wait: float = 2.0
    asp_a_rate_limit_rps: float = 8.0  # headroom under ASP A's 10/s; shared by submits and polls
    asp_a_timeout_resends: tuple[float, ...] = (1.0, 2.0, 4.0)
