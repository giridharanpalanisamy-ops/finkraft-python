"""Runs the mock data end to end in memory and prints what happened to every invoice.

    python -m finkraft.demo                       # mock data on (default)
    python -m finkraft.demo --no-mock-data        # start empty
    FINKRAFT_MOCK_DATA=0 python -m finkraft.demo  # same, from the environment
"""
from __future__ import annotations

import argparse
import logging

from .app import build_app
from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-mock-data", action="store_true", help="do not load the sample sellers and invoices")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every transition")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")

    # FINKRAFT_MOCK_DATA decides by default (on unless set to 0); --no-mock-data always turns it off.
    settings = Settings(mock_data_enabled=False) if args.no_mock_data else Settings()
    app = build_app(settings)
    if not app.loaded:
        print("Mock data is disabled: nothing to process. Run without --no-mock-data to load the samples.")
        return 0

    start = app.clock.now()
    app.service.run_until_idle()

    print(f"{'Invoice':<10} {'Seller':<12} {'Upload':<25} {'Final state':<18} Scenario")
    print("-" * 110)
    for item in app.loaded:
        final = app.store.get(item.result.invoice_id).state
        print(f"{item.invoice_number:<10} {item.seller_id:<12} {item.result.outcome:<25} {final:<18} {item.label}")
    print()
    print("Paths:")
    seen = set()
    for item in app.loaded:
        if item.result.invoice_id in seen:
            continue
        seen.add(item.result.invoice_id)
        path = " -> ".join(app.store.path(item.result.invoice_id))
        print(f"  {item.invoice_number} ({item.seller_id}): {path}")
    print()
    print(f"Simulated time: {app.clock.now() - start:.0f} s. Mock ASP A: {app.asp_a.distinct_invoices} invoices received, "
          f"{app.asp_a.total_calls} calls, peak {app.asp_a.peak_requests_per_second} requests/s.")
    if app.service.dead_letters:
        print(f"Dead letters: {len(app.service.dead_letters)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
