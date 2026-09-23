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
