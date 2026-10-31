"""InvoiceService: the single writer. Every state change goes through apply_event(), which checks
the dedup ledger, asks the state machine, applies side effects and saves with a version check."""
from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .adapters.base import (
    AspAdapter, AspCredentials, AspUnavailable, StatusMode, SubmitOutcome, SubmitRequest, UnmappedStatus,
)
from .clock import Clock
from .config import Settings
from .hashing import content_hash
from .models import AspError, AuditEntry, Event, Invoice, InvoiceRecord, InvoiceState
from .rate_limiter import RateLimiter, Unlimited
from .state_machine import POLL_STATES, InvalidTransition, implied_states, next_state
from .store import InMemoryInvoiceStore
from .validation import validate

log = logging.getLogger("finkraft")
S = InvoiceState


class NoRoute(Exception):
    """The seller has no ASP configured."""


@dataclass(frozen=True)
class UploadResult:
    outcome: str  # CREATED, REPLAY, REVISION or DUPLICATE_INVOICE_NUMBER
    invoice_id: str
    state: InvoiceState
    revision: int = 1
    possible_duplicate_of: str | None = None


@dataclass
class DeadLetter:
    invoice_id: str
    reason: str


@dataclass
class InvoiceService:
    store: InMemoryInvoiceStore
    adapters: dict[str, AspAdapter]
    clock: Clock
    settings: Settings = field(default_factory=Settings)
    rng: random.Random = field(default_factory=lambda: random.Random(7))
    dead_letters: list[DeadLetter] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._limiters = {
            asp_id: RateLimiter(self._rate(a), self.clock) if self._rate(a) > 0 else Unlimited()
            for asp_id, a in self.adapters.items()
        }

    def _rate(self, adapter: AspAdapter) -> float:
        if adapter.asp_id == "asp-a":
            return self.settings.asp_a_rate_limit_rps
        return adapter.capabilities.rate_limit_rps

    # ------------------------------------------------------------------ intake

    def upload(self, seller_id: str, invoice: Invoice | dict) -> UploadResult:
        """Applies the duplicate rule, then starts validation."""
        inv = invoice if isinstance(invoice, Invoice) else Invoice.from_dict(invoice)
        if not inv.invoice_number:
            raise ValueError("invoice_number is required")
        digest = content_hash(inv)
        with self.store.lock:
            existing = self.store.find_by_number(seller_id, inv.invoice_number)
            if existing is not None:
                if existing.content_hash == digest:
                    return UploadResult("REPLAY", existing.id, existing.state, existing.revision, existing.possible_duplicate_of)
                if existing.state in (S.VALIDATION_FAILED, S.REJECTED):
                    return self._revise(existing, inv, digest)
                return UploadResult("DUPLICATE_INVOICE_NUMBER", existing.id, existing.state, existing.revision)
            rec = InvoiceRecord(
                id=str(uuid.uuid4()), seller_id=seller_id, invoice_number=inv.invoice_number, invoice=inv,
                content_hash=digest,
                possible_duplicate_of=self.store.find_same_content(seller_id, digest, inv.invoice_number))
            self.store.insert(rec)
            self._audit(rec.id, None, S.RECEIVED, "INVOICE_CREATED", None, "api")
            state = self.apply_event(rec.id, Event.VALIDATION_STARTED, f"start:{rec.id}:1", actor="api")
            return UploadResult("CREATED", rec.id, state, 1, rec.possible_duplicate_of)

    def add_revision(self, invoice_id: str, invoice: Invoice | dict) -> UploadResult:
        """A corrected invoice, allowed only after VALIDATION_FAILED or REJECTED."""
        inv = invoice if isinstance(invoice, Invoice) else Invoice.from_dict(invoice)
        with self.store.lock:
            rec = self.store.get(invoice_id)
            if inv.invoice_number != rec.invoice_number:
                raise ValueError("a revision must keep the invoice number")
            return self._revise(rec, inv, content_hash(inv))

    def _revise(self, rec: InvoiceRecord, inv: Invoice, digest: str) -> UploadResult:
        revision = rec.revision + 1
        self.apply_event(rec.id, Event.REVISION_UPLOADED, f"revision:{rec.id}:{revision}", actor="api",
                         revision=(revision, inv, digest))
        state = self.apply_event(rec.id, Event.VALIDATION_STARTED, f"start:{rec.id}:{revision}", actor="api")
        return UploadResult("REVISION", rec.id, state, revision)

    # ------------------------------------------------------------------ the single writer

    def apply_event(self, invoice_id: str, event: Event, dedup_key: str, *, actor: str,
                    asp_ref: str | None = None, errors: tuple[AspError, ...] = (),
                    expected_state: InvoiceState | None = None,
                    revision: tuple[int, Invoice, str] | None = None) -> InvoiceState:
        """Applies one event at most once. Raises InvalidTransition (and changes nothing) if illegal."""
        with self.store.lock:
            rec = self.store.get(invoice_id)
            if self.store.is_processed(dedup_key):
                return rec.state  # already applied (R5)
            if expected_state is not None and rec.state != expected_state:
                # e.g. a lease timer that fired after the outcome arrived: stale, recorded, harmless
                self.store.mark_processed(dedup_key)
                self._audit(rec.id, rec.state, rec.state, event, dedup_key, actor, "stale: no-op")
                return rec.state
            max_reached = event == Event.SUBMIT_RETRYABLE and rec.attempt_count >= self.settings.max_attempts
            target = next_state(rec.state, event, max_attempts_reached=max_reached)  # may raise: nothing changed
            current = rec.state
            if target is None:
                if asp_ref and rec.asp_ref is None:
                    rec.asp_ref = asp_ref
                    self.store.save(rec)
                self.store.mark_processed(dedup_key)
                self._audit(rec.id, current, current, event, dedup_key, actor, "late or repeated status: no-op")
                return current
            if revision is not None:
                rec.revision, rec.invoice, rec.content_hash = revision
                rec.asp_id = rec.asp_ref = rec.client_ref = None
                rec.attempt_count = rec.poll_count = 0
            self._enter(rec, target, event, asp_ref, errors)
            self.store.save(rec)  # version check
            self.store.mark_processed(dedup_key)
            implied = implied_states(current, target)
            self._audit(rec.id, current, target, event, dedup_key, actor,
                        f"implies {', '.join(implied)}" if implied else "")
            log.info("%s %s: %s -> %s", rec.invoice_number, event, current, target)
            return target

    def _enter(self, rec: InvoiceRecord, target: InvoiceState, event: Event, asp_ref: str | None,
               errors: tuple[AspError, ...]) -> None:
        """Side effects of entering a state, committed together with the state change."""
        now = self.clock.now()
        rec.state = target
        if asp_ref and rec.asp_ref is None:
            rec.asp_ref = asp_ref
        if errors:
            rec.errors = list(errors)
        rec.lease_expires_at = rec.lease_expires_at if target == S.SUBMITTING else None
        rec.deadline_at = None
        rec.next_attempt_at = None
        rec.next_poll_at = None
        if target == S.READY_TO_SUBMIT and event == Event.VALIDATION_PASSED:
            asp_id = self.store.route(rec.seller_id)
            if asp_id is None or asp_id not in self.adapters:
                raise NoRoute(f"seller {rec.seller_id} has no ASP configured")
            rec.asp_id = asp_id  # pinned: later routing changes do not move this invoice
            rec.client_ref = str(uuid.uuid4())
            rec.errors = []
            rec.next_attempt_at = now
        elif target == S.READY_TO_SUBMIT and event == Event.SUBMIT_RETRYABLE:
            rec.next_attempt_at = now + self._backoff(rec.attempt_count)
        elif target in POLL_STATES and self._is_poll(rec.asp_id):
            rec.poll_count = 0
            rec.next_poll_at = now + self.settings.poll_first
        elif target == S.SUBMISSION_UNKNOWN:
            rec.deadline_at = now + self.settings.reconcile_window

    def _backoff(self, attempt: int) -> float:
        s = self.settings
        delay = min(s.backoff_base * s.backoff_factor ** max(0, attempt - 1), s.backoff_max)
        return max(0.0, delay * (1 + self.rng.uniform(-s.backoff_jitter, s.backoff_jitter)))

    def _is_poll(self, asp_id: str | None) -> bool:
        return asp_id in self.adapters and self.adapters[asp_id].capabilities.status_mode == StatusMode.POLL

    def _audit(self, invoice_id, from_state, to_state, event, dedup_key, actor, detail="") -> None:
        self.store.append_audit(AuditEntry(invoice_id, from_state, to_state, str(event), dedup_key, actor,
                                           self.clock.now(), detail))

    # ------------------------------------------------------------------ workers

    def validate_pending(self, invoice_id: str) -> None:
        rec = self.store.get(invoice_id)
        if rec.state != S.VALIDATING:
            return
        today = datetime.fromtimestamp(self.clock.now(), tz=timezone.utc).date()
        errors = tuple(validate(rec.invoice, today))
        result = "FAILED" if errors else "PASSED"
        self.apply_event(rec.id, Event.VALIDATION_FAILED if errors else Event.VALIDATION_PASSED,
                         f"val:{rec.id}:{rec.revision}:{result}", actor="validator", errors=errors)

    def submit(self, invoice_id: str, worker: str = "worker-1") -> None:
        """Read, claim, rate-limit, submit, record the outcome."""
        rec = self.store.get(invoice_id)
        if rec.state != S.READY_TO_SUBMIT:
            return  # already handled
        claimed = self.store.claim(rec.id, rec.version, self.clock.now() + self.settings.lease_seconds)
        if claimed is None:
            return  # another worker (or an earlier delivery) has it
        self._audit(rec.id, S.READY_TO_SUBMIT, S.SUBMITTING, Event.CLAIMED, None, worker)
        adapter = self.adapters[claimed.asp_id]
        limiter = self._limiters[claimed.asp_id]
        attempt = claimed.attempt_count

        def outcome(event: Event, label: str, **kw) -> None:
            self.apply_event(rec.id, event, f"submit:{rec.id}:{attempt}:{label}", actor=worker, **kw)

        if not limiter.acquire(self.settings.permit_wait):
            outcome(Event.SUBMIT_RETRYABLE, "NO_PERMIT", expected_state=S.SUBMITTING)
            return
        request = SubmitRequest(rec.id, claimed.client_ref, claimed.invoice,
                                AspCredentials(self.store.credentials(rec.seller_id, claimed.asp_id)))
        result = adapter.submit(request)
        if result.outcome == SubmitOutcome.UNKNOWN and adapter.capabilities.safe_to_retry_on_timeout:
            # The ASP deduplicates, so resending is safe: a 409 tells us it already has the invoice.
            for delay in self.settings.asp_a_timeout_resends:
                self.clock.sleep(delay)
                if not limiter.acquire(self.settings.permit_wait):
                    continue
                resent = adapter.submit(request)
                if resent.outcome not in (SubmitOutcome.UNKNOWN, SubmitOutcome.RETRYABLE):
                    result = resent
                    break
        match result.outcome:
            case SubmitOutcome.RECEIVED | SubmitOutcome.DUPLICATE:
                outcome(Event.ASP_RECEIVED, "RECEIVED", asp_ref=result.asp_ref)
            case SubmitOutcome.REJECTED:
                outcome(Event.ASP_REJECTED, "REJECTED", asp_ref=result.asp_ref, errors=result.errors)
            case SubmitOutcome.RETRYABLE:
                outcome(Event.SUBMIT_RETRYABLE, "RETRYABLE", expected_state=S.SUBMITTING)
            case SubmitOutcome.UNKNOWN:
                # Never resent where the ASP cannot deduplicate; reconcile or a human decides.
                outcome(Event.SUBMIT_TIMEOUT, "TIMEOUT", expected_state=S.SUBMITTING)

    def poll(self, invoice_id: str) -> None:
        rec = self.store.get(invoice_id)
        if rec.state not in POLL_STATES or not self._is_poll(rec.asp_id) or rec.asp_ref is None:
            return
        if self._limiters[rec.asp_id].acquire(self.settings.permit_wait):
            try:
                update = self.adapters[rec.asp_id].get_status(rec.asp_ref)
                self.on_status(rec.id, rec.asp_id, update.asp_ref or rec.asp_ref, update.raw_status, update.event,
                               update.errors)
            except AspUnavailable as e:
                log.warning("poll of %s failed: %s", rec.invoice_number, e)
            except UnmappedStatus as e:
                self.dead_letters.append(DeadLetter(rec.id, str(e)))
        with self.store.lock:  # unchanged status: poll again later, backing off
            after = self.store.get(invoice_id)
            if after.state == rec.state and after.state in POLL_STATES:
                after.poll_count += 1
                after.next_poll_at = self.clock.now() + min(
                    self.settings.poll_first * 2 ** after.poll_count, self.settings.poll_max)
                self.store.save(after)

    def on_status(self, invoice_id: str, asp_id: str, asp_ref: str, raw_status: str, event: Event,
                  errors: tuple[AspError, ...] = ()) -> None:
        """A status from an ASP (poll today, webhook later). Illegal ones are dead-lettered, not applied."""
        try:
            self.apply_event(invoice_id, event, f"asp:{asp_id}:{asp_ref}:{raw_status}", actor=asp_id,
                             asp_ref=asp_ref, errors=errors)
        except InvalidTransition as e:
            self.dead_letters.append(DeadLetter(invoice_id, str(e)))

    def run_timers(self) -> int:
        now = self.clock.now()
        fired = 0
        for rec in self.store.all([S.SUBMITTING, S.SUBMISSION_UNKNOWN]):
            if rec.state == S.SUBMITTING and rec.lease_expires_at is not None and rec.lease_expires_at <= now:
                self.apply_event(rec.id, Event.LEASE_EXPIRED, f"timer:{rec.id}:LEASE:{rec.lease_expires_at}",
                                 actor="scheduler", expected_state=S.SUBMITTING)
                fired += 1
            elif rec.state == S.SUBMISSION_UNKNOWN and rec.deadline_at is not None and rec.deadline_at <= now:
                self.apply_event(rec.id, Event.UNKNOWN_DEADLINE_PASSED, f"timer:{rec.id}:DEADLINE:{rec.deadline_at}",
                                 actor="scheduler", expected_state=S.SUBMISSION_UNKNOWN)
                fired += 1
        return fired

    def tick(self) -> int:
        """One pass of every worker over the due work; returns how many invoices were acted on."""
        now = self.clock.now()
        done = 0
        for rec in self.store.all([S.VALIDATING]):
            self.validate_pending(rec.id)
            done += 1
        for rec in self.store.all([S.READY_TO_SUBMIT]):
            if rec.next_attempt_at is not None and rec.next_attempt_at <= now:
                self.submit(rec.id)
                done += 1
        done += self.run_timers()
        for rec in self.store.all(POLL_STATES):
            if rec.next_poll_at is not None and rec.next_poll_at <= self.clock.now():
                self.poll(rec.id)
                done += 1
        return done

    def next_due(self) -> float | None:
        times = [t for r in self.store.all() for t in (r.next_attempt_at, r.next_poll_at, r.lease_expires_at, r.deadline_at)
                 if t is not None]
        return min(times) if times else None

    def run_until_idle(self, max_ticks: int = 10_000) -> None:
        """Runs the workers, letting the clock move to the next due timer whenever nothing is due."""
        for _ in range(max_ticks):
            if self.tick() == 0:
                due = self.next_due()
                if due is None:
                    return
                self.clock.sleep(max(0.0, due - self.clock.now()))
        raise RuntimeError("not idle after max_ticks")
