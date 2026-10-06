"""Incremental reconciliation checkpoint tests.

Regression coverage for the "empty incremental window" bug: a scheduled
incremental run whose window contains no new fund movements must not
reset the "last covered seq" checkpoint. Previously such a run recorded
``max_seq=0``, so the next incremental run re-paired every already
covered entry and re-reported old discrepancies, and its conclusion no
longer matched a full reconciliation restricted to the same range.

Also covers repeated empty runs, checkpoint survival across a restart,
and concurrent incremental runs that must never overwrite the
checkpoint with an earlier position or touch fund-movement tables.
"""
from __future__ import annotations

import logging
import unittest
from concurrent.futures import ThreadPoolExecutor

from payment_ledger.models import (
    AuthorizeRequest,
    CaptureRequest,
    EntryType,
    RefundRequest,
)
from payment_ledger.reconciliation import Reconciler
from payment_ledger.reconciliation_types import (
    DiscrepancyType,
    ReconciliationScope,
    SettlementRecord,
    item_signature,
)

from .support import LedgerTestCase, log, reopen_service

log = logging.getLogger("payment_ledger.tests")


def signatures(items) -> list[tuple]:
    return sorted(item_signature(i) for i in items)


class IncrementalCheckpointTests(LedgerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.reconciler = Reconciler(self.service.repo)

    # -- helpers --------------------------------------------------------

    def _record(self, statement, scope=None, tag="run"):
        log.info(
            "RECONCILE-RECORD[%s] | scope=%s | statement=%s",
            tag, scope, [s.__dict__ for s in statement],
        )
        run = self.reconciler.reconcile_and_record(statement, scope)
        log.info(
            "RECONCILE-RECORD[%s] | run_id=%s resolved_scope=%s max_seq=%s",
            tag, run.run_id, run.scope, self._latest_max_seq(),
        )
        for m in run.matched:
            log.info(
                "RECONCILE[%s] | JUDGEMENT=ACCEPTED | kind=MATCHED payment=%s type=%s "
                "amount=%s ref=%s",
                tag, m.payment_id, m.entry_type.value, m.ledger_amount, m.external_ref,
            )
        for d in run.discrepancies:
            log.info(
                "RECONCILE[%s] | JUDGEMENT=REJECTED | discrepancy=%s payment=%s type=%s "
                "ledger=%s statement=%s ref=%s | reason=%s",
                tag, d.discrepancy.value, d.payment_id, d.entry_type.value,
                d.ledger_amount, d.statement_amount, d.external_ref, d.detail,
            )
        return run

    def _full_scoped(self, statement, scope, tag):
        """A full reconciliation restricted to the given concrete scope."""
        log.info(
            "RECONCILE-FULL[%s] | scope=%s | statement=%s",
            tag, scope, [s.__dict__ for s in statement],
        )
        report = self.reconciler.reconcile(statement, scope)
        log.info(
            "RECONCILE-FULL[%s] | matched=%d discrepancies=%d",
            tag, len(report.matched), len(report.discrepancies),
        )
        return report

    def _latest_max_seq(self) -> int:
        with self.service.repo.transaction() as conn:
            value = self.service.repo.latest_run_max_seq(conn)
        return 0 if value is None else value

    def _assert_items_equal(self, run_items, full_items, label) -> None:
        self.assertEqual(
            signatures(run_items), signatures(full_items),
            f"{label} differs from full reconciliation restricted to the same range",
        )

    def _seed_day_one(self) -> int:
        """p1 authorize + capture 4000 (S1). Returns the S1 entry seq."""
        svc = self.service
        log.info(
            "STEP=seed-day1 | idempotency_key=a1 | request=%s",
            {"payment_id": "p1", "amount": 10000, "currency": "USD"},
        )
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "a1")
        log.info(
            "STEP=seed-day1 | idempotency_key=c1 | request=%s",
            {"payment_id": "p1", "amount": 4000, "external_ref": "S1"},
        )
        svc.capture(CaptureRequest("p1", 4000, "USD", external_ref="S1"), "c1")
        return self.service.get_payment("p1").entries[-1].seq

    # -- the regression: empty window then new entries ------------------

    def test_empty_incremental_window_then_new_entries_matches_full_scoped(self):
        seq_s1 = self._seed_day_one()

        # Day 1: record a run covering S1; its statement matches it.
        day1_stmt = [SettlementRecord("S1", "p1", EntryType.CAPTURE, 4000, "USD")]
        run1 = self._record(day1_stmt, ReconciliationScope(to_seq=seq_s1), tag="day1")
        self.assertEqual(self._latest_max_seq(), seq_s1)
        # Day 1 conclusion equals a full run restricted to the same range.
        full1 = self._full_scoped(day1_stmt, run1.scope, "day1")
        self._assert_items_equal(run1.matched, full1.matched, "day1 matched")
        self._assert_items_equal(run1.discrepancies, full1.discrepancies, "day1 discrepancies")

        # Business day with no new fund movement: the scheduled
        # incremental run has an empty window and an empty statement.
        empty = self._record([], ReconciliationScope(incremental=True), tag="empty-day")
        self.assertEqual(empty.matched, [])
        self.assertEqual(empty.discrepancies, [])
        # The checkpoint must survive the empty run, not reset to 0.
        self.assertEqual(empty.scope.from_seq, seq_s1)
        self.assertEqual(self._latest_max_seq(), seq_s1)
        # ... and its conclusion equals a full run over the same (empty) range.
        full_empty = self._full_scoped([], empty.scope, "empty-day")
        self._assert_items_equal(empty.matched, full_empty.matched, "empty matched")
        self._assert_items_equal(
            empty.discrepancies, full_empty.discrepancies, "empty discrepancies"
        )

        # New fund movement arrives afterwards; continue incrementally.
        log.info(
            "STEP=seed-day3 | idempotency_key=c2 | request=%s",
            {"payment_id": "p1", "amount": 3000, "external_ref": None},
        )
        self.service.capture(CaptureRequest("p1", 3000, "USD"), "c2")
        seq_s2 = self.service.get_payment("p1").entries[-1].seq
        day3_stmt = [SettlementRecord("L1", "p1", EntryType.CAPTURE, 3000, "USD")]
        run3 = self._record(day3_stmt, ReconciliationScope(incremental=True), tag="day3")

        # Only entries after the day-1 checkpoint are in scope.
        self.assertEqual(run3.scope.from_seq, seq_s1)
        self.assertEqual(self._latest_max_seq(), seq_s2)
        refs = {m.external_ref for m in run3.matched}
        self.assertEqual(refs, {"L1"})
        # The already-covered S1 must not be re-paired or re-reported.
        all_refs = {i.external_ref for i in run3.matched + run3.discrepancies}
        self.assertNotIn("S1", all_refs)

        # Item-by-item equivalence with a full run restricted to exactly
        # the same seq range.
        full3 = self._full_scoped(day3_stmt, run3.scope, "day3")
        self._assert_items_equal(run3.matched, full3.matched, "day3 matched")
        self._assert_items_equal(run3.discrepancies, full3.discrepancies, "day3 discrepancies")

    def test_empty_window_with_statement_lines_still_matches_full_scoped(self):
        # Even when the empty-window run is handed statement lines, both
        # directions must stay consistent with the scoped full run:
        # ledger-less statement lines are MISSING_IN_LEDGER on both paths.
        seq_s1 = self._seed_day_one()
        self._record(
            [SettlementRecord("S1", "p1", EntryType.CAPTURE, 4000, "USD")],
            ReconciliationScope(to_seq=seq_s1),
            tag="day1",
        )
        stmt = [SettlementRecord("S9", "p9", EntryType.CAPTURE, 700, "USD")]
        empty = self._record(stmt, ReconciliationScope(incremental=True), tag="empty-with-lines")
        self.assertEqual(self._latest_max_seq(), seq_s1)
        full = self._full_scoped(stmt, empty.scope, "empty-with-lines")
        self._assert_items_equal(empty.matched, full.matched, "empty matched")
        self._assert_items_equal(
            empty.discrepancies, full.discrepancies, "empty discrepancies"
        )
        self.assertEqual(
            [d.discrepancy for d in empty.discrepancies],
            [DiscrepancyType.MISSING_IN_LEDGER],
        )

    def test_repeated_empty_runs_keep_checkpoint_stable(self):
        seq_s1 = self._seed_day_one()
        self._record(
            [SettlementRecord("S1", "p1", EntryType.CAPTURE, 4000, "USD")],
            ReconciliationScope(to_seq=seq_s1),
            tag="day1",
        )
        for i in range(3):
            empty = self._record([], ReconciliationScope(incremental=True), tag=f"empty-{i}")
            self.assertEqual(empty.scope.from_seq, seq_s1)
            self.assertEqual(self._latest_max_seq(), seq_s1)

        # New entries after several empty runs: still only the new part.
        self.service.capture(CaptureRequest("p1", 2000, "USD", external_ref="S3"), "c3")
        seq_s3 = self.service.get_payment("p1").entries[-1].seq
        run = self._record(
            [SettlementRecord("S3", "p1", EntryType.CAPTURE, 2000, "USD")],
            ReconciliationScope(incremental=True),
            tag="after-empties",
        )
        self.assertEqual(run.scope.from_seq, seq_s1)
        self.assertEqual(self._latest_max_seq(), seq_s3)
        self.assertEqual({m.external_ref for m in run.matched}, {"S3"})
        full = self._full_scoped(
            [SettlementRecord("S3", "p1", EntryType.CAPTURE, 2000, "USD")],
            run.scope, "after-empties",
        )
        self._assert_items_equal(run.matched, full.matched, "after-empties matched")
        self._assert_items_equal(
            run.discrepancies, full.discrepancies, "after-empties discrepancies"
        )

    def test_checkpoint_survives_restart_with_empty_run_in_between(self):
        seq_s1 = self._seed_day_one()
        self._record(
            [SettlementRecord("S1", "p1", EntryType.CAPTURE, 4000, "USD")],
            ReconciliationScope(to_seq=seq_s1),
            tag="day1",
        )
        self._record([], ReconciliationScope(incremental=True), tag="empty-before-restart")

        # Restart: reopen the same database file with a new repository.
        restarted_svc = reopen_service(self.db_path)
        restarted = Reconciler(restarted_svc.repo)
        log.info(
            "STEP=restart | idempotency_key=n/a | request=%s",
            {"db": str(self.db_path)},
        )

        # A new fund movement lands through the restarted service...
        log.info(
            "STEP=seed-after-restart | idempotency_key=c2 | request=%s",
            {"payment_id": "p1", "amount": 3000, "external_ref": "S2"},
        )
        restarted_svc.capture(CaptureRequest("p1", 3000, "USD", external_ref="S2"), "c2")

        log.info(
            "RECONCILE-RECORD[after-restart] | scope=%s | statement=%s",
            ReconciliationScope(incremental=True),
            [{"external_ref": "S2", "amount": 3000}],
        )
        run = restarted.reconcile_and_record(
            [SettlementRecord("S2", "p1", EntryType.CAPTURE, 3000, "USD")],
            ReconciliationScope(incremental=True),
        )
        log.info(
            "RECONCILE[after-restart] | run_id=%s resolved_scope=%s matched_refs=%s "
            "discrepancy_refs=%s",
            run.run_id, run.scope,
            [m.external_ref for m in run.matched],
            [(d.discrepancy.value, d.external_ref) for d in run.discrepancies],
        )
        # Checkpoint comes from persisted runs, despite the empty run + restart.
        self.assertEqual(run.scope.from_seq, seq_s1)
        self.assertEqual({m.external_ref for m in run.matched}, {"S2"})
        self.assertEqual(
            [d.external_ref for d in run.discrepancies], [],
            "old S1 must not be re-reported after restart",
        )
        full = restarted.reconcile(
            [SettlementRecord("S2", "p1", EntryType.CAPTURE, 3000, "USD")],
            run.scope,
        )
        self.assertEqual(signatures(run.matched), signatures(full.matched))
        self.assertEqual(signatures(run.discrepancies), signatures(full.discrepancies))

    def test_concurrent_incremental_runs_never_regress_checkpoint_or_touch_ledger(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "a1")
        svc.capture(CaptureRequest("p1", 4000, "USD", external_ref="S1"), "c1")
        self._record(
            [SettlementRecord("S1", "p1", EntryType.CAPTURE, 4000, "USD")],
            tag="baseline",
        )
        baseline_seq = self._latest_max_seq()

        # New entries are committed BEFORE the concurrent incrementals
        # start: exactly one run may cover them and advance the
        # checkpoint; every other run sees an empty window and must keep
        # the checkpoint instead of regressing it.
        svc.capture(CaptureRequest("p1", 3000, "USD", external_ref="S2"), "c2")
        svc.refund(RefundRequest("p1", 1000, "USD", external_ref="R1"), "r1")
        seq_after = svc.get_payment("p1").entries[-1].seq

        before_entries = {
            (e.seq, e.entry_type.value, e.amount, e.external_ref)
            for e in svc.get_payment("p1").entries
        }
        before_total = (
            svc.get_payment("p1").captured_amount,
            svc.get_payment("p1").refunded_amount,
        )

        new_statement = [
            SettlementRecord("S2", "p1", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("R1", "p1", EntryType.REFUND, 1000, "USD"),
        ]
        threads = 8
        log.info(
            "CONCURRENT-INCREMENTAL threads=%d | idempotency_key=n/a | request=%s",
            threads,
            {"incremental": True, "statement": [s.__dict__ for s in new_statement]},
        )
        with ThreadPoolExecutor(max_workers=threads) as pool:
            runs = list(
                pool.map(
                    lambda _: self.reconciler.reconcile_and_record(
                        new_statement, ReconciliationScope(incremental=True)
                    ),
                    range(threads),
                )
            )

        log.info(
            "CONCURRENT-INCREMENTAL from_seqs=%s", [r.scope.from_seq for r in runs]
        )
        # Exactly one run can resolve the old checkpoint and cover the new
        # window; every other run is serialized behind it, resolves the
        # already-advanced checkpoint and sees an empty window.
        covering = [r for r in runs if r.scope.from_seq == baseline_seq]
        starved = [r for r in runs if r.scope.from_seq != baseline_seq]
        self.assertEqual(len(covering), 1)
        self.assertEqual(len(starved), threads - 1)

        # Each run's conclusion equals a full reconciliation restricted
        # to that run's own resolved range (item by item, both directions).
        winner = covering[0]
        full_scoped = self._full_scoped(new_statement, winner.scope, "concurrent-winner")
        self._assert_items_equal(winner.matched, full_scoped.matched, "winner matched")
        self._assert_items_equal(
            winner.discrepancies, full_scoped.discrepancies, "winner discrepancies"
        )
        self.assertEqual({m.external_ref for m in winner.matched}, {"S2", "R1"})
        for i, r in enumerate(starved):
            full_starved = self._full_scoped(
                new_statement, r.scope, f"concurrent-starved-{i}"
            )
            self._assert_items_equal(r.matched, full_starved.matched, f"starved-{i} matched")
            self._assert_items_equal(
                r.discrepancies, full_starved.discrepancies, f"starved-{i} discrepancies"
            )

        # No run re-reports the already-covered S1, every run starts at or
        # after the baseline checkpoint, and the final checkpoint advanced
        # exactly to the newest entry (never reset towards 0).
        for r in runs:
            self.assertGreaterEqual(r.scope.from_seq or 0, baseline_seq)
            refs = {i.external_ref for i in r.matched + r.discrepancies}
            self.assertNotIn("S1", refs)
        self.assertEqual(self._latest_max_seq(), seq_after)

        # Fund-movement tables untouched by reconciliation.
        after_entries = {
            (e.seq, e.entry_type.value, e.amount, e.external_ref)
            for e in svc.get_payment("p1").entries
        }
        self.assertEqual(before_entries, after_entries)
        self.assertEqual(
            (
                svc.get_payment("p1").captured_amount,
                svc.get_payment("p1").refunded_amount,
            ),
            before_total,
        )
        log.info(
            "CONCURRENT-INCREMENTAL verified: %d runs, checkpoint=%s, ledger unchanged",
            threads, seq_after,
        )


if __name__ == "__main__":
    unittest.main()
