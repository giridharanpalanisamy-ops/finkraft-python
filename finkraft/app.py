"""Wires the in-memory store, the mock ASP A and the service together."""
from __future__ import annotations

from dataclasses import dataclass

from .adapters.asp_a import ASP_A, AspAAdapter
from .clock import Clock, FakeClock
from .config import Settings
from .mock_asp_a import MockAspA
from .service import InvoiceService
from .store import InMemoryInvoiceStore


@dataclass
class App:
    store: InMemoryInvoiceStore
    service: InvoiceService
    asp_a: MockAspA
    clock: Clock
    loaded: list | None = None  # what load_mock_data uploaded, when mock data is enabled


def build_app(settings: Settings | None = None, clock: Clock | None = None, mock_step_seconds: float = 60.0) -> App:
    """Everything in memory. Mock data (sample sellers and invoices) is loaded when
    settings.mock_data_enabled is true, which is the default."""
    settings = settings or Settings()
    clock = clock or FakeClock()
    store = InMemoryInvoiceStore()
    asp_a = MockAspA(clock, step_seconds=mock_step_seconds)
    service = InvoiceService(store, {ASP_A: AspAAdapter(asp_a, status_api_key="finkraft-status-key")}, clock, settings)
    app = App(store, service, asp_a, clock)
    if settings.mock_data_enabled:
        from .mock_data import load_mock_data
        app.loaded = load_mock_data(app)
    return app
