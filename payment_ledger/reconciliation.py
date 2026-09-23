"""Day-end reconciliation against external settlement statements.

Reconciliation only READS committed ledger entries via a read-only
database connection. It never inserts, updates or deletes ledger data.

Matching model
--------------
Only fund-moving entries (CAPTURE/REFUND) are reconciled; AUTHORIZE
holds no settled funds. Entries and statement lines are grouped by
``(payment_id, entry_type)``. Within each group:

1. DUPLICATE_IN_LEDGER: the ledger contains more entries than the
   statement has lines. The extra (highest-seq) ledger entries have no
   settlement counterpart -> repeated/duplicate booking in the ledger.
2. MISSING_IN_LEDGER: the statement contains more lines than the
   ledger has entries -> money settled but never booked.
3. The first ``min(n_ledger, n_statement)`` pairs are compared by
   position (ledger seq order vs statement ref order):
   * equal amount, equal currency -> MATCHED
   * equal amount, different currency -> CURRENCY_MISMATCH
   * different amount -> AMOUNT_MISMATCH
4. A statement external_ref appearing more than once is additionally
   reported as DUPLICATE_IN_LEDGER (duplicate settlement feed).
"""
from __future__ import annotations

from collections import defaultdict

from .models import EntryType, LedgerEntry
from .repository import LedgerRepository
from .reconciliation_types import (
    DiscrepancyType,
    ReconciliationItem,
    ReconciliationReport,
    SettlementRecord,
)

_MONEY_TYPES = (EntryType.CAPTURE, EntryType.REFUND)


class Reconciler:
    def __init__(self, repository: LedgerRepository) -> None:
        self.repo = repository

    def reconcile(self, statement: list[SettlementRecord]) -> ReconciliationReport:
        # Strictly read-only access; opened and closed within this call.
        conn = self.repo.read_connection()
        try:
            all_entries = self.repo.list_entries(conn)
        finally:
            conn.close()

        ledger_entries = [e for e in all_entries if e.entry_type in _MONEY_TYPES]

        ledger_groups: dict[tuple[str, EntryType], list[LedgerEntry]] = defaultdict(list)
        for e in ledger_entries:
            ledger_groups[(e.payment_id, e.entry_type)].append(e)

        stmt_groups: dict[tuple[str, EntryType], list[SettlementRecord]] = defaultdict(list)
        for s in statement:
            stmt_groups[(s.payment_id, s.entry_type)].append(s)

        matched: list[ReconciliationItem] = []
        discrepancies: list[ReconciliationItem] = []

        all_keys = set(ledger_groups) | set(stmt_groups)
        for key in sorted(all_keys, key=lambda k: (k[0], k[1].value)):
            payment_id, entry_type = key
            l_entries = sorted(ledger_groups.get(key, []), key=lambda e: e.seq)
            s_lines = sorted(stmt_groups.get(key, []), key=lambda s: s.external_ref)

            n_ledger, n_stmt = len(l_entries), len(s_lines)
            pair_count = min(n_ledger, n_stmt)

            # Duplicate ledger entries (booked more times than settled).
            for le in l_entries[pair_count:]:
                discrepancies.append(
                    ReconciliationItem(
                        discrepancy=DiscrepancyType.DUPLICATE_IN_LEDGER,
                        external_ref=None,
                        payment_id=payment_id,
                        entry_type=entry_type,
                        ledger_amount=le.amount,
                        statement_amount=None,
                        currency=le.currency,
                        detail=(
                            f"duplicate ledger booking: {n_ledger} ledger entries vs "
                            f"{n_stmt} statement line(s); entry seq={le.seq} has no settlement"
                        ),
                    )
                )

            # Missing ledger entries (settled but never booked).
            for st in s_lines[pair_count:]:
                discrepancies.append(
                    ReconciliationItem(
                        discrepancy=DiscrepancyType.MISSING_IN_LEDGER,
                        external_ref=st.external_ref,
                        payment_id=payment_id,
                        entry_type=entry_type,
                        ledger_amount=None,
                        statement_amount=st.amount,
                        currency=st.currency,
                        detail="statement line has no corresponding ledger entry",
                    )
                )

            # Pair-wise comparison of the aligned prefix.
            for le, st in zip(l_entries[:pair_count], s_lines[:pair_count]):
                if le.currency != st.currency:
                    discrepancies.append(
                        ReconciliationItem(
                            discrepancy=DiscrepancyType.CURRENCY_MISMATCH,
                            external_ref=st.external_ref,
                            payment_id=payment_id,
                            entry_type=entry_type,
                            ledger_amount=le.amount,
                            statement_amount=st.amount,
                            currency=f"ledger={le.currency} statement={st.currency}",
                            detail="currency code differs between ledger and statement",
                        )
                    )
                elif le.amount != st.amount:
                    discrepancies.append(
                        ReconciliationItem(
                            discrepancy=DiscrepancyType.AMOUNT_MISMATCH,
                            external_ref=st.external_ref,
                            payment_id=payment_id,
                            entry_type=entry_type,
                            ledger_amount=le.amount,
                            statement_amount=st.amount,
                            currency=le.currency,
                            detail=f"ledger amount {le.amount} != statement amount {st.amount}",
                        )
                    )
                else:
                    matched.append(
                        ReconciliationItem(
                            discrepancy=None,
                            external_ref=st.external_ref,
                            payment_id=payment_id,
                            entry_type=entry_type,
                            ledger_amount=le.amount,
                            statement_amount=st.amount,
                            currency=le.currency,
                            detail="matched",
                        )
                    )

        # Repeated external_ref: same settlement line fed more than once.
        ref_lines: dict[str, list[SettlementRecord]] = defaultdict(list)
        for s in statement:
            ref_lines[s.external_ref].append(s)
        for ref, lines in sorted(ref_lines.items()):
            if len(lines) > 1:
                s = lines[0]
                discrepancies.append(
                    ReconciliationItem(
                        discrepancy=DiscrepancyType.DUPLICATE_IN_LEDGER,
                        external_ref=ref,
                        payment_id=s.payment_id,
                        entry_type=s.entry_type,
                        ledger_amount=None,
                        statement_amount=s.amount,
                        currency=s.currency,
                        detail=f"external_ref {ref!r} appears {len(lines)} times in statement",
                    )
                )

        return ReconciliationReport(matched=matched, discrepancies=discrepancies)
