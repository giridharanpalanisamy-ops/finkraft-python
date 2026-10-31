"""Duplicate invoice numbers are rejected per seller; same content is replayed, never re-created."""
from __future__ import annotations

import pytest

from finkraft.models import InvoiceState as S

from conftest import SELLER, invoice


def test_same_number_same_content_is_an_idempotent_replay(app):
    first = app.service.upload(SELLER, invoice("INV-1"))
    again = app.service.upload(SELLER, invoice("INV-1"))
    assert (first.outcome, again.outcome) == ("CREATED", "REPLAY")
    assert again.invoice_id == first.invoice_id
    assert len(app.store.all()) == 1


def test_same_number_different_content_is_rejected(app):
    first = app.service.upload(SELLER, invoice("INV-1"))
    original = app.store.get(first.invoice_id)
    clash = app.service.upload(SELLER, invoice("INV-1", price="75"))
    assert clash.outcome == "DUPLICATE_INVOICE_NUMBER"
    assert clash.invoice_id == first.invoice_id
    after = app.store.get(first.invoice_id)
    assert (after.content_hash, after.version, after.invoice) == (original.content_hash, original.version, original.invoice)
    assert len(app.store.all()) == 1


def test_duplicate_numbers_are_scoped_per_seller(app):
    app.store.set_route("seller-2", "asp-a")
    a = app.service.upload(SELLER, invoice("INV-1"))
    b = app.service.upload("seller-2", invoice("INV-1", seller_trn="100000000000029"))
    assert (a.outcome, b.outcome) == ("CREATED", "CREATED")
    assert a.invoice_id != b.invoice_id


def test_new_number_with_known_content_is_created_as_a_possible_duplicate(app):
    first = app.service.upload(SELLER, invoice("INV-1"))
    second = app.service.upload(SELLER, invoice("INV-2"))
    assert second.outcome == "CREATED"
    assert second.possible_duplicate_of == first.invoice_id


def test_same_number_after_a_failure_becomes_a_new_revision(app):
    bad = invoice("INV-1")
    bad["lines"][0]["lineNet"] = "99"
    first = app.service.upload(SELLER, bad)
    app.service.run_until_idle()
    assert app.store.get(first.invoice_id).state == S.VALIDATION_FAILED

    fixed = app.service.upload(SELLER, invoice("INV-1"))
    assert fixed.outcome == "REVISION"
    assert fixed.invoice_id == first.invoice_id
    assert fixed.revision == 2
    app.service.run_until_idle()
    assert app.store.get(first.invoice_id).state == S.COMPLETED


def test_upload_without_a_number_is_refused(app):
    no_number = invoice()
    del no_number["invoiceNumber"]
    with pytest.raises(ValueError):
        app.service.upload(SELLER, no_number)
