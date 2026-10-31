"""In-memory storage. A lock stands in for database transactions, a version check for optimistic
locking, and a per-seller index for the unique (seller, invoice number) constraint."""
from __future__ import annotations

import copy
import threading
from collections.abc import Iterable

from .models import AuditEntry, InvoiceRecord, InvoiceState


class VersionConflict(Exception):
    pass


class DuplicateInvoiceNumber(Exception):
    pass


class InMemoryInvoiceStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._invoices: dict[str, InvoiceRecord] = {}
        self._by_number: dict[tuple[str, str], str] = {}
        self._processed: set[str] = set()
        self._routes: dict[str, str] = {}
        self._credentials: dict[tuple[str, str], str] = {}
        self._audit: list[AuditEntry] = []

    # --- invoices ---

    def insert(self, record: InvoiceRecord) -> None:
        with self.lock:
            key = (record.seller_id, record.invoice_number)
            if key in self._by_number:
                raise DuplicateInvoiceNumber(f"{record.invoice_number} already exists for seller {record.seller_id}")
            self._invoices[record.id] = copy.deepcopy(record)
            self._by_number[key] = record.id

    def get(self, invoice_id: str) -> InvoiceRecord:
        with self.lock:
            return copy.deepcopy(self._invoices[invoice_id])

    def find_by_number(self, seller_id: str, invoice_number: str) -> InvoiceRecord | None:
        with self.lock:
            invoice_id = self._by_number.get((seller_id, invoice_number))
            return None if invoice_id is None else copy.deepcopy(self._invoices[invoice_id])

    def find_same_content(self, seller_id: str, content_hash: str, other_than_number: str) -> str | None:
        with self.lock:
            for r in self._invoices.values():
                if r.seller_id == seller_id and r.content_hash == content_hash and r.invoice_number != other_than_number:
                    return r.id
            return None

    def save(self, record: InvoiceRecord) -> None:
        """Writes only if nobody changed the row since it was read; bumps the version."""
        with self.lock:
            current = self._invoices[record.id]
            if current.version != record.version:
                raise VersionConflict(f"{record.id}: expected version {record.version}, found {current.version}")
            record.version += 1
            self._invoices[record.id] = copy.deepcopy(record)

    def claim(self, invoice_id: str, expected_version: int, lease_until: float) -> InvoiceRecord | None:
        """Compare-and-set READY_TO_SUBMIT -> SUBMITTING. Exactly one caller wins per version."""
        with self.lock:
            r = self._invoices[invoice_id]
            if r.state != InvoiceState.READY_TO_SUBMIT or r.version != expected_version:
                return None
            r.state = InvoiceState.SUBMITTING
            r.version += 1
            r.attempt_count += 1
            r.lease_expires_at = lease_until
            r.next_attempt_at = None
            return copy.deepcopy(r)

    def all(self, states: Iterable[InvoiceState] | None = None) -> list[InvoiceRecord]:
        with self.lock:
            wanted = None if states is None else set(states)
            return [copy.deepcopy(r) for r in self._invoices.values() if wanted is None or r.state in wanted]

    # --- dedup ledger ---

    def is_processed(self, dedup_key: str) -> bool:
        with self.lock:
            return dedup_key in self._processed

    def mark_processed(self, dedup_key: str) -> None:
        with self.lock:
            self._processed.add(dedup_key)

    # --- routing and credentials (management data) ---

    def set_route(self, seller_id: str, asp_id: str) -> None:
        with self.lock:
            self._routes[seller_id] = asp_id

    def route(self, seller_id: str) -> str | None:
        with self.lock:
            return self._routes.get(seller_id)

    def set_credentials(self, seller_id: str, asp_id: str, api_key: str) -> None:
        with self.lock:
            self._credentials[(seller_id, asp_id)] = api_key

    def credentials(self, seller_id: str, asp_id: str) -> str | None:
        with self.lock:
            return self._credentials.get((seller_id, asp_id))

    # --- audit (insert-only) ---

    def append_audit(self, entry: AuditEntry) -> None:
        with self.lock:
            self._audit.append(entry)

    def audit(self, invoice_id: str) -> list[AuditEntry]:
        with self.lock:
            return [a for a in self._audit if a.invoice_id == invoice_id]

    def path(self, invoice_id: str) -> list[InvoiceState]:
        """Every state the invoice entered, in order."""
        return [a.to_state for a in self.audit(invoice_id) if a.to_state is not None and a.to_state != a.from_state]
