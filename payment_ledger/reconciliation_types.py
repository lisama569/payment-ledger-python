"""Types used by day-end reconciliation."""
from __future__ import annotations

from dataclasses import dataclass, field
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
class ReconcileScope:
    """Restricts which ledger entries a reconcile run looks at.

    * ``since_seq`` / ``until_seq``: ledger entry sequence bounds
      (``seq > since_seq`` and ``seq <= until_seq``). Incremental
      reconciliation is ``since_seq=<max_seq of the previous run>``.
    * ``since_time`` / ``until_time``: ISO-8601 ``created_at`` bounds
      (``created_at >= since_time`` and ``created_at < until_time``),
      e.g. one business day.

    All bounds are optional and combined with AND. ``None`` everywhere
    means a full reconcile.
    """

    since_seq: int | None = None
    until_seq: int | None = None
    since_time: str | None = None
    until_time: str | None = None

    def is_full(self) -> bool:
        return (
            self.since_seq is None
            and self.until_seq is None
            and self.since_time is None
            and self.until_time is None
        )


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
    # Seq of the ledger entry involved, if any; useful for scoping.
    ledger_seq: int | None = None


@dataclass
class ReconciliationReport:
    matched: list[ReconciliationItem]
    discrepancies: list[ReconciliationItem]
    # Journal metadata (None when the run was not persisted).
    run_id: int | None = None
    scope: ReconcileScope | None = None
    # Diff against the previous persisted run: which discrepancies are
    # new, which are still present, and which have disappeared.
    new_discrepancies: list[ReconciliationItem] = field(default_factory=list)
    ongoing_discrepancies: list[ReconciliationItem] = field(default_factory=list)
    resolved_discrepancies: list[ReconciliationItem] = field(default_factory=list)
