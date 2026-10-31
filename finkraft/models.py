"""Canonical types. Nothing here knows about any particular ASP."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any


class InvoiceState(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTING = "SUBMITTING"
    SUBMISSION_PENDING = "SUBMISSION_PENDING"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    ACCEPTED_BY_ASP = "ACCEPTED_BY_ASP"
    REJECTED = "REJECTED"
    DELIVERED = "DELIVERED"
    REPORTED = "REPORTED"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class Event(StrEnum):
    VALIDATION_STARTED = "VALIDATION_STARTED"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CLAIMED = "CLAIMED"
    SUBMIT_RETRYABLE = "SUBMIT_RETRYABLE"
    SUBMIT_TIMEOUT = "SUBMIT_TIMEOUT"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    ASP_RECEIVED = "ASP_RECEIVED"
    ASP_ACCEPTED = "ASP_ACCEPTED"
    ASP_REJECTED = "ASP_REJECTED"
    BUYER_DELIVERED = "BUYER_DELIVERED"
    TAX_REPORTED = "TAX_REPORTED"
    UNKNOWN_DEADLINE_PASSED = "UNKNOWN_DEADLINE_PASSED"
    REVISION_UPLOADED = "REVISION_UPLOADED"


@dataclass(frozen=True)
class AspError:
    code: str | None = None
    field: str | None = None
    rule: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class InvoiceLine:
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    vat_category: str | None
    vat_rate: Decimal | None  # percentage, e.g. 5 for the UAE standard rate
    line_net: Decimal | None
    line_vat: Decimal | None


@dataclass(frozen=True)
class Totals:
    net: Decimal | None
    vat: Decimal | None
    gross: Decimal | None


@dataclass(frozen=True)
class Invoice:
    """PINT AE aligned subset. Amounts are Decimal, never float."""

    seller_trn: str | None
    buyer_trn: str | None
    invoice_number: str | None
    issue_date: date | None
    currency: str | None
    lines: tuple[InvoiceLine, ...]
    totals: Totals | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Invoice:
        def dec(v: Any) -> Decimal | None:
            return None if v is None else Decimal(str(v))

        def text(v: Any) -> str | None:
            return None if v is None else str(v).strip()

        lines = tuple(
            InvoiceLine(
                description=text(l.get("description")),
                quantity=dec(l.get("quantity")),
                unit_price=dec(l.get("unitPrice")),
                vat_category=None if l.get("vatCategory") is None else str(l["vatCategory"]).strip().upper(),
                vat_rate=dec(l.get("vatRate")),
                line_net=dec(l.get("lineNet")),
                line_vat=dec(l.get("lineVat")),
            )
            for l in d.get("lines") or []
        )
        t = d.get("totals")
        totals = None if t is None else Totals(dec(t.get("netTotal")), dec(t.get("vatTotal")), dec(t.get("grossTotal")))
        currency = text(d.get("currency"))
        return cls(
            seller_trn=text(d.get("sellerTrn")),
            buyer_trn=text(d.get("buyerTrn")),
            invoice_number=text(d.get("invoiceNumber")),
            issue_date=None if d.get("issueDate") is None else date.fromisoformat(str(d["issueDate"]).strip()),
            currency=currency.upper() if currency else currency,
            lines=lines,
            totals=totals,
        )

    @classmethod
    def from_json(cls, text: str) -> Invoice:
        return cls.from_dict(json.loads(text, parse_float=Decimal))


@dataclass
class InvoiceRecord:
    """One row of the invoice store. Only InvoiceService changes it, through the store's version check."""

    id: str
    seller_id: str
    invoice_number: str
    invoice: Invoice
    content_hash: str
    state: InvoiceState = InvoiceState.RECEIVED
    version: int = 0
    revision: int = 1
    asp_id: str | None = None
    asp_ref: str | None = None
    client_ref: str | None = None
    attempt_count: int = 0
    poll_count: int = 0
    next_attempt_at: float | None = None
    next_poll_at: float | None = None
    lease_expires_at: float | None = None
    deadline_at: float | None = None
    possible_duplicate_of: str | None = None
    errors: list[AspError] = field(default_factory=list)


@dataclass(frozen=True)
class AuditEntry:
    invoice_id: str
    from_state: InvoiceState | None
    to_state: InvoiceState | None
    event: str
    dedup_key: str | None
    actor: str
    at: float
    detail: str = ""
