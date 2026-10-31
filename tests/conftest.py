from __future__ import annotations

from decimal import Decimal

import pytest

from finkraft.app import App, build_app
from finkraft.clock import FakeClock
from finkraft.config import Settings

SELLER = "seller-1"
SELLER_TRN = "100000000000003"


@pytest.fixture
def app() -> App:
    """Empty in-memory app (no mock data), one seller routed to ASP A, on a fake clock."""
    a = build_app(Settings(mock_data_enabled=False), FakeClock())
    a.store.set_route(SELLER, "asp-a")
    a.store.set_credentials(SELLER, "asp-a", "seller-key")
    return a


def invoice(number: str = "INV-1", price: str = "50", seller_trn: str = SELLER_TRN) -> dict:
    net = Decimal(price) * 2
    vat = (net * Decimal("0.05")).quantize(Decimal("0.01"))
    return {
        "sellerTrn": seller_trn, "buyerTrn": "100000000000011", "invoiceNumber": number,
        "issueDate": "2026-10-01", "currency": "AED",
        "lines": [{"description": "Consulting", "quantity": 2, "unitPrice": price, "vatCategory": "S",
                   "vatRate": 5, "lineNet": str(net), "lineVat": str(vat)}],
        "totals": {"netTotal": str(net), "vatTotal": str(vat), "grossTotal": str(net + vat)},
    }
