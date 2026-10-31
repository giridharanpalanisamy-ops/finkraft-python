"""The adapter contract. Only canonical types cross it; raw ASP bodies are kept for audit only.
Adapters hold no state, never retry and never touch storage: the service decides what to do."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from ..models import AspError, Event, Invoice


class StatusMode(StrEnum):
    POLL = "POLL"
    WEBHOOK = "WEBHOOK"


class SubmitOutcome(StrEnum):
    RECEIVED = "RECEIVED"    # the ASP has it
    REJECTED = "REJECTED"    # the ASP refused the content
    DUPLICATE = "DUPLICATE"  # the ASP already had it (treated as received)
    RETRYABLE = "RETRYABLE"  # provably NOT received: safe to try again later
    UNKNOWN = "UNKNOWN"      # may have been received: never blindly resent


@dataclass(frozen=True)
class AspCapabilities:
    status_mode: StatusMode
    native_dedup: bool
    rate_limit_rps: float  # 0 means no published limit
    safe_to_retry_on_timeout: bool


@dataclass(frozen=True)
class AspCredentials:
    api_key: str | None

    def __repr__(self) -> str:
        return "AspCredentials(***)"


@dataclass(frozen=True)
class SubmitRequest:
    invoice_id: str
    client_ref: str
    invoice: Invoice
    credentials: AspCredentials


@dataclass(frozen=True)
class SubmitResult:
    outcome: SubmitOutcome
    asp_ref: str | None = None
    raw_status: str | None = None
    errors: tuple[AspError, ...] = ()
    raw_body: str | None = None


@dataclass(frozen=True)
class StatusUpdate:
    asp_ref: str | None
    client_ref: str | None
    raw_status: str
    event: Event
    occurred_at: float | None = None
    errors: tuple[AspError, ...] = ()
    raw_body: str | None = None


class UnmappedStatus(Exception):
    """A status the adapter has no mapping for. Never guessed."""


class AspUnavailable(Exception):
    """A status could not be read."""


class AspAdapter(ABC):
    asp_id: str
    capabilities: AspCapabilities

    @abstractmethod
    def submit(self, request: SubmitRequest) -> SubmitResult:
        """Converts canonical to ASP format. Never raises for HTTP outcomes."""

    @abstractmethod
    def get_status(self, asp_ref: str) -> StatusUpdate:
        """POLL ASPs only. Raises AspUnavailable."""

    @abstractmethod
    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[StatusUpdate]:
        """WEBHOOK ASPs only; verifies the signature first."""

    @abstractmethod
    def to_event(self, raw_status: str) -> Event:
        """Raises UnmappedStatus."""


@dataclass
class HttpResponse:
    status: int
    body: dict = field(default_factory=dict)
