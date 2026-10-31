"""The one table-driven state machine. A pure function: it never touches storage."""
from __future__ import annotations

from .models import Event as E
from .models import InvoiceState as S


class InvalidTransition(Exception):
    def __init__(self, state: S, event: E, reason: str = ""):
        super().__init__(f"Illegal transition: {event} in state {state}" + (f" ({reason})" if reason else ""))
        self.state = state
        self.event = event


TRANSITIONS: dict[tuple[S, E], S] = {
    (S.RECEIVED, E.VALIDATION_STARTED): S.VALIDATING,
    (S.VALIDATING, E.VALIDATION_PASSED): S.READY_TO_SUBMIT,
    (S.VALIDATING, E.VALIDATION_FAILED): S.VALIDATION_FAILED,
    (S.READY_TO_SUBMIT, E.CLAIMED): S.SUBMITTING,
    # (SUBMITTING, SUBMIT_RETRYABLE) depends on attempts left: see next_state()
    (S.SUBMITTING, E.ASP_RECEIVED): S.SUBMISSION_PENDING,
    (S.SUBMITTING, E.ASP_REJECTED): S.REJECTED,
    (S.SUBMITTING, E.SUBMIT_TIMEOUT): S.SUBMISSION_UNKNOWN,
    (S.SUBMITTING, E.LEASE_EXPIRED): S.SUBMISSION_UNKNOWN,
    (S.SUBMISSION_PENDING, E.ASP_ACCEPTED): S.ACCEPTED_BY_ASP,
    (S.SUBMISSION_PENDING, E.ASP_REJECTED): S.REJECTED,
    (S.SUBMISSION_UNKNOWN, E.ASP_RECEIVED): S.SUBMISSION_PENDING,
    (S.SUBMISSION_UNKNOWN, E.ASP_REJECTED): S.REJECTED,
    (S.SUBMISSION_UNKNOWN, E.UNKNOWN_DEADLINE_PASSED): S.NEEDS_REVIEW,
    (S.ACCEPTED_BY_ASP, E.BUYER_DELIVERED): S.DELIVERED,
    (S.ACCEPTED_BY_ASP, E.TAX_REPORTED): S.REPORTED,
    (S.DELIVERED, E.TAX_REPORTED): S.COMPLETED,
    (S.REPORTED, E.BUYER_DELIVERED): S.COMPLETED,
    # The ASP contradicting an earlier acceptance needs a human.
    (S.ACCEPTED_BY_ASP, E.ASP_REJECTED): S.NEEDS_REVIEW,
    (S.DELIVERED, E.ASP_REJECTED): S.NEEDS_REVIEW,
    (S.REPORTED, E.ASP_REJECTED): S.NEEDS_REVIEW,
    (S.COMPLETED, E.ASP_REJECTED): S.NEEDS_REVIEW,
    (S.VALIDATION_FAILED, E.REVISION_UPLOADED): S.RECEIVED,
    (S.REJECTED, E.REVISION_UPLOADED): S.RECEIVED,
}

# Rank rule for ASP progress events: late statuses are no-ops, skipped statuses jump ahead.
EVENT_RANK = {E.ASP_RECEIVED: 1, E.ASP_ACCEPTED: 2, E.BUYER_DELIVERED: 3, E.TAX_REPORTED: 3}
STATE_RANK = {
    S.SUBMITTING: 0, S.SUBMISSION_UNKNOWN: 0, S.SUBMISSION_PENDING: 1, S.ACCEPTED_BY_ASP: 2,
    S.DELIVERED: 3, S.REPORTED: 3, S.COMPLETED: 4,
}
RANK_TARGET = {
    E.ASP_RECEIVED: S.SUBMISSION_PENDING, E.ASP_ACCEPTED: S.ACCEPTED_BY_ASP,
    E.BUYER_DELIVERED: S.DELIVERED, E.TAX_REPORTED: S.REPORTED,
}

POLL_STATES = frozenset({S.SUBMISSION_PENDING, S.ACCEPTED_BY_ASP, S.DELIVERED, S.REPORTED})


def next_state(state: S, event: E, *, max_attempts_reached: bool = False) -> S | None:
    """Returns the next state, or None for a ranked no-op. Raises InvalidTransition otherwise."""
    if event == E.SUBMIT_RETRYABLE:
        if state == S.SUBMITTING:
            return S.NEEDS_REVIEW if max_attempts_reached else S.READY_TO_SUBMIT
        raise InvalidTransition(state, event)
    target = TRANSITIONS.get((state, event))
    if target is not None:
        return target
    if event in EVENT_RANK and state in STATE_RANK:
        return RANK_TARGET[event] if EVENT_RANK[event] > STATE_RANK[state] else None
    raise InvalidTransition(state, event)


def implied_states(from_state: S, to_state: S) -> list[S]:
    """States a ranked jump passed over, for the audit trail (PENDING -> REPORTED implies ACCEPTED_BY_ASP)."""
    if from_state not in STATE_RANK or to_state not in STATE_RANK or to_state == S.COMPLETED:
        return []
    lo, hi = STATE_RANK[from_state], STATE_RANK[to_state]
    return [s for s in (S.SUBMISSION_PENDING, S.ACCEPTED_BY_ASP) if lo < STATE_RANK[s] < hi]
