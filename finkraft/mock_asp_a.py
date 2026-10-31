"""An in-memory ASP A that behaves like the spec, with fault injection and assertions for tests.
It plays the server side of the Transport the adapter uses; no network is involved."""
from __future__ import annotations

import itertools
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .adapters.base import HttpResponse
from .clock import Clock

LIFECYCLE = ("RECEIVED", "VALIDATED", "DELIVERED", "REPORTED")


@dataclass
class _Submission:
    id: str
    seller_trn: str
    invoice_number: str
    accepted_at: float
    statuses: tuple[str, ...]
    step_seconds: float
    error: dict | None = None

    def status(self, now: float) -> str:
        steps = int((now - self.accepted_at) // self.step_seconds) if self.step_seconds > 0 else len(self.statuses)
        return self.statuses[min(max(steps, 0), len(self.statuses) - 1)]


@dataclass
class Faults:
    """Each counter applies to the next N POSTs it covers (all invoices, or one: see MockAspA.fault_for)."""

    timeout_after_accept: int = 0  # accept, then the caller sees a timeout
    fail_5xx: int = 0
    reject: int = 0
    reject_error: dict = field(default_factory=lambda: {"code": "E104", "field": "BT-48"})
    refuse_connections: bool = False
    jump_to: str | None = None  # skip straight from RECEIVED to this status


class MockAspA:
    def __init__(self, clock: Clock, rate_limit: int = 10, step_seconds: float = 60.0):
        self._clock = clock
        self._rate_limit = rate_limit
        self.step_seconds = step_seconds
        self.faults = Faults()  # applies to every invoice without its own entry
        self._invoice_faults: dict[str, Faults] = {}
        self._ids = itertools.count(1)
        self._submissions: dict[str, _Submission] = {}
        self._by_invoice: dict[tuple[str, str], str] = {}
        self._window: deque[float] = deque()
        self.peak_requests_per_second = 0
        self.total_calls = 0
        self.posts_by_invoice: dict[str, int] = defaultdict(int)

    # --- the API, as seen through the adapter's Transport ---

    def request(self, method: str, path: str, body: dict | None, headers: dict[str, str], timeout: float) -> HttpResponse:
        if self.faults.refuse_connections:
            raise ConnectionRefusedError("ASP A is down")
        self.total_calls += 1
        if not self._admit():
            return HttpResponse(429, {"error": "RATE_LIMITED"})
        if method == "POST" and path == "/invoices":
            return self._submit(body or {})
        if method == "GET" and path.startswith("/invoices/"):
            sub = self._submissions.get(path.removeprefix("/invoices/"))
            if sub is None:
                return HttpResponse(404, {"error": "NOT_FOUND"})
            return HttpResponse(200, self._view(sub))
        return HttpResponse(404, {"error": "NO_ROUTE"})

    def _submit(self, body: dict) -> HttpResponse:
        number, seller = body.get("invoice_number"), body.get("seller_trn")
        self.posts_by_invoice[number] += 1
        f = self._invoice_faults.get(number, self.faults)
        if _take(f, "fail_5xx"):
            return HttpResponse(500, {"error": "INTERNAL"})
        existing = self._by_invoice.get((seller, number))
        if existing and self._submissions[existing].status(self._clock.now()) != "REJECTED":
            return HttpResponse(409, {"error": "DUPLICATE_INVOICE_NUMBER", "submission_id": existing})
        reject = _take(f, "reject")
        statuses = ("REJECTED",) if reject else LIFECYCLE
        if not reject and f.jump_to in LIFECYCLE:
            statuses = ("RECEIVED",) + LIFECYCLE[LIFECYCLE.index(f.jump_to):]
        sub = _Submission(f"A-{next(self._ids):06d}", seller, number, self._clock.now(), statuses, self.step_seconds,
                          f.reject_error if reject else None)
        self._submissions[sub.id] = sub
        self._by_invoice[(seller, number)] = sub.id
        if _take(f, "timeout_after_accept"):
            raise TimeoutError("read timed out (ASP A accepted the invoice)")
        return HttpResponse(200, self._view(sub))

    def _view(self, sub: _Submission) -> dict:
        status = sub.status(self._clock.now())
        view = {"submission_id": sub.id, "status": status}
        if status == "REJECTED" and sub.error:
            view["errors"] = [sub.error]
        return view

    def _admit(self) -> bool:
        now = self._clock.now()
        while self._window and self._window[0] <= now - 1.0:
            self._window.popleft()
        if len(self._window) >= self._rate_limit:
            return False
        self._window.append(now)
        self.peak_requests_per_second = max(self.peak_requests_per_second, len(self._window))
        return True

    # --- fault injection and assertions for tests ---

    def fault_for(self, invoice_number: str) -> Faults:
        """Faults that apply only to this invoice number."""
        return self._invoice_faults.setdefault(invoice_number, Faults())

    def accepted(self, invoice_number: str) -> int:
        return sum(1 for s in self._submissions.values() if s.invoice_number == invoice_number)

    @property
    def distinct_invoices(self) -> int:
        return len(self._submissions)


def _take(faults: Faults, name: str) -> bool:
    n = getattr(faults, name)
    if n > 0:
        setattr(faults, name, n - 1)
        return True
    return False
