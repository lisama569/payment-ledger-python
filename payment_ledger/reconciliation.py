"""Day-end reconciliation against external settlement statements.

Reconciliation only READS committed ledger entries via a read-only
database connection. It never inserts, updates or deletes ledger data.
Recording a run writes to the separate ``reconciliation_runs`` table
only; fund-movement tables are never touched.

Matching model
--------------
Only fund-moving entries (CAPTURE/REFUND) are reconciled; AUTHORIZE
holds no settled funds.

1. Entries that carry an ``external_ref`` (registered at capture /
   refund time) are paired one-to-one with the statement line of the
   same reference; paired sides are then compared by amount and
   currency. A reference used more than once in the statement is
   reported as DUPLICATE_EXTERNAL_REF (the ledger itself rejects a
   second registration of the same reference at write time).
2. One-sided items stay distinguishable by direction:
   * statement line with no ledger counterpart -> MISSING_IN_LEDGER
     (settled outside, never booked here);
   * ledger entry whose reference is absent from the statement ->
     MISSING_IN_STATEMENT (booked here, not settled outside).
3. Ledger entries recorded before references existed (``external_ref``
   is NULL) and statement lines left over after reference pairing fall
   back to the legacy positional alignment within each
   ``(payment_id, entry_type)`` group, so historical data reconciles
   exactly as it did before this capability was added.

Scopes
------
A run may cover the whole ledger, a window (entry seq range and/or
creation-time range, e.g. one business day), or only the entries added
since the previous recorded run (incremental). Because filtering is by
entry attributes, a windowed/incremental run reaches the same
conclusions for its entries as a full run restricted to the same range.

Every recorded run is persisted with its resolved scope and a digest of
the statement, so the same inputs after a restart reproduce the same
conclusion, and two consecutive runs can be diffed into new /
persisting / resolved discrepancies.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

from .models import EntryType, LedgerEntry
from .repository import LedgerRepository, utc_now_iso
from .reconciliation_types import (
    DiscrepancyType,
    ReconciliationDiff,
    ReconciliationItem,
    ReconciliationReport,
    ReconciliationRun,
    ReconciliationScope,
    SettlementRecord,
    item_signature,
)

_MONEY_TYPES = (EntryType.CAPTURE, EntryType.REFUND)


def statement_digest(statement: list[SettlementRecord]) -> str:
    """Stable digest of a statement, stored with each recorded run."""
    canonical = json.dumps(
        [
            {
                "external_ref": s.external_ref,
                "payment_id": s.payment_id,
                "entry_type": s.entry_type.value,
                "amount": s.amount,
                "currency": s.currency,
            }
            for s in statement
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _in_scope(entry: LedgerEntry, scope: ReconciliationScope) -> bool:
    if scope.from_seq is not None and entry.seq <= scope.from_seq:
        return False
    if scope.to_seq is not None and entry.seq > scope.to_seq:
        return False
    if scope.from_time is not None and entry.created_at < scope.from_time:
        return False
    if scope.to_time is not None and entry.created_at > scope.to_time:
        return False
    return True


def _compare_pair(
    le: LedgerEntry, st: SettlementRecord, matched: list, discrepancies: list
) -> None:
    """Amount/currency comparison for a pair already aligned by reference
    or by legacy position."""
    if le.currency != st.currency:
        discrepancies.append(
            ReconciliationItem(
                discrepancy=DiscrepancyType.CURRENCY_MISMATCH,
                external_ref=st.external_ref,
                payment_id=le.payment_id,
                entry_type=le.entry_type,
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
                payment_id=le.payment_id,
                entry_type=le.entry_type,
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
                payment_id=le.payment_id,
                entry_type=le.entry_type,
                ledger_amount=le.amount,
                statement_amount=st.amount,
                currency=le.currency,
                detail="matched",
            )
        )


def _sort_key(item: ReconciliationItem) -> tuple:
    return (
        item.payment_id,
        item.entry_type.value,
        item.external_ref or "",
        item.ledger_amount if item.ledger_amount is not None else -1,
        item.statement_amount if item.statement_amount is not None else -1,
        item.discrepancy.value if item.discrepancy else "",
    )


class Reconciler:
    def __init__(self, repository: LedgerRepository) -> None:
        self.repo = repository

    # -- core matching -------------------------------------------------

    def _load_entries(self, scope: ReconciliationScope) -> list[LedgerEntry]:
        # Strictly read-only access; opened and closed within this call.
        conn = self.repo.read_connection()
        try:
            all_entries = self.repo.list_entries(conn)
        finally:
            conn.close()
        return [
            e
            for e in all_entries
            if e.entry_type in _MONEY_TYPES and _in_scope(e, scope)
        ]

    def reconcile(
        self,
        statement: list[SettlementRecord],
        scope: ReconciliationScope | None = None,
    ) -> ReconciliationReport:
        scope = scope or ReconciliationScope()
        return self._match(self._load_entries(scope), statement)

    def _match(
        self,
        ledger_entries: list[LedgerEntry],
        statement: list[SettlementRecord],
    ) -> ReconciliationReport:
        matched: list[ReconciliationItem] = []
        discrepancies: list[ReconciliationItem] = []

        # --- statement bookkeeping: first occurrence per reference -----
        stmt_by_ref: dict[str, SettlementRecord] = {}
        ref_counts: Counter[str] = Counter(s.external_ref for s in statement)
        for s in statement:
            stmt_by_ref.setdefault(s.external_ref, s)
        consumed_refs: set[str] = set()

        # Repeated external_ref: same settlement line fed more than once.
        for ref in sorted(ref_counts):
            if ref_counts[ref] > 1:
                s = stmt_by_ref[ref]
                discrepancies.append(
                    ReconciliationItem(
                        discrepancy=DiscrepancyType.DUPLICATE_EXTERNAL_REF,
                        external_ref=ref,
                        payment_id=s.payment_id,
                        entry_type=s.entry_type,
                        ledger_amount=None,
                        statement_amount=s.amount,
                        currency=s.currency,
                        detail=(
                            f"external_ref {ref!r} appears {ref_counts[ref]} "
                            f"times in statement"
                        ),
                    )
                )

        # --- pass 1: pair ledger entries carrying a reference ----------
        legacy_entries: list[LedgerEntry] = []
        for le in sorted(ledger_entries, key=lambda e: e.seq):
            if le.external_ref is None:
                legacy_entries.append(le)
                continue
            st = stmt_by_ref.get(le.external_ref)
            if st is None:
                discrepancies.append(
                    ReconciliationItem(
                        discrepancy=DiscrepancyType.MISSING_IN_STATEMENT,
                        external_ref=le.external_ref,
                        payment_id=le.payment_id,
                        entry_type=le.entry_type,
                        ledger_amount=le.amount,
                        statement_amount=None,
                        currency=le.currency,
                        detail=(
                            "ledger entry registered external_ref "
                            f"{le.external_ref!r} but the statement has no "
                            "such settlement line"
                        ),
                    )
                )
                continue
            consumed_refs.add(le.external_ref)
            _compare_pair(le, st, matched, discrepancies)

        # --- pass 2: legacy positional alignment for the rest ----------
        leftover_statement = [
            s for s in statement if s.external_ref not in consumed_refs
        ]
        self._reconcile_positionally(legacy_entries, leftover_statement, matched, discrepancies)

        matched.sort(key=_sort_key)
        discrepancies.sort(key=_sort_key)
        return ReconciliationReport(matched=matched, discrepancies=discrepancies)

    def _reconcile_positionally(
        self,
        ledger_entries: list[LedgerEntry],
        statement: list[SettlementRecord],
        matched: list[ReconciliationItem],
        discrepancies: list[ReconciliationItem],
    ) -> None:
        """Legacy alignment used for entries/lines without a usable
        reference: group by (payment_id, entry_type) and compare the
        seq-ordered ledger entries with the ref-ordered statement lines
        position by position."""
        ledger_groups: dict[tuple[str, EntryType], list[LedgerEntry]] = defaultdict(list)
        for e in ledger_entries:
            ledger_groups[(e.payment_id, e.entry_type)].append(e)

        stmt_groups: dict[tuple[str, EntryType], list[SettlementRecord]] = defaultdict(list)
        for s in statement:
            stmt_groups[(s.payment_id, s.entry_type)].append(s)

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
                _compare_pair(le, st, matched, discrepancies)

    # -- recorded runs ---------------------------------------------------

    def _resolve_scope(self, scope: ReconciliationScope) -> ReconciliationScope:
        """Turn an incremental scope into a concrete seq window."""
        if not scope.incremental:
            return scope
        with self.repo.transaction() as conn:
            last = self.repo.latest_run_max_seq(conn)
        return ReconciliationScope(
            from_seq=last if last is not None else scope.from_seq,
            to_seq=scope.to_seq,
            from_time=scope.from_time,
            to_time=scope.to_time,
            incremental=True,
        )

    def reconcile_and_record(
        self,
        statement: list[SettlementRecord],
        scope: ReconciliationScope | None = None,
    ) -> ReconciliationRun:
        """Reconcile and persist the conclusion as a new run.

        Concurrent callers are serialized by the repository write lock;
        each run lands as its own append-only row, and the fund-movement
        tables are never written.
        """
        resolved = self._resolve_scope(scope or ReconciliationScope())
        # Load entries once so the recorded max_seq covers exactly the
        # entries this conclusion was computed from.
        entries = self._load_entries(resolved)
        report = self._match(entries, statement)
        max_seq = max((e.seq for e in entries), default=resolved.to_seq or 0)
        with self.repo.transaction() as conn:
            run_id = self.repo.save_reconciliation_run(
                conn,
                resolved,
                statement_digest(statement),
                max_seq,
                report.matched,
                report.discrepancies,
                utc_now_iso(),
            )
        return ReconciliationRun(
            run_id=run_id,
            created_at=utc_now_iso(),
            scope=resolved,
            statement_digest=statement_digest(statement),
            matched=report.matched,
            discrepancies=report.discrepancies,
        )

    def get_run(self, run_id: int) -> ReconciliationRun | None:
        with self.repo.transaction() as conn:
            return self.repo.get_reconciliation_run(conn, run_id)

    def list_runs(self) -> list[ReconciliationRun]:
        with self.repo.transaction() as conn:
            return self.repo.list_reconciliation_runs(conn)

    def diff_runs(self, older_run_id: int, newer_run_id: int) -> ReconciliationDiff:
        """Diff the discrepancies of two recorded runs (older -> newer)."""
        with self.repo.transaction() as conn:
            older = self.repo.get_reconciliation_run(conn, older_run_id)
            newer = self.repo.get_reconciliation_run(conn, newer_run_id)
        if older is None:
            raise KeyError(f"reconciliation run {older_run_id} not found")
        if newer is None:
            raise KeyError(f"reconciliation run {newer_run_id} not found")

        older_sigs = Counter(item_signature(d) for d in older.discrepancies)
        newer_sigs = Counter(item_signature(d) for d in newer.discrepancies)

        def pick(run_items, sig_pool):
            """Take items whose signature is still present in the pool."""
            taken = []
            for item in run_items:
                sig = item_signature(item)
                if sig_pool[sig] > 0:
                    sig_pool[sig] -= 1
                    taken.append(item)
            return taken

        common = older_sigs & newer_sigs
        new_pool = newer_sigs - common
        resolved_pool = older_sigs - common

        return ReconciliationDiff(
            older_run_id=older_run_id,
            newer_run_id=newer_run_id,
            new_discrepancies=pick(newer.discrepancies, new_pool),
            persisting_discrepancies=pick(newer.discrepancies, Counter(common)),
            resolved_discrepancies=pick(older.discrepancies, resolved_pool),
        )
