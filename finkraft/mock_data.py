"""Loads the sample sellers and invoices in data/mock_data.json into the in-memory app."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from typing import TYPE_CHECKING

from .service import UploadResult

if TYPE_CHECKING:
    from .app import App


@dataclass(frozen=True)
class LoadedInvoice:
    label: str
    seller_id: str
    invoice_number: str
    result: UploadResult


def read_mock_data() -> dict:
    text = resources.files("finkraft").joinpath("data/mock_data.json").read_text()
    return json.loads(text, parse_float=Decimal)


def load_mock_data(app: App, data: dict | None = None) -> list[LoadedInvoice]:
    data = data or read_mock_data()
    for seller in data["sellers"]:
        app.store.set_route(seller["id"], seller["asp"])
        app.store.set_credentials(seller["id"], seller["asp"], seller["apiKey"])
    loaded = []
    for item in data["invoices"]:
        number = item["invoice"]["invoiceNumber"]
        _apply_fault(app, number, item.get("fault"))
        result = app.service.upload(item["seller"], item["invoice"])
        loaded.append(LoadedInvoice(item["label"], item["seller"], number, result))
    return loaded


def _apply_fault(app: App, number: str, fault: str | None) -> None:
    if fault is None:
        return
    f = app.asp_a.fault_for(number)
    if fault == "timeout_after_accept":
        f.timeout_after_accept = 1
    elif fault == "reject":
        f.reject = 1
    elif fault == "jump_to_delivered":
        f.jump_to = "DELIVERED"
    else:
        raise ValueError(f"unknown fault {fault!r} in mock data")
