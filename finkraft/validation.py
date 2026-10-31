"""Deterministic PINT AE subset. Each rule has an id so errors are traceable."""
from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .models import AspError, Invoice

_TRN = re.compile(r"\d{15}")
_CENT = Decimal("0.01")
PERMITTED_VAT = {"S": Decimal("5"), "Z": Decimal("0"), "E": Decimal("0"), "O": Decimal("0")}


def _round(v: Decimal) -> Decimal:
    return v.quantize(_CENT, rounding=ROUND_HALF_UP)


def _err(rule: str, field: str, message: str) -> AspError:
    return AspError(code=rule, field=field, rule=rule, message=message)


def validate(inv: Invoice, today: date) -> list[AspError]:
    errors: list[AspError] = []

    # FK-R001 required fields
    for name in ("seller_trn", "buyer_trn", "invoice_number", "issue_date", "currency"):
        if not getattr(inv, name):
            errors.append(_err("FK-R001", name, f"{name} is required"))
    if not inv.lines:
        errors.append(_err("FK-R001", "lines", "at least one line is required"))
    for i, l in enumerate(inv.lines):
        for name in ("description", "quantity", "unit_price", "vat_category", "vat_rate", "line_net", "line_vat"):
            if getattr(l, name) in (None, ""):
                errors.append(_err("FK-R001", f"lines[{i}].{name}", f"{name} is required"))
    if inv.totals is None or None in (inv.totals.net, inv.totals.vat, inv.totals.gross):
        errors.append(_err("FK-R001", "totals", "net, vat and gross totals are required"))

    # FK-R002 TRNs are 15 digits
    for name in ("seller_trn", "buyer_trn"):
        value = getattr(inv, name)
        if value and not _TRN.fullmatch(value):
            errors.append(_err("FK-R002", name, f"{name} must be 15 digits"))

    # FK-R003 issue date not in the future
    if inv.issue_date and inv.issue_date > today:
        errors.append(_err("FK-R003", "issue_date", f"issue date {inv.issue_date} is in the future"))

    for i, l in enumerate(inv.lines):
        # FK-R005 lineNet = quantity x unitPrice
        if None not in (l.quantity, l.unit_price, l.line_net) and _round(l.quantity * l.unit_price) != _round(l.line_net):
            errors.append(_err("FK-R005", f"lines[{i}].line_net", f"should be {_round(l.quantity * l.unit_price)}"))
        # FK-R006 lineVat = lineNet x vatRate / 100
        if None not in (l.line_net, l.vat_rate, l.line_vat):
            expected = _round(l.line_net * l.vat_rate / 100)
            if expected != _round(l.line_vat):
                errors.append(_err("FK-R006", f"lines[{i}].line_vat", f"should be {expected}"))
        # FK-R008 VAT category and rate are a permitted pair
        if l.vat_category is not None and l.vat_rate is not None:
            rate = PERMITTED_VAT.get(l.vat_category)
            if rate is None:
                errors.append(_err("FK-R008", f"lines[{i}].vat_category", f"unknown category {l.vat_category}"))
            elif rate != l.vat_rate:
                errors.append(_err("FK-R008", f"lines[{i}].vat_rate", f"category {l.vat_category} requires {rate}%"))

    # FK-R007 totals equal the sums of the lines
    t = inv.totals
    if t and inv.lines and all(l.line_net is not None and l.line_vat is not None for l in inv.lines):
        net = sum((l.line_net for l in inv.lines), Decimal(0))
        vat = sum((l.line_vat for l in inv.lines), Decimal(0))
        if t.net is not None and _round(net) != _round(t.net):
            errors.append(_err("FK-R007", "totals.net", f"should be {_round(net)}"))
        if t.vat is not None and _round(vat) != _round(t.vat):
            errors.append(_err("FK-R007", "totals.vat", f"should be {_round(vat)}"))
        if None not in (t.net, t.vat, t.gross) and _round(t.net + t.vat) != _round(t.gross):
            errors.append(_err("FK-R007", "totals.gross", f"should be {_round(t.net + t.vat)}"))
    return errors
