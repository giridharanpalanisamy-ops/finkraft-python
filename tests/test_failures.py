"""Timeouts, retries, duplicate submissions, late / repeated / out-of-order statuses, concurrency."""
from __future__ import annotations

import threading

import pytest

from finkraft.models import Event
from finkraft.models import InvoiceState as S
from finkraft.state_machine import InvalidTransition

from conftest import SELLER, invoice


def submitted(app, number="INV-1"):
    r = app.service.upload(SELLER, invoice(number))
    app.service.tick()
    return app.store.get(r.invoice_id)


def test_timeout_after_accept_is_resent_safely_and_the_asp_has_it_once(app):
    app.asp_a.fault_for("INV-1").timeout_after_accept = 1
    r = app.service.upload(SELLER, invoice("INV-1"))
    app.service.run_until_idle()
    assert app.store.get(r.invoice_id).state == S.COMPLETED
    assert app.asp_a.posts_by_invoice["INV-1"] == 2  # original + resend, which got 409
    assert app.asp_a.accepted("INV-1") == 1           # never submitted twice
    assert S.SUBMISSION_UNKNOWN not in app.store.path(r.invoice_id)


def test_outcome_still_unknown_after_resends_goes_to_review_after_the_reconcile_window(app):
    app.asp_a.fault_for("INV-1").fail_5xx = 4  # original + 3 resends all ambiguous
    r = app.service.upload(SELLER, invoice("INV-1"))
    app.service.tick()
    assert app.store.get(r.invoice_id).state == S.SUBMISSION_UNKNOWN
    app.service.run_until_idle()
    assert app.store.get(r.invoice_id).state == S.NEEDS_REVIEW
    assert S.READY_TO_SUBMIT not in app.store.path(r.invoice_id)[3:]  # never blindly resubmitted


def test_provably_unsent_submits_are_retried_with_backoff_then_reviewed(app):
    app.asp_a.faults.refuse_connections = True
    r = app.service.upload(SELLER, invoice("INV-1"))
    app.service.run_until_idle()
    rec = app.store.get(r.invoice_id)
    assert rec.state == S.NEEDS_REVIEW
    assert rec.attempt_count == app.service.settings.max_attempts
    claims = [a for a in app.store.audit(r.invoice_id) if a.event == Event.CLAIMED]
    assert len(claims) == 6
    gaps = [b.at - a.at for a, b in zip(claims, claims[1:])]
    assert gaps == sorted(gaps), "backoff grows between attempts"


def test_retry_succeeds_once_the_asp_is_back(app):
    app.asp_a.faults.refuse_connections = True
    r = app.service.upload(SELLER, invoice("INV-1"))
    app.service.tick()
    assert app.store.get(r.invoice_id).state == S.READY_TO_SUBMIT
    app.asp_a.faults.refuse_connections = False
    app.service.run_until_idle()
    assert app.store.get(r.invoice_id).state == S.COMPLETED


def test_a_worker_that_dies_after_claiming_leads_to_unknown_never_back_to_ready(app):
    r = app.service.upload(SELLER, invoice("INV-1"))
    app.service.validate_pending(r.invoice_id)
    rec = app.store.get(r.invoice_id)
    assert app.store.claim(rec.id, rec.version, app.clock.now() + 120) is not None  # ...and the worker crashes
    app.clock.advance(121)
    app.service.run_timers()
    assert app.store.get(r.invoice_id).state == S.SUBMISSION_UNKNOWN
    assert app.asp_a.total_calls == 0


def test_redelivered_submit_makes_one_claim_and_one_asp_call(app):
    r = app.service.upload(SELLER, invoice("INV-1"))
    app.service.validate_pending(r.invoice_id)
    app.service.submit(r.invoice_id)
    app.service.submit(r.invoice_id)  # duplicate delivery of the same work item
    assert app.asp_a.posts_by_invoice["INV-1"] == 1
    assert sum(1 for a in app.store.audit(r.invoice_id) if a.event == Event.CLAIMED) == 1


def test_two_workers_racing_for_one_invoice_exactly_one_claims(app):
    for round_ in range(50):
        r = app.service.upload(SELLER, invoice(f"RACE-{round_}"))
        app.service.validate_pending(r.invoice_id)
        version = app.store.get(r.invoice_id).version
        barrier = threading.Barrier(2)
        wins = []

        def worker():
            barrier.wait()
            wins.append(app.store.claim(r.invoice_id, version, app.clock.now() + 120))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sum(w is not None for w in wins) == 1


def test_the_same_status_twice_is_applied_once(app):
    rec = submitted(app)
    for _ in range(2):
        app.service.on_status(rec.id, "asp-a", rec.asp_ref, "VALIDATED", Event.ASP_ACCEPTED)
    assert app.store.get(rec.id).state == S.ACCEPTED_BY_ASP
    assert sum(1 for a in app.store.audit(rec.id) if a.event == Event.ASP_ACCEPTED) == 1


def test_out_of_order_statuses_still_complete(app):
    rec = submitted(app)
    app.service.on_status(rec.id, "asp-a", rec.asp_ref, "REPORTED", Event.TAX_REPORTED)   # C5 first
    assert app.store.get(rec.id).state == S.REPORTED
    app.service.on_status(rec.id, "asp-a", rec.asp_ref, "DELIVERED", Event.BUYER_DELIVERED)  # C3 late
    assert app.store.get(rec.id).state == S.COMPLETED
    app.service.on_status(rec.id, "asp-a", rec.asp_ref, "VALIDATED", Event.ASP_ACCEPTED)  # very late: no-op
    assert app.store.get(rec.id).state == S.COMPLETED
    assert not app.service.dead_letters


def test_rejection_after_acceptance_needs_review(app):
    rec = submitted(app)
    app.service.on_status(rec.id, "asp-a", rec.asp_ref, "VALIDATED", Event.ASP_ACCEPTED)
    app.service.on_status(rec.id, "asp-a", rec.asp_ref, "REJECTED", Event.ASP_REJECTED)
    assert app.store.get(rec.id).state == S.NEEDS_REVIEW


def test_an_illegal_event_changes_nothing(app):
    rec = submitted(app)
    before = app.store.get(rec.id)
    with pytest.raises(InvalidTransition):
        app.service.apply_event(rec.id, Event.VALIDATION_PASSED, "bogus-1", actor="test")
    after = app.store.get(rec.id)
    assert (after.state, after.version) == (before.state, before.version)
    assert not app.store.is_processed("bogus-1")


def test_rate_limit_is_never_exceeded(app):
    for i in range(40):
        app.service.upload(SELLER, invoice(f"BULK-{i:03d}", price=str(10 + i)))
    app.service.run_until_idle()
    assert all(r.state == S.COMPLETED for r in app.store.all())
    assert app.asp_a.peak_requests_per_second <= 10
    assert app.asp_a.distinct_invoices == 40
