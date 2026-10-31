from __future__ import annotations

from datetime import date
from decimal import Decimal

from finkraft.hashing import content_hash, normalize_decimal
from finkraft.models import Invoice
from finkraft.validation import validate

from conftest import invoice


def test_hash_ignores_key_order_whitespace_and_trailing_zeros():
    a = Invoice.from_json('{"sellerTrn":"100000000000003","buyerTrn":"100000000000011","invoiceNumber":"INV-1",'
                          '"issueDate":"2026-10-01","currency":"AED","lines":[{"description":"Widget","quantity":2,'
                          '"unitPrice":50,"vatCategory":"S","vatRate":5,"lineNet":100,"lineVat":5}],'
                          '"totals":{"netTotal":100,"vatTotal":5,"grossTotal":105}}')
    b = Invoice.from_json('{ "totals": {"grossTotal":"105.00","vatTotal":"5.0","netTotal":"100.00"}, "currency":" aed ",'
                          ' "lines":[{"lineVat":"5.00","lineNet":"100.00","vatRate":"5.00","vatCategory":" s ",'
                          '"unitPrice":"50.0","quantity":"2","description":"  Widget "}], "issueDate":"2026-10-01",'
                          ' "invoiceNumber":"INV-1", "buyerTrn":" 100000000000011", "sellerTrn":"100000000000003 " }')
    assert content_hash(a) == content_hash(b)


def test_hash_excludes_the_invoice_number_but_not_amounts():
    base = Invoice.from_dict(invoice("INV-1"))
    assert content_hash(base) == content_hash(Invoice.from_dict(invoice("INV-2")))
    assert content_hash(base) != content_hash(Invoice.from_dict(invoice("INV-1", price="50.01")))


def test_decimals_are_normalised_without_rounding():
    assert normalize_decimal(Decimal("100")) == "100.00"
    assert normalize_decimal(Decimal("1E+2")) == "100.00"
    assert normalize_decimal(Decimal("0.125")) == "0.125"


def test_valid_invoice_passes_and_broken_one_names_each_rule():
    today = date(2026, 11, 1)
    assert validate(Invoice.from_dict(invoice()), today) == []
    bad = invoice()
    bad["sellerTrn"] = "123"
    bad["issueDate"] = "2027-01-01"
    bad["lines"][0]["lineNet"] = "99"
    bad["lines"][0]["vatRate"] = 7
    rules = {e.rule for e in validate(Invoice.from_dict(bad), today)}
    assert {"FK-R002", "FK-R003", "FK-R005", "FK-R007", "FK-R008"} <= rules
