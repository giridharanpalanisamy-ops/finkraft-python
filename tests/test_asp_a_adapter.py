"""ASP A behaviour mapped to the adapter contract (RETRYABLE = provably not received, UNKNOWN = maybe)."""
from __future__ import annotations

import pytest

from finkraft.adapters.asp_a import AspAAdapter
from finkraft.adapters.base import AspCredentials, AspUnavailable, HttpResponse, SubmitOutcome, SubmitRequest, UnmappedStatus
from finkraft.clock import FakeClock
from finkraft.mock_asp_a import MockAspA
from finkraft.models import AspError, Event, Invoice

from conftest import invoice


class Scripted:
    """A transport that answers with whatever the test scripts."""

    def __init__(self, answer):
        self.answer = answer

    def request(self, method, path, body, headers, timeout):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def request(number="INV-1") -> SubmitRequest:
    return SubmitRequest("id-1", "ref-1", Invoice.from_dict(invoice(number)), AspCredentials("key"))


@pytest.mark.parametrize(("answer", "outcome"), [
    (HttpResponse(200, {"submission_id": "A-1", "status": "RECEIVED"}), SubmitOutcome.RECEIVED),
    (HttpResponse(200, {"submission_id": "A-1", "status": "VALIDATED"}), SubmitOutcome.RECEIVED),
    (HttpResponse(200, {"submission_id": "A-1", "status": "REJECTED"}), SubmitOutcome.REJECTED),
    (HttpResponse(409, {"submission_id": "A-1"}), SubmitOutcome.DUPLICATE),
    (HttpResponse(429, {}), SubmitOutcome.RETRYABLE),
    (ConnectionRefusedError("down"), SubmitOutcome.RETRYABLE),
    (TimeoutError("read timed out"), SubmitOutcome.UNKNOWN),
    (HttpResponse(500, {}), SubmitOutcome.UNKNOWN),
    (HttpResponse(503, {}), SubmitOutcome.UNKNOWN),
])
def test_submit_outcomes(answer, outcome):
    assert AspAAdapter(Scripted(answer)).submit(request()).outcome == outcome


def test_rejection_errors_are_canonical():
    answer = HttpResponse(200, {"submission_id": "A-1", "status": "REJECTED", "errors": {"code": "E104", "field": "BT-48"}})
    result = AspAAdapter(Scripted(answer)).submit(request())
    assert result.errors == (AspError(code="E104", field="BT-48", rule=None, message="E104"),)


@pytest.mark.parametrize(("raw", "event"), [
    ("RECEIVED", Event.ASP_RECEIVED), ("VALIDATED", Event.ASP_ACCEPTED), ("REJECTED", Event.ASP_REJECTED),
    ("DELIVERED", Event.BUYER_DELIVERED), ("REPORTED", Event.TAX_REPORTED),
])
def test_status_map(raw, event):
    assert AspAAdapter(Scripted(None)).to_event(raw) == event


def test_unknown_status_is_never_guessed():
    with pytest.raises(UnmappedStatus):
        AspAAdapter(Scripted(None)).to_event("ARCHIVED")


def test_against_the_mock_a_repeated_number_returns_409_with_the_existing_id():
    asp = MockAspA(FakeClock())
    adapter = AspAAdapter(asp)
    first = adapter.submit(request())
    second = adapter.submit(request())
    assert first.outcome == SubmitOutcome.RECEIVED
    assert second.outcome == SubmitOutcome.DUPLICATE and second.asp_ref == first.asp_ref
    assert asp.accepted("INV-1") == 1


def test_status_read_failures_raise():
    with pytest.raises(AspUnavailable):
        AspAAdapter(Scripted(TimeoutError())).get_status("A-1")
    with pytest.raises(AspUnavailable):
        AspAAdapter(Scripted(HttpResponse(404, {}))).get_status("A-1")
