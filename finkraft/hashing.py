"""Content hash: the same business content always gives the same hash."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .models import Invoice

_TWO_PLACES = Decimal("0.01")


def normalize_decimal(v: Decimal | None) -> str | None:
    """'100', '100.0' and '1E+2' all become '100.00'; '0.125' stays '0.125' (never rounded)."""
    if v is None:
        return None
    d = v.normalize()
    if d.as_tuple().exponent > -2:
        d = d.quantize(_TWO_PLACES)
    return format(d, "f")


def content_hash(invoice: Invoice) -> str:
    """SHA-256 over sorted-key JSON of the business content. The invoice number is excluded, so the
    duplicate rule can spot the same content uploaded under a new number."""
    t = invoice.totals
    canonical = {
        "sellerTrn": invoice.seller_trn,
        "buyerTrn": invoice.buyer_trn,
        "issueDate": invoice.issue_date.isoformat() if invoice.issue_date else None,
        "currency": invoice.currency,
        "lines": [
            {
                "description": l.description,
                "quantity": normalize_decimal(l.quantity),
                "unitPrice": normalize_decimal(l.unit_price),
                "vatCategory": l.vat_category,
                "vatRate": normalize_decimal(l.vat_rate),
                "lineNet": normalize_decimal(l.line_net),
                "lineVat": normalize_decimal(l.line_vat),
            }
            for l in invoice.lines
        ],
        "totals": None if t is None else {
            "netTotal": normalize_decimal(t.net),
            "vatTotal": normalize_decimal(t.vat),
            "grossTotal": normalize_decimal(t.gross),
        },
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
