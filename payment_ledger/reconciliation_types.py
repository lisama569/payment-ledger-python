"""Types used by day-end reconciliation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import EntryType


class DiscrepancyType(str, Enum):
    # The three required, distinguishable discrepancy kinds.
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    MISSING_IN_LEDGER = "MISSING_IN_LEDGER"
    DUPLICATE_IN_LEDGER = "DUPLICATE_IN_LEDGER"
    # Extra, informational kinds.
    MISSING_IN_STATEMENT = "MISSING_IN_STATEMENT"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    # The same external settlement reference was fed more than once.
    DUPLICATE_EXTERNAL_REF = "DUPLICATE_EXTERNAL_REF"


@dataclass
class SettlementRecord:
    """One line from the external settlement statement."""

    external_ref: str
    payment_id: str
    entry_type: EntryType
    amount: int
    currency: str


@dataclass
class ReconciliationItem:
    discrepancy: DiscrepancyType | None
    external_ref: str | None
    payment_id: str
    entry_type: EntryType
    ledger_amount: int | None
    statement_amount: int | None
    currency: str | None
    detail: str


@dataclass
class ReconciliationReport:
    matched: list[ReconciliationItem]
    discrepancies: list[ReconciliationItem]


@dataclass
class ReconciliationScope:
    """Restricts which ledger entries a reconciliation run covers.

    * ``from_seq`` / ``to_seq``: only entries with
      ``from_seq < seq <= to_seq`` (seq is the append-only entry order).
    * ``from_time`` / ``to_time``: only entries created inside
      ``[from_time, to_time]`` (ISO-8601 strings, e.g. one business day).
    * ``incremental``: start right after the entry covered by the most
      recent recorded run (resolves to a concrete ``from_seq``). The
      recorded high-water mark is monotonic: an incremental run whose
      window contains no new entries keeps the previous ``from_seq``
      rather than resetting it, and concurrent runs never move the
      position backwards.
    """

    from_seq: int | None = None
    to_seq: int | None = None
    from_time: str | None = None
    to_time: str | None = None
    incremental: bool = False


@dataclass
class ReconciliationRun:
    """A persisted reconciliation conclusion, reproducible after restart."""

    run_id: int
    created_at: str
    scope: ReconciliationScope
    statement_digest: str
    matched: list[ReconciliationItem]
    discrepancies: list[ReconciliationItem]


@dataclass
class ReconciliationDiff:
    """What changed between two recorded runs (older -> newer)."""

    older_run_id: int
    newer_run_id: int
    new_discrepancies: list[ReconciliationItem]
    persisting_discrepancies: list[ReconciliationItem]
    resolved_discrepancies: list[ReconciliationItem]


def item_signature(item: ReconciliationItem) -> tuple:
    """Stable identity of a report item, used to diff two runs."""
    return (
        item.discrepancy.value if item.discrepancy else None,
        item.external_ref,
        item.payment_id,
        item.entry_type.value,
        item.ledger_amount,
        item.statement_amount,
        item.currency,
    )
