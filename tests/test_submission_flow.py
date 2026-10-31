"""Main transitions end to end, in memory, against mock ASP A."""
from __future__ import annotations

from finkraft.models import InvoiceState as S

from conftest import SELLER, invoice


def test_happy_path_reaches_completed_through_polling(app):
    r = app.service.upload(SELLER, invoice("INV-1"))
    assert r.state == S.VALIDATING
    app.service.run_until_idle()
    rec = app.store.get(r.invoice_id)
    assert rec.state == S.COMPLETED
    assert app.store.path(r.invoice_id) == [
        S.RECEIVED, S.VALIDATING, S.READY_TO_SUBMIT, S.SUBMITTING, S.SUBMISSION_PENDING,
        S.ACCEPTED_BY_ASP, S.DELIVERED, S.COMPLETED]
    assert rec.asp_ref.startswith("A-")
    assert app.asp_a.accepted("INV-1") == 1


def test_validation_failure_then_corrected_revision_completes(app):
    bad = invoice("INV-1")
    bad["totals"]["grossTotal"] = "1"
    r = app.service.upload(SELLER, bad)
    app.service.run_until_idle()
    rec = app.store.get(r.invoice_id)
    assert rec.state == S.VALIDATION_FAILED
    assert [e.rule for e in rec.errors] == ["FK-R007"]
    assert app.asp_a.total_calls == 0  # nothing invalid ever reaches the ASP

    app.service.add_revision(r.invoice_id, invoice("INV-1"))
    app.service.run_until_idle()
    assert app.store.get(r.invoice_id).state == S.COMPLETED


def test_asp_rejection_keeps_the_asp_errors_and_allows_a_revision(app):
    app.asp_a.fault_for("INV-1").reject = 1
    r = app.service.upload(SELLER, invoice("INV-1"))
    app.service.run_until_idle()
    rec = app.store.get(r.invoice_id)
    assert rec.state == S.REJECTED
    assert rec.errors[0].code == "E104" and rec.errors[0].field == "BT-48"

    app.service.add_revision(r.invoice_id, invoice("INV-1", price="60"))
    app.service.run_until_idle()
    assert app.store.get(r.invoice_id).state == S.COMPLETED


def test_asp_is_pinned_when_ready_to_submit_and_routing_changes_only_affect_later_invoices(app):
    r = app.service.upload(SELLER, invoice("INV-1"))
    app.service.tick()  # validated and submitted
    assert app.store.get(r.invoice_id).asp_id == "asp-a"
    app.store.set_route(SELLER, "asp-b")  # the seller switches ASP
    app.service.run_until_idle()
    assert app.store.get(r.invoice_id).state == S.COMPLETED
    assert app.store.get(r.invoice_id).asp_id == "asp-a"


def test_skipped_status_between_polls_jumps_straight_to_delivered(app):
    app.asp_a.fault_for("INV-1").jump_to = "DELIVERED"
    r = app.service.upload(SELLER, invoice("INV-1"))
    app.service.run_until_idle()
    path = app.store.path(r.invoice_id)
    assert S.ACCEPTED_BY_ASP not in path
    assert path[path.index(S.SUBMISSION_PENDING) + 1] == S.DELIVERED
    jump = next(a for a in app.store.audit(r.invoice_id) if a.to_state == S.DELIVERED)
    assert jump.from_state == S.SUBMISSION_PENDING and jump.detail == "implies ACCEPTED_BY_ASP"
    assert app.store.get(r.invoice_id).state == S.COMPLETED
