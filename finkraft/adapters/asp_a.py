"""ASP A: synchronous POST /invoices, polling GET /invoices/{submission_id}, 409 on a repeated
invoice number, 10 requests/second, occasional timeouts after the invoice was accepted."""
from __future__ import annotations

import json
from typing import Protocol

from ..models import AspError, Event, Invoice
from .base import (
    AspAdapter, AspCapabilities, AspUnavailable, HttpResponse, StatusMode, StatusUpdate, SubmitOutcome,
    SubmitRequest, SubmitResult, UnmappedStatus,
)

ASP_A = "asp-a"

STATUS_MAP = {
    "RECEIVED": Event.ASP_RECEIVED,
    "VALIDATED": Event.ASP_ACCEPTED,  # ASP A's own check, never Finkraft's validation states
    "REJECTED": Event.ASP_REJECTED,
    "DELIVERED": Event.BUYER_DELIVERED,  # reached the buyer (C3)
    "REPORTED": Event.TAX_REPORTED,  # reported to the FTA (C5)
}


class Transport(Protocol):
    """HTTP as the adapter needs it. Raises TimeoutError (sent, no answer) or ConnectionRefusedError (never sent)."""

    def request(self, method: str, path: str, body: dict | None, headers: dict[str, str], timeout: float) -> HttpResponse: ...


class AspAAdapter(AspAdapter):
    asp_id = ASP_A
    capabilities = AspCapabilities(StatusMode.POLL, native_dedup=True, rate_limit_rps=10, safe_to_retry_on_timeout=True)

    def __init__(self, transport: Transport, status_api_key: str = "", read_timeout: float = 10.0):
        self._transport = transport
        self._status_api_key = status_api_key
        self._read_timeout = read_timeout

    def submit(self, request: SubmitRequest) -> SubmitResult:
        try:
            res = self._transport.request("POST", "/invoices", to_asp_format(request.invoice),
                                          {"X-Api-Key": request.credentials.api_key or ""}, self._read_timeout)
        except ConnectionRefusedError as e:
            return SubmitResult(SubmitOutcome.RETRYABLE, raw_body=str(e))
        except TimeoutError as e:
            return SubmitResult(SubmitOutcome.UNKNOWN, raw_body=str(e))
        raw = json.dumps(res.body)
        if res.status >= 500:
            return SubmitResult(SubmitOutcome.UNKNOWN, raw_body=raw)
        if res.status == 200:
            status = res.body.get("status")
            outcome = SubmitOutcome.REJECTED if self.to_event(status) == Event.ASP_REJECTED else SubmitOutcome.RECEIVED
            return SubmitResult(outcome, res.body.get("submission_id"), status, to_errors(res.body.get("errors")), raw)
        if res.status == 409:
            return SubmitResult(SubmitOutcome.DUPLICATE, res.body.get("submission_id"), raw_body=raw)
        if res.status in (429, 401, 403):
            return SubmitResult(SubmitOutcome.RETRYABLE, raw_body=raw)
        return SubmitResult(SubmitOutcome.REJECTED, errors=(AspError(code=f"HTTP_{res.status}"),), raw_body=raw)

    def get_status(self, asp_ref: str) -> StatusUpdate:
        try:
            res = self._transport.request("GET", f"/invoices/{asp_ref}", None, {"X-Api-Key": self._status_api_key},
                                          self._read_timeout)
        except (TimeoutError, ConnectionRefusedError) as e:
            raise AspUnavailable(str(e)) from e
        if res.status != 200:
            raise AspUnavailable(f"ASP A answered {res.status} for {asp_ref}")
        status = res.body.get("status")
        return StatusUpdate(asp_ref, None, status, self.to_event(status), res.body.get("updated_at"),
                            to_errors(res.body.get("errors")), json.dumps(res.body))

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[StatusUpdate]:
        raise NotImplementedError("ASP A has no webhooks")

    def to_event(self, raw_status: str) -> Event:
        try:
            return STATUS_MAP[raw_status]
        except KeyError:
            raise UnmappedStatus(f"ASP A status not mapped: {raw_status}") from None


def to_errors(raw) -> tuple[AspError, ...]:
    """{"code": "E104", "field": "BT-48"} (or a list of them) to AspError(code, field, None, code)."""
    if not raw:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    return tuple(AspError(code=e.get("code"), field=e.get("field"), rule=None, message=e.get("code")) for e in items)


def to_asp_format(inv: Invoice) -> dict:
    return {
        "invoice_number": inv.invoice_number,
        "seller_trn": inv.seller_trn,
        "buyer_trn": inv.buyer_trn,
        "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
        "currency": inv.currency,
        "lines": [
            {"description": l.description, "quantity": str(l.quantity), "unit_price": str(l.unit_price),
             "vat_category": l.vat_category, "vat_rate": str(l.vat_rate), "net_amount": str(l.line_net),
             "vat_amount": str(l.line_vat)}
            for l in inv.lines
        ],
        "totals": {"net": str(inv.totals.net), "vat": str(inv.totals.vat), "gross": str(inv.totals.gross)},
    }
