"""Incremental reconciliation: high-water mark must survive empty windows.

Regression scenario: an incremental run whose window contains no new
ledger entries (e.g. a scheduled run on a business day with no fund
movements) must not reset the "reconciled up to" position. Otherwise
the next incremental run reopens every already-reconciled entry and
reports stale discrepancies that do not agree with a same-scope full
run.
"""
from __future__ import annotations

import logging
import unittest
from concurrent.futures import ThreadPoolExecutor

from payment_ledger.models import (
    AuthorizeRequest,
    CaptureRequest,
    EntryType,
)
from payment_ledger.reconciliation import Reconciler
from payment_ledger.reconciliation_types import (
    DiscrepancyType,
    ReconciliationScope,
    SettlementRecord,
    item_signature,
)

from .support import LedgerTestCase, log, log_request, reopen_service

log = logging.getLogger("payment_ledger.tests")


def signatures(items) -> list[tuple]:
    return sorted(item_signature(i) for i in items)


class EmptyWindowIncrementalTests(LedgerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.reconciler = Reconciler(self.service.repo)

    def _seed_two_captures_with_one_mismatch(self) -> None:
        svc = self.service
        log_request("seed-authorize", "a1", {"payment_id": "p1", "amount": 10000, "currency": "USD"})
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "a1")
        log_request("seed-capture", "c1", {"payment_id": "p1", "amount": 4000, "currency": "USD", "external_ref": "S1"})
        svc.capture(CaptureRequest("p1", 4000, "USD", external_ref="S1"), "c1")
        log_request("seed-authorize", "a2", {"payment_id": "p2", "amount": 5000, "currency": "USD"})
        svc.authorize(AuthorizeRequest("p2", 5000, "USD"), "a2")
        log_request("seed-capture", "c2", {"payment_id": "p2", "amount": 5000, "currency": "USD", "external_ref": "S3"})
        svc.capture(CaptureRequest("p2", 5000, "USD", external_ref="S3"), "c2")

    def _base_statement(self) -> list[SettlementRecord]:
        return [
            SettlementRecord("S1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("S3", "p2", EntryType.CAPTURE, 4999, "USD"),  # mismatch
        ]

    def _log_run(self, tag, run) -> None:
        log.info(
            "INCREMENTAL[%s] run_id=%s scope=%s",
            tag, run.run_id, run.scope,
        )
        for d in run.discrepancies:
            log.info(
                "INCREMENTAL[%s] judgement=REJECTED discrepancy=%s payment=%s ref=%s reason=%s",
                tag, d.discrepancy.value, d.payment_id, d.external_ref, d.detail,
            )
        for m in run.matched:
            log.info(
                "INCREMENTAL[%s] judgement=MATCHED payment=%s ref=%s amount=%s",
                tag, m.payment_id, m.external_ref, m.ledger_amount,
            )

    def test_empty_incremental_then_new_entries_matches_same_scope_full(self):
        self._seed_two_captures_with_one_mismatch()
        seq_s3 = self.service.get_payment("p2").entries[1].seq  # newest money entry

        # Run 1: ordinary recorded run covering everything so far.
        run1 = self.reconciler.reconcile_and_record(self._base_statement())
        self._log_run("baseline", run1)
        self.assertEqual(
            [d.external_ref for d in run1.discrepancies if d.discrepancy == DiscrepancyType.AMOUNT_MISMATCH],
            ["S3"],
        )

        # Empty business day: scheduled incremental run with no new entries.
        empty_run = self.reconciler.reconcile_and_record(
            [], ReconciliationScope(incremental=True)
        )
        self._log_run("empty", empty_run)
        # The empty conclusion must equal a full run restricted to the
        # same (empty) window.
        empty_full = self.reconciler.reconcile(
            [], ReconciliationScope(from_seq=seq_s3)
        )
        self.assertEqual(signatures(empty_run.matched), signatures(empty_full.matched))
        self.assertEqual(
            signatures(empty_run.discrepancies), signatures(empty_full.discrepancies)
        )
        # The watermark must not move backwards.
        self.assertEqual(empty_run.scope.from_seq, seq_s3)

        # New fund movement after the empty run.
        log_request("new-entry-after-empty", "c3", {"payment_id": "p1", "amount": 3000, "currency": "USD", "external_ref": "S4"})
        self.service.capture(
            CaptureRequest("p1", 3000, "USD", external_ref="S4"), "c3"
        )
        seq_s4 = self.service.get_payment("p1").entries[2].seq

        # Next incremental run covers only entries strictly after the
        # previous watermark; old entries must not be re-paired.
        inc_statement = [
            SettlementRecord("S4", "p1", EntryType.CAPTURE, 3000, "USD"),
        ]
        run_inc = self.reconciler.reconcile_and_record(
            inc_statement, ReconciliationScope(incremental=True)
        )
        self._log_run("after-empty", run_inc)
        self.assertEqual(run_inc.scope.from_seq, seq_s3)
        self.assertEqual([m.external_ref for m in run_inc.matched], ["S4"])
        # No stale discrepancies for already-reconciled S1/S3 resurface.
        self.assertEqual(run_inc.discrepancies, [])

        # Item-by-item identical to a full run restricted to same window.
        scoped_full = self.reconciler.reconcile(
            inc_statement, ReconciliationScope(from_seq=seq_s3)
        )
        self.assertEqual(
            signatures(run_inc.matched), signatures(scoped_full.matched)
        )
        self.assertEqual(
            signatures(run_inc.discrepancies),
            signatures(scoped_full.discrepancies),
        )
        self.assertEqual(seq_s4, seq_s3 + 1)

    def test_repeated_empty_runs_keep_watermark_then_increment_advances(self):
        self._seed_two_captures_with_one_mismatch()
        seq_s3 = self.service.get_payment("p2").entries[1].seq
        self.reconciler.reconcile_and_record(self._base_statement())

        # The scheduled job keeps firing on days with no fund movements:
        # every empty run must leave the high-water mark exactly where it
        # was and conclude nothing.
        for day in range(3):
            empty = self.reconciler.reconcile_and_record(
                [], ReconciliationScope(incremental=True)
            )
            self.assertEqual(empty.scope.from_seq, seq_s3)
            self.assertEqual(empty.matched, [])
            self.assertEqual(empty.discrepancies, [])
            log.info(
                "EMPTY-RUN day=%s run_id=%s watermark_preserved_at=%s",
                day, empty.run_id, seq_s3,
            )

        # New entries after several empty days: only the new window is
        # reconciled, old entries are never revisited.
        log_request("new-entry-after-repeated-empty", "c3", {"payment_id": "p1", "amount": 3000, "currency": "USD", "external_ref": "S4"})
        self.service.capture(
            CaptureRequest("p1", 3000, "USD", external_ref="S4"), "c3"
        )
        inc = self.reconciler.reconcile_and_record(
            [SettlementRecord("S4", "p1", EntryType.CAPTURE, 3000, "USD")],
            ReconciliationScope(incremental=True),
        )
        self.assertEqual(inc.scope.from_seq, seq_s3)
        self.assertEqual([m.external_ref for m in inc.matched], ["S4"])
        self.assertEqual(inc.discrepancies, [])

        # And yet another empty run after the advanced run keeps the new
        # position (behaviour must hold repeatedly in both directions).
        another_empty = self.reconciler.reconcile_and_record(
            [], ReconciliationScope(incremental=True)
        )
        self.assertEqual(another_empty.scope.from_seq, seq_s3 + 1)
        self.assertEqual(another_empty.matched, [])
        self.assertEqual(another_empty.discrepancies, [])

    def test_concurrent_empty_incremental_runs_never_regress_watermark(self):
        self._seed_two_captures_with_one_mismatch()
        seq_s3 = self.service.get_payment("p2").entries[1].seq
        baseline = self.reconciler.reconcile_and_record(self._base_statement())
        log.info(
            "CONCURRENT-INCREMENTAL baseline run_id=%s watermark=%s",
            baseline.run_id, seq_s3,
        )

        before_entries = {
            (e.seq, e.entry_type.value, e.amount, e.external_ref)
            for pid in ("p1", "p2")
            for e in self.service.get_payment(pid).entries
        }
        before_totals = {
            pid: (
                self.service.get_payment(pid).captured_amount,
                self.service.get_payment(pid).refunded_amount,
            )
            for pid in ("p1", "p2")
        }

        # Many empty incremental runs fire at once (e.g. scheduled jobs
        # from several workers): none may push the position back.
        threads = 8
        with ThreadPoolExecutor(max_workers=threads) as pool:
            runs = list(
                pool.map(
                    lambda _: self.reconciler.reconcile_and_record(
                        [], ReconciliationScope(incremental=True)
                    ),
                    range(threads),
                )
            )

        self.assertEqual(len({r.run_id for r in runs}), threads)
        for r in runs:
            self.assertEqual(r.scope.from_seq, seq_s3)
            self.assertEqual(r.matched, [])
            self.assertEqual(r.discrepancies, [])

        # The persisted high-water mark is unchanged and no run stored an
        # earlier seq; the next incremental starts after the baseline.
        with self.reconciler.repo.transaction() as conn:
            watermark = self.reconciler.repo.latest_run_max_seq(conn)
        self.assertEqual(watermark, seq_s3)
        persisted = self.reconciler.list_runs()
        self.assertEqual(len(persisted), threads + 1)
        self.assertTrue(all(row.scope.from_seq == seq_s3 for row in persisted[1:]))

        # Fund movements are byte-for-byte untouched.
        after_entries = {
            (e.seq, e.entry_type.value, e.amount, e.external_ref)
            for pid in ("p1", "p2")
            for e in self.service.get_payment(pid).entries
        }
        self.assertEqual(before_entries, after_entries)
        for pid in ("p1", "p2"):
            self.assertEqual(
                (
                    self.service.get_payment(pid).captured_amount,
                    self.service.get_payment(pid).refunded_amount,
                ),
                before_totals[pid],
            )
        log.info(
            "CONCURRENT-INCREMENTAL verified: %d empty runs, watermark=%s, ledger unchanged",
            threads, watermark,
        )

    def test_concurrent_incremental_runs_with_new_entries_never_regress(self):
        self._seed_two_captures_with_one_mismatch()
        seq_s3 = self.service.get_payment("p2").entries[1].seq
        self.reconciler.reconcile_and_record(self._base_statement())

        # One new entry appears, then many incremental runs fire at once.
        # Every run must resolve its window starting at the committed
        # baseline watermark -- none may start earlier (from_seq=0) -- and
        # the persisted watermark after all runs is the new entry's seq.
        log_request("new-entry-before-concurrent", "c3", {"payment_id": "p1", "amount": 3000, "currency": "USD", "external_ref": "S4"})
        self.service.capture(
            CaptureRequest("p1", 3000, "USD", external_ref="S4"), "c3"
        )
        seq_s4 = self.service.get_payment("p1").entries[2].seq
        inc_statement = [
            SettlementRecord("S4", "p1", EntryType.CAPTURE, 3000, "USD"),
        ]

        threads = 8
        log.info(
            "CONCURRENT-INCREMENTAL-NEW threads=%d watermark_before=%s", threads, seq_s3
        )
        before_entries = {
            (e.seq, e.entry_type.value, e.amount, e.external_ref)
            for pid in ("p1", "p2")
            for e in self.service.get_payment(pid).entries
        }
        with ThreadPoolExecutor(max_workers=threads) as pool:
            runs = list(
                pool.map(
                    lambda _: self.reconciler.reconcile_and_record(
                        inc_statement, ReconciliationScope(incremental=True)
                    ),
                    range(threads),
                )
            )

        # Each run resolves against the watermark committed at the
        # instant its short write transaction runs: starts are therefore
        # either the baseline (seq_s3, matching S4) or the advanced mark
        # (seq_s4, an empty window for later callers). No start can be
        # earlier than the baseline (it must never be 0), and every
        # conclusion equals a same-scope full run.
        starts = [r.scope.from_seq for r in runs]
        self.assertTrue(
            all(s in (seq_s3, seq_s4) for s in starts),
            f"unexpected incremental starts: {starts}",
        )
        self.assertIn(seq_s3, starts)  # at least one run covered S4
        # Concurrent runs are allowed to cover the same window and append
        # equivalent rows (they never block each other's read-only
        # matching), so S4 may be matched by more than one run. The real
        # regression -- an old entry re-paired/re-reported -- must never
        # happen: no run mentions the old refs S1/S3 at all.
        self.assertTrue(
            any(
                m.external_ref == "S4"
                for r in runs
                for m in r.matched
            )
        )
        stale = [
            d
            for r in runs
            for d in r.discrepancies
            if d.external_ref in ("S1", "S3")
        ]
        self.assertEqual(stale, [])
        for r in runs:
            # Each conclusion must equal, item by item, a full run
            # restricted to exactly the scope this run resolved.
            scoped_full = self.reconciler.reconcile(
                inc_statement, ReconciliationScope(from_seq=r.scope.from_seq)
            )
            self.assertEqual(
                signatures(r.matched), signatures(scoped_full.matched)
            )
            self.assertEqual(
                signatures(r.discrepancies),
                signatures(scoped_full.discrepancies),
            )
        with self.reconciler.repo.transaction() as conn:
            self.assertEqual(
                self.reconciler.repo.latest_run_max_seq(conn), seq_s4
            )
        # Persisted runs: the first is the baseline full run, and every
        # concurrent row resolved a start no earlier than the committed
        # baseline watermark. Rows need not be ordered by start (a run
        # resolving the advanced mark may commit later), but no row may
        # ever move the position backwards.
        persisted = self.reconciler.list_runs()
        self.assertEqual(persisted[0].scope.from_seq, None)
        self.assertTrue(
            all(row.scope.from_seq >= seq_s3 for row in persisted[1:])
        )
        # Fund movements are untouched by the concurrent reconciliation.
        after_entries = {
            (e.seq, e.entry_type.value, e.amount, e.external_ref)
            for pid in ("p1", "p2")
            for e in self.service.get_payment(pid).entries
        }
        self.assertEqual(before_entries, after_entries)
        log.info(
            "CONCURRENT-INCREMENTAL-NEW verified: starts=%s, watermark=%s",
            starts, seq_s4,
        )

    def test_watermark_survives_restart_after_empty_run(self):
        self._seed_two_captures_with_one_mismatch()
        seq_s3 = self.service.get_payment("p2").entries[1].seq
        self.reconciler.reconcile_and_record(self._base_statement())
        empty = self.reconciler.reconcile_and_record(
            [], ReconciliationScope(incremental=True)
        )
        self.assertEqual(empty.scope.from_seq, seq_s3)

        # Simulate a process restart against the same database file.
        restarted_svc = reopen_service(self.db_path)
        restarted = Reconciler(restarted_svc.repo)
        with restarted.repo.transaction() as conn:
            watermark = restarted.repo.latest_run_max_seq(conn)
        self.assertEqual(watermark, seq_s3)
        log.info("RESTART-EMPTY watermark reloaded=%s", watermark)

        # New entries after restart; replay the same inputs and assert
        # the conclusion is reproducible and covers only the new window.
        log_request("new-entry-after-restart", "c3", {"payment_id": "p1", "amount": 3000, "currency": "USD", "external_ref": "S4"})
        restarted_svc.capture(
            CaptureRequest("p1", 3000, "USD", external_ref="S4"), "c3"
        )
        inc_statement = [
            SettlementRecord("S4", "p1", EntryType.CAPTURE, 3000, "USD"),
        ]
        run = restarted.reconcile_and_record(
            inc_statement, ReconciliationScope(incremental=True)
        )
        self.assertEqual(run.scope.from_seq, seq_s3)
        self.assertEqual([m.external_ref for m in run.matched], ["S4"])
        self.assertEqual(run.discrepancies, [])

        reloaded = restarted.get_run(run.run_id)
        self.assertIsNotNone(reloaded)
        replay = Reconciler(reopen_service(self.db_path).repo).reconcile(
            inc_statement, ReconciliationScope(from_seq=seq_s3)
        )
        self.assertEqual(signatures(reloaded.matched), signatures(replay.matched))
        self.assertEqual(
            signatures(reloaded.discrepancies), signatures(replay.discrepancies)
        )
        log.info("RESTART-EMPTY replay identical to same-scope full conclusion")


if __name__ == "__main__":
    unittest.main()
