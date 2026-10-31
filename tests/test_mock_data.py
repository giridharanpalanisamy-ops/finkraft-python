"""Mock data is on by default and runs end to end in memory."""
from __future__ import annotations

from finkraft import demo
from finkraft.app import build_app
from finkraft.config import Settings
from finkraft.models import InvoiceState as S


def test_mock_data_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("FINKRAFT_MOCK_DATA", raising=False)
    assert Settings().mock_data_enabled is True
    app = build_app()
    assert app.loaded and len(app.store.all()) == 7  # 9 uploads: one replay, one duplicate rejected


def test_mock_data_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("FINKRAFT_MOCK_DATA", "0")
    app = build_app()
    assert app.loaded is None and app.store.all() == []


def test_every_mock_scenario_ends_where_expected(monkeypatch):
    monkeypatch.delenv("FINKRAFT_MOCK_DATA", raising=False)
    app = build_app()
    app.service.run_until_idle()
    final = {(i.seller_id, i.invoice_number): app.store.get(i.result.invoice_id).state for i in app.loaded}
    assert final[("seller-acme", "ACME-0001")] == S.COMPLETED
    assert final[("seller-acme", "ACME-0002")] == S.COMPLETED
    assert final[("seller-acme", "ACME-0003")] == S.COMPLETED
    assert final[("seller-acme", "ACME-0004")] == S.VALIDATION_FAILED
    assert final[("seller-acme", "ACME-0005")] == S.REJECTED
    assert final[("seller-zen", "ACME-0001")] == S.COMPLETED
    assert final[("seller-zen", "ZEN-0100")] == S.COMPLETED
    outcomes = [i.result.outcome for i in app.loaded]
    assert outcomes.count("REPLAY") == 1 and outcomes.count("DUPLICATE_INVOICE_NUMBER") == 1
    assert app.asp_a.accepted("ACME-0003") == 1


def test_demo_runs(capsys, monkeypatch):
    monkeypatch.delenv("FINKRAFT_MOCK_DATA", raising=False)
    assert demo.main([]) == 0
    out = capsys.readouterr().out
    assert "COMPLETED" in out and "peak" in out
    assert demo.main(["--no-mock-data"]) == 0


def test_demo_honours_the_environment_switch(capsys, monkeypatch):
    monkeypatch.setenv("FINKRAFT_MOCK_DATA", "0")
    assert demo.main([]) == 0
    assert "Mock data is disabled" in capsys.readouterr().out
