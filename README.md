# Finkraft: invoice submission core (Python, in-memory)

A minimal, runnable version of the design: the invoice state machine, one adapter contract for both ASPs with a mock ASP A behind it, per-seller duplicate rejection, and the failure handling around submission and status tracking. There is no database, web server or UI: storage is in memory, and the mock ASP A runs in the same process.

## Run it

Requires Python 3.11 or later. There are no runtime dependencies; pytest is the only dev dependency.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

.venv/bin/pytest                         # 224 tests, under a second
.venv/bin/python -m finkraft.demo        # runs the mock data end to end
.venv/bin/python -m finkraft.demo -v     # ...and logs every transition
```

The demo prints the outcome of each sample upload, the final state and full path of every invoice, and what mock ASP A saw:

```
Invoice    Seller       Upload                    Final state        Scenario
ACME-0001  seller-acme  CREATED                   COMPLETED          happy path
ACME-0003  seller-acme  CREATED                   COMPLETED          ASP A times out after accepting (resend gets 409)
ACME-0004  seller-acme  CREATED                   VALIDATION_FAILED  fails validation (lineNet is wrong)
ACME-0005  seller-acme  CREATED                   REJECTED           ASP A rejects it
ACME-0001  seller-acme  REPLAY                    COMPLETED          re-upload of ACME-0001 (same content: idempotent replay)
ACME-0001  seller-acme  DUPLICATE_INVOICE_NUMBER  COMPLETED          ACME-0001 again with different content (duplicate number: rejected)
ACME-0001  seller-zen   CREATED                   COMPLETED          same number as Acme's, different seller (allowed)
ZEN-0100   seller-zen   CREATED                   COMPLETED          ASP A skips from RECEIVED to DELIVERED between polls
...
```

Time is simulated (`FakeClock`), so polls, backoff, leases and deadlines that would take minutes in real time finish instantly and deterministically.

## Mock data (on by default)

`finkraft/data/mock_data.json` holds two sellers routed to ASP A and nine sample uploads, one per scenario above. Some carry a `fault` that tells mock ASP A how to misbehave for that invoice (`timeout_after_accept`, `reject`, `jump_to_delivered`).

- `build_app()` loads it into the in-memory store at start-up.
- Turn it off with `FINKRAFT_MOCK_DATA=0`, `python -m finkraft.demo --no-mock-data`, or `build_app(Settings(mock_data_enabled=False))`. The tests start empty and build their own data.

## How the code maps to the design

| Design element | Code |
|---|---|
| Invoice state machine, rejecting invalid transitions | `finkraft/state_machine.py`: one transition table, the rank rule for late or skipped ASP statuses, `InvalidTransition` for anything else |
| Adapter contract for both ASPs | `finkraft/adapters/base.py`: `AspAdapter` (`submit`, `get_status`, `parse_webhook`, `to_event`), `AspCapabilities`, `SubmitResult`, `SubmitOutcome`, `StatusUpdate` |
| Mock adapter | `finkraft/adapters/asp_a.py` (ASP A wire format and status map) talking to `finkraft/mock_asp_a.py` (in-memory ASP A: 409 on repeated numbers, 10 rps with 429, status progression, fault injection) |
| Duplicate invoice number rejection per seller | `InvoiceService.upload` with the store's unique (seller, invoice number) index; content hash in `finkraft/hashing.py` |
| Single writer, no lost or double submissions | `InvoiceService.apply_event` (dedup ledger, version-checked save, audit) and `InMemoryInvoiceStore.claim` (compare-and-set READY_TO_SUBMIT to SUBMITTING) |
| Validation | `finkraft/validation.py`: rules FK-R001 to FK-R008 (required fields, TRN, issue date, line and VAT arithmetic, totals, VAT category) |
| Workers (validator, submitter, poller, timers) | `InvoiceService.tick()` and `run_until_idle()` |

### The two outcomes that matter most

| Adapter outcome | Meaning | What the service does |
|---|---|---|
| `RETRYABLE` (429, connection refused) | The ASP provably did not get it | Back to READY_TO_SUBMIT with exponential backoff and jitter; NEEDS_REVIEW after 6 attempts |
| `UNKNOWN` (timeout, 5xx) | The ASP may have it | ASP A deduplicates, so resend at 1 s, 2 s, 4 s; a 409 means "already have it". Still unknown: SUBMISSION_UNKNOWN, then NEEDS_REVIEW after the reconcile window. Never blindly resubmitted |

## What the tests cover

| File | Covers |
|---|---|
| `test_state_machine.py` | Every (state, event) pair: legal, ranked no-op, or rejected (169 cases); retry limit; skips and late statuses |
| `test_duplicates.py` | Replay, duplicate number rejected, per-seller scope, possible duplicate, revision after failure |
| `test_asp_a_adapter.py` | Every ASP A response mapped to an outcome; status map; error shape; unknown status never guessed |
| `test_submission_flow.py` | Happy path to COMPLETED, validation failure then revision, ASP rejection, ASP pinned at READY_TO_SUBMIT, skipped status |
| `test_failures.py` | Timeout after accept (one invoice at the ASP), unknown outcome to NEEDS_REVIEW, retry with backoff, dead worker's lease, redelivered submit, two racing workers, duplicate / out-of-order / contradictory statuses, illegal event changes nothing, rate limit never above 10 rps |
| `test_hashing_and_validation.py`, `test_mock_data.py` | Hash stability, validation rules, mock data on/off, the demo |

## Assumptions

- **Invoice number excluded from the content hash**, so "new number, same content" can be flagged as a possible duplicate.
- **Decimals normalised without rounding:** "100" and "100.00" hash the same, 0.125 stays 0.125.
- **`vatRate` is a percentage** (5 = 5 %).
- **ASP A's 409 body includes the existing `submission_id`.**
- **ASP A's VALIDATED is its own acceptance** (maps to ASP_ACCEPTED), not Finkraft's validation.
- **The ASP is pinned when the invoice becomes READY_TO_SUBMIT**; switching a seller's ASP affects only invoices not yet READY_TO_SUBMIT.
- **401/403 count as RETRYABLE** (a credentials problem, not the invoice's fault); other 4xx count as REJECTED.

## What I skipped

- **ASP B adapter** (webhooks and signature checks). The contract has `parse_webhook`, and the late, duplicate and out-of-order handling it needs is already exercised through `on_status`. Only the ASP B wire format is missing.
- **Persistence and messaging.** The in-memory store stands in for Postgres (lock = transaction, version check = optimistic locking, index = unique constraint). Workers are a `tick()` loop instead of Kafka consumers, and there is no outbox.
- **HTTP.** The adapter talks to a `Transport`; the mock implements it in-process. A real `urllib`/`httpx` transport is a few lines.
- **Unmatched statuses** (a webhook before the submit response) and **operator resolution** of NEEDS_REVIEW.
- **Full PINT AE coverage and enrichment;** eight core rules only.

## What I would do next

1. ASP B adapter and webhook entry point, with signature verification and parking of statuses that arrive before the submit response.
2. Postgres store with the same interface: unique constraints, `UPDATE ... WHERE version = ?` for the claim, a transactional outbox.
3. Real workers on a queue keyed by invoice id, so one invoice's events are processed in order.
4. Decide what happens when ASP A is first seen at REPORTED. Polling only shows the current status, so an invoice can reach REPORTED without ever seeing DELIVERED, and would then never complete. I would treat ASP A's REPORTED as implying delivery, since its lifecycle is linear.
5. Metrics (invoices per state and age, ASP latency and errors) and an operator view for NEEDS_REVIEW.

## Layout

```
finkraft/
  models.py          canonical types, states, events
  state_machine.py   transition table and rank rule
  hashing.py         content hash
  validation.py      FK-R001..FK-R008
  store.py           in-memory store (lock, version check, unique index, dedup ledger, audit)
  service.py         InvoiceService: upload, apply_event, submit, poll, timers
  adapters/base.py   the adapter contract
  adapters/asp_a.py  ASP A adapter
  mock_asp_a.py      in-memory ASP A
  rate_limiter.py    one permit per 1/rate seconds
  clock.py           SystemClock and FakeClock
  config.py          settings (mock data on by default)
  mock_data.py       loads data/mock_data.json
  app.py             wiring
  demo.py            python -m finkraft.demo
tests/
```
