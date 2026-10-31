"""Every (state, event) pair is either a legal transition, a ranked no-op, or rejected."""
from __future__ import annotations

import pytest

from finkraft.models import Event as E
from finkraft.models import InvoiceState as S
from finkraft.state_machine import InvalidTransition, implied_states, next_state

# The design's transition table, written out independently of the implementation.
SPEC = {
    (S.RECEIVED, E.VALIDATION_STARTED): S.VALIDATING,
    (S.VALIDATING, E.VALIDATION_PASSED): S.READY_TO_SUBMIT,
    (S.VALIDATING, E.VALIDATION_FAILED): S.VALIDATION_FAILED,
    (S.READY_TO_SUBMIT, E.CLAIMED): S.SUBMITTING,
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
    (S.ACCEPTED_BY_ASP, E.ASP_REJECTED): S.NEEDS_REVIEW,
    (S.DELIVERED, E.ASP_REJECTED): S.NEEDS_REVIEW,
    (S.REPORTED, E.ASP_REJECTED): S.NEEDS_REVIEW,
    (S.COMPLETED, E.ASP_REJECTED): S.NEEDS_REVIEW,
    (S.VALIDATION_FAILED, E.REVISION_UPLOADED): S.RECEIVED,
    (S.REJECTED, E.REVISION_UPLOADED): S.RECEIVED,
}
EVENT_RANK = {E.ASP_RECEIVED: 1, E.ASP_ACCEPTED: 2, E.BUYER_DELIVERED: 3, E.TAX_REPORTED: 3}
STATE_RANK = {S.SUBMITTING: 0, S.SUBMISSION_UNKNOWN: 0, S.SUBMISSION_PENDING: 1, S.ACCEPTED_BY_ASP: 2,
              S.DELIVERED: 3, S.REPORTED: 3, S.COMPLETED: 4}
RANK_TARGET = {E.ASP_RECEIVED: S.SUBMISSION_PENDING, E.ASP_ACCEPTED: S.ACCEPTED_BY_ASP,
               E.BUYER_DELIVERED: S.DELIVERED, E.TAX_REPORTED: S.REPORTED}

ALL_PAIRS = [(s, e) for s in S for e in E if e != E.SUBMIT_RETRYABLE]


@pytest.mark.parametrize(("state", "event"), ALL_PAIRS, ids=lambda x: str(x))
def test_every_pair_is_legal_a_ranked_no_op_or_rejected(state, event):
    if (state, event) in SPEC:
        assert next_state(state, event) == SPEC[(state, event)]
    elif event in EVENT_RANK and state in STATE_RANK:
        expected = RANK_TARGET[event] if EVENT_RANK[event] > STATE_RANK[state] else None
        assert next_state(state, event) == expected
    else:
        with pytest.raises(InvalidTransition):
            next_state(state, event)


def test_submit_retryable_depends_on_attempts_left():
    assert next_state(S.SUBMITTING, E.SUBMIT_RETRYABLE) == S.READY_TO_SUBMIT
    assert next_state(S.SUBMITTING, E.SUBMIT_RETRYABLE, max_attempts_reached=True) == S.NEEDS_REVIEW
    for s in S:
        if s != S.SUBMITTING:
            with pytest.raises(InvalidTransition):
                next_state(s, E.SUBMIT_RETRYABLE)


def test_skipped_status_jumps_ahead_and_records_what_it_implies():
    assert next_state(S.SUBMISSION_PENDING, E.BUYER_DELIVERED) == S.DELIVERED
    assert next_state(S.SUBMISSION_PENDING, E.TAX_REPORTED) == S.REPORTED
    assert implied_states(S.SUBMISSION_PENDING, S.REPORTED) == [S.ACCEPTED_BY_ASP]


def test_late_or_repeated_status_is_a_no_op():
    assert next_state(S.ACCEPTED_BY_ASP, E.ASP_RECEIVED) is None
    assert next_state(S.DELIVERED, E.BUYER_DELIVERED) is None
    assert next_state(S.COMPLETED, E.TAX_REPORTED) is None


def test_out_of_order_c3_and_c5_both_complete():
    assert next_state(S.REPORTED, E.BUYER_DELIVERED) == S.COMPLETED
    assert next_state(S.DELIVERED, E.TAX_REPORTED) == S.COMPLETED


def test_validation_events_are_illegal_once_submitted():
    with pytest.raises(InvalidTransition):
        next_state(S.COMPLETED, E.VALIDATION_PASSED)
