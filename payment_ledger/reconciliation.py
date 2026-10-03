"""Day-end reconciliation against external settlement statements.

Reconciliation only READS committed ledger entries via a read-only
database connection. It never inserts, updates or deletes ledger data.
Each run's conclusion is appended to a separate journal table
(``reconciliation_runs``) so results survive restarts and consecutive
runs can be diffed; the journal never touches fund movements.

Matching model
--------------
Only fund-moving entries (CAPTURE/REFUND) are reconciled; AUTHORIZE
holds no settled funds.

1. Entries that registered an ``external_ref`` are matched against
   statement lines **by settlement reference first**, one-to-one; a
   matched pair is then compared on amount and currency.
   * statement ref with no ledger entry  -> MISSING_IN_LEDGER
     (settled outside, never booked here)
   * ledger ref with no statement line   -> MISSING_IN_STATEMENT
     (booked here, never settled outside)
   These two one-sided directions are reported as distinct kinds.
   * the same ref on multiple ledger entries, or multiple statement
     lines, or both -> DUPLICATE_IN_LEDGER (ref reused).
2. Legacy entries recorded without an ``external_ref`` keep the
   original positional matching: grouped by ``(payment_id,
   entry_type)``, aligned by ledger ``seq`` vs statement
   ``external_ref`` order, compared pairwise. Statement lines whose
   reference is not registered on any ledger entry also join this
   pool, so old data produces exactly the same conclusions as before
   the reference capability existed.

Scoping
-------
A run may restrict the ledger entries it looks at (sequence or
``created_at`` window). Because reference-based matching is
independent per reference, a scoped run over a window reaches the
same conclusions for that window as a full run restricted to the
same entries.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .models import EntryType, LedgerEntry
from .repository import LedgerRepository, utc_now_iso
from .reconciliation_types import (
    DiscrepancyType,
    ReconcileScope,
    ReconciliationItem,
    ReconciliationReport,
    SettlementRecord,
)

_MONEY_TYPES = (EntryType.CAPTURE, EntryType.REFUND)


def _in_scope(entry: LedgerEntry, scope: ReconcileScope) -> bool:
    if scope.since_seq is not None and entry.seq <= scope.since_seq:
        return False
    if scope.until_seq is not None and entry.seq > scope.until_seq:
        return False
    if scope.since_time is not None and entry.created_at < scope.since_time:
        return False
    if scope.until_time is not None and entry.created_at >= scope.until_time:
        return False
    return True


def _item_to_dict(item: ReconciliationItem) -> dict:
    return {
        "discrepancy": item.discrepancy.value if item.discrepancy else None,
        "external_ref": item.external_ref,
        "payment_id": item.payment_id,
        "entry_type": item.entry_type.value,
        "ledger_amount": item.ledger_amount,
        "statement_amount": item.statement_amount,
        "currency": item.currency,
        "detail": item.detail,
        "ledger_seq": item.ledger_seq,
    }


def _item_from_dict(data: dict) -> ReconciliationItem:
    return ReconciliationItem(
        discrepancy=DiscrepancyType(data["discrepancy"]) if data["discrepancy"] else None,
        external_ref=data["external_ref"],
        payment_id=data["payment_id"],
        entry_type=EntryType(data["entry_type"]),
        ledger_amount=data["ledger_amount"],
        statement_amount=data["statement_amount"],
        currency=data["currency"],
        detail=data["detail"],
        ledger_seq=data.get("ledger_seq"),
    )


def _item_key(item: ReconciliationItem) -> str:
    """Stable identity of a discrepancy, used to diff consecutive runs."""
    return json.dumps(
        {
            "discrepancy": item.discrepancy.value if item.discrepancy else None,
            "external_ref": item.external_ref,
            "payment_id": item.payment_id,
            "entry_type": item.entry_type.value,
            "ledger_amount": item.ledger_amount,
            "statement_amount": item.statement_amount,
            "currency": item.currency,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _statement_hash(statement: list[SettlementRecord]) -> str:
    lines = sorted(
        json.dumps(
            {
                "external_ref": s.external_ref,
                "payment_id": s.payment_id,
                "entry_type": s.entry_type.value,
                "amount": s.amount,
                "currency": s.currency,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        for s in statement
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _sort_key(item: ReconciliationItem):
    return (
        item.payment_id,
        item.entry_type.value,
        item.external_ref or "",
        item.ledger_seq if item.ledger_seq is not None else -1,
        item.discrepancy.value if item.discrepancy else "",
    )


def _compare_pair(
    le: LedgerEntry,
    st: SettlementRecord,
    matched: list[ReconciliationItem],
    discrepancies: list[ReconciliationItem],
) -> None:
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
                ledger_seq=le.seq,
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
                ledger_seq=le.seq,
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
                ledger_seq=le.seq,
            )
        )


def _match(
    entries: list[LedgerEntry],
    statement: list[SettlementRecord],
) -> tuple[list[ReconciliationItem], list[ReconciliationItem]]:
    matched: list[ReconciliationItem] = []
    discrepancies: list[ReconciliationItem] = []

    stmt_by_ref: dict[str, list[SettlementRecord]] = defaultdict(list)
    for s in statement:
        stmt_by_ref[s.external_ref].append(s)
    for lines in stmt_by_ref.values():
        lines.sort(key=lambda s: (s.payment_id, s.entry_type.value, s.amount))

    ledger_by_ref: dict[str, list[LedgerEntry]] = defaultdict(list)
    legacy_entries: list[LedgerEntry] = []
    for e in entries:
        if e.external_ref is not None:
            ledger_by_ref[e.external_ref].append(e)
        else:
            legacy_entries.append(e)

    consumed_refs: set[str] = set()

    # -- 1. reference-based matching ----------------------------------
    for ref in sorted(ledger_by_ref):
        l_entries = sorted(ledger_by_ref[ref], key=lambda e: e.seq)
        s_lines = stmt_by_ref.get(ref, [])
        consumed_refs.add(ref)

        first, extra_entries = l_entries[0], l_entries[1:]
        for le in extra_entries:
            discrepancies.append(
                ReconciliationItem(
                    discrepancy=DiscrepancyType.DUPLICATE_IN_LEDGER,
                    external_ref=ref,
                    payment_id=le.payment_id,
                    entry_type=le.entry_type,
                    ledger_amount=le.amount,
                    statement_amount=None,
                    currency=le.currency,
                    detail=(
                        f"external_ref {ref!r} is registered on "
                        f"{len(l_entries)} ledger entries; seq={le.seq} is a reuse"
                    ),
                    ledger_seq=le.seq,
                )
            )

        if not s_lines:
            discrepancies.append(
                ReconciliationItem(
                    discrepancy=DiscrepancyType.MISSING_IN_STATEMENT,
                    external_ref=ref,
                    payment_id=first.payment_id,
                    entry_type=first.entry_type,
                    ledger_amount=first.amount,
                    statement_amount=None,
                    currency=first.currency,
                    detail="ledger entry has a settlement reference that "
                    "does not appear in the statement",
                    ledger_seq=first.seq,
                )
            )
        else:
            _compare_pair(first, s_lines[0], matched, discrepancies)

    # -- 2. legacy positional matching --------------------------------
    # Statement lines whose reference is not registered on any ledger
    # entry join the legacy pool together with ref-less entries.
    legacy_stmt: list[SettlementRecord] = []
    for s in statement:
        if s.external_ref not in consumed_refs:
            legacy_stmt.append(s)

    ledger_groups: dict[tuple[str, EntryType], list[LedgerEntry]] = defaultdict(list)
    for e in legacy_entries:
        ledger_groups[(e.payment_id, e.entry_type)].append(e)

    stmt_groups: dict[tuple[str, EntryType], list[SettlementRecord]] = defaultdict(list)
    for s in legacy_stmt:
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
                    ledger_seq=le.seq,
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

    # -- 3. repeated settlement reference in the statement feed -------
    for ref, lines in sorted(stmt_by_ref.items()):
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

    matched.sort(key=_sort_key)
    discrepancies.sort(key=_sort_key)
    return matched, discrepancies


class Reconciler:
    def __init__(self, repository: LedgerRepository) -> None:
        self.repo = repository

    # -- public API ----------------------------------------------------

    def reconcile(
        self,
        statement: list[SettlementRecord],
        scope: ReconcileScope | None = None,
        persist: bool = True,
    ) -> ReconciliationReport:
        """Reconcile the statement against committed ledger entries.

        Ledger data is only ever read. When ``persist`` is true the
        conclusion is appended to the reconciliation run journal and
        diffed against the previous persisted run.
        """
        scope = scope if scope is not None else ReconcileScope()

        # Strictly read-only access; opened and closed within this call.
        conn = self.repo.read_connection()
        try:
            all_entries = self.repo.list_entries(conn)
        finally:
            conn.close()

        money_entries = [e for e in all_entries if e.entry_type in _MONEY_TYPES]
        scoped = [e for e in money_entries if _in_scope(e, scope)]

        matched, discrepancies = _match(scoped, statement)
        report = ReconciliationReport(
            matched=matched, discrepancies=discrepancies, scope=scope
        )

        if persist:
            self._persist_run(report, statement, scope, money_entries)
        return report

    def reconcile_incremental(
        self, statement: list[SettlementRecord]
    ) -> ReconciliationReport:
        """Reconcile only entries recorded after the previous run."""
        conn = self.repo.read_connection()
        try:
            last = self.repo.get_latest_run(conn)
        finally:
            conn.close()
        since = last["max_seq"] if last is not None else None
        return self.reconcile(statement, scope=ReconcileScope(since_seq=since))

    def list_runs(self) -> list[dict]:
        """All persisted runs, oldest first (without report payloads)."""
        conn = self.repo.read_connection()
        try:
            runs = self.repo.list_runs(conn)
        finally:
            conn.close()
        return [
            {
                "run_id": r["run_id"],
                "created_at": r["created_at"],
                "scope": json.loads(r["scope_json"]),
                "statement_hash": r["statement_hash"],
                "max_seq": r["max_seq"],
            }
            for r in runs
        ]

    def get_run(self, run_id: int) -> dict | None:
        """One persisted run with its full conclusion, or None."""
        conn = self.repo.read_connection()
        try:
            row = self.repo.get_run(conn, run_id)
        finally:
            conn.close()
        if row is None:
            return None
        report = json.loads(row["report_json"])
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "scope": json.loads(row["scope_json"]),
            "statement_hash": row["statement_hash"],
            "max_seq": row["max_seq"],
            "matched": [_item_from_dict(d) for d in report["matched"]],
            "discrepancies": [_item_from_dict(d) for d in report["discrepancies"]],
            "new_discrepancies": [_item_from_dict(d) for d in report["new_discrepancies"]],
            "ongoing_discrepancies": [
                _item_from_dict(d) for d in report["ongoing_discrepancies"]
            ],
            "resolved_discrepancies": [
                _item_from_dict(d) for d in report["resolved_discrepancies"]
            ],
        }

    # -- journaling ------------------------------------------------------

    def _persist_run(
        self,
        report: ReconciliationReport,
        statement: list[SettlementRecord],
        scope: ReconcileScope,
        money_entries: list[LedgerEntry],
    ) -> None:
        scoped_seqs = [e.seq for e in money_entries if _in_scope(e, scope)]
        # High-water mark chains incremental runs even when a run sees
        # no new entries at all.
        max_seq = max(scoped_seqs, default=scope.since_seq or 0)

        with self.repo.transaction() as conn:
            prev = self.repo.get_latest_run(conn)
            prev_items: list[ReconciliationItem] = []
            if prev is not None:
                prev_report = json.loads(prev["report_json"])
                prev_items = [
                    _item_from_dict(d) for d in prev_report["discrepancies"]
                ]

            cur_by_key = {_item_key(d): d for d in report.discrepancies}
            prev_by_key = {_item_key(d): d for d in prev_items}
            report.new_discrepancies = [
                cur_by_key[k] for k in sorted(cur_by_key.keys() - prev_by_key.keys())
            ]
            report.ongoing_discrepancies = [
                cur_by_key[k] for k in sorted(cur_by_key.keys() & prev_by_key.keys())
            ]
            report.resolved_discrepancies = [
                prev_by_key[k] for k in sorted(prev_by_key.keys() - cur_by_key.keys())
            ]

            report_json = json.dumps(
                {
                    "matched": [_item_to_dict(m) for m in report.matched],
                    "discrepancies": [_item_to_dict(d) for d in report.discrepancies],
                    "new_discrepancies": [
                        _item_to_dict(d) for d in report.new_discrepancies
                    ],
                    "ongoing_discrepancies": [
                        _item_to_dict(d) for d in report.ongoing_discrepancies
                    ],
                    "resolved_discrepancies": [
                        _item_to_dict(d) for d in report.resolved_discrepancies
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            scope_json = json.dumps(
                {
                    "since_seq": scope.since_seq,
                    "until_seq": scope.until_seq,
                    "since_time": scope.since_time,
                    "until_time": scope.until_time,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            report.run_id = self.repo.insert_reconciliation_run(
                conn,
                created_at=utc_now_iso(),
                scope_json=scope_json,
                statement_hash=_statement_hash(statement),
                report_json=report_json,
                max_seq=max_seq,
                discrepancy_keys=json.dumps(sorted(cur_by_key.keys())),
            )
