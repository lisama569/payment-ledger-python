"""Reconciliation v2: reference-based pairing, one-sided directions,
duplicate references, scoped/incremental runs, recorded-run diffs,
concurrency and restart reproducibility.
"""
from __future__ import annotations

import logging
import time
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

from .support import LedgerTestCase, log, log_request, reopen_service

log = logging.getLogger("payment_ledger.tests")


def seed_with_refs(svc) -> None:
    """p1: capture 4000 (S1) + legacy capture 3000 (no ref), refund 1000 (S2);
    p2: capture 5000 (S3)."""
    log_request("seed-authorize", "a1", {"payment_id": "p1", "amount": 10000, "currency": "USD"})
    svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "a1")
    log_request("seed-capture", "c1", {"payment_id": "p1", "amount": 4000, "currency": "USD", "external_ref": "S1"})
    svc.capture(CaptureRequest("p1", 4000, "USD", external_ref="S1"), "c1")
    log_request("seed-capture", "c2", {"payment_id": "p1", "amount": 3000, "currency": "USD", "external_ref": None})
    svc.capture(CaptureRequest("p1", 3000, "USD"), "c2")  # legacy entry, no ref
    log_request("seed-refund", "r1", {"payment_id": "p1", "amount": 1000, "currency": "USD", "external_ref": "S2"})
    svc.refund(RefundRequest("p1", 1000, "USD", external_ref="S2"), "r1")
    log_request("seed-authorize", "a2", {"payment_id": "p2", "amount": 5000, "currency": "USD"})
    svc.authorize(AuthorizeRequest("p2", 5000, "USD"), "a2")
    log_request("seed-capture", "c3", {"payment_id": "p2", "amount": 5000, "currency": "USD", "external_ref": "S3"})
    svc.capture(CaptureRequest("p2", 5000, "USD", external_ref="S3"), "c3")


def full_statement() -> list[SettlementRecord]:
    return [
        SettlementRecord("S1", "p1", EntryType.CAPTURE, 4000, "USD"),
        SettlementRecord("L1", "p1", EntryType.CAPTURE, 3000, "USD"),  # pairs with legacy entry
        SettlementRecord("S2", "p1", EntryType.REFUND, 1000, "USD"),
        SettlementRecord("S3", "p2", EntryType.CAPTURE, 5000, "USD"),
    ]


def signatures(items) -> list[tuple]:
    return sorted(item_signature(i) for i in items)


class ReconciliationV2Tests(LedgerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.reconciler = Reconciler(self.service.repo)

    def _log_report(self, tag, report) -> None:
        for m in report.matched:
            log.info(
                "RECONCILE[%s] judgement=MATCHED payment=%s type=%s amount=%s ref=%s",
                tag, m.payment_id, m.entry_type.value, m.ledger_amount, m.external_ref,
            )
        for d in report.discrepancies:
            log.info(
                "RECONCILE[%s] judgement=REJECTED discrepancy=%s payment=%s type=%s "
                "ledger=%s statement=%s ref=%s reason=%s",
                tag, d.discrepancy.value, d.payment_id, d.entry_type.value,
                d.ledger_amount, d.statement_amount, d.external_ref, d.detail,
            )

    def _run(self, statement, scope=None, tag="run"):
        log.info("RECONCILE[%s] scope=%s statement=%s", tag, scope, [s.__dict__ for s in statement])
        report = self.reconciler.reconcile(statement, scope)
        self._log_report(tag, report)
        return report

    # -- reference-based pairing ---------------------------------------

    def test_mixed_ref_and_legacy_entries_all_match(self):
        seed_with_refs(self.service)
        report = self._run(full_statement())
        self.assertEqual(len(report.matched), 4)
        self.assertEqual(report.discrepancies, [])
        refs = {m.external_ref for m in report.matched}
        self.assertEqual(refs, {"S1", "S2", "S3", "L1"})

    def test_ref_pair_amount_mismatch_identified_by_ref(self):
        seed_with_refs(self.service)
        statement = full_statement()
        statement[3] = SettlementRecord("S3", "p2", EntryType.CAPTURE, 4999, "USD")
        report = self._run(statement)
        mism = [d for d in report.discrepancies if d.discrepancy == DiscrepancyType.AMOUNT_MISMATCH]
        self.assertEqual(len(mism), 1)
        self.assertEqual(mism[0].external_ref, "S3")
        self.assertEqual((mism[0].ledger_amount, mism[0].statement_amount), (5000, 4999))

    def test_one_sided_directions_are_distinguishable(self):
        seed_with_refs(self.service)
        # Statement drops S2 (ledger booked it, outside never settled)
        # and adds S9 (settled outside, never booked here).
        statement = [
            SettlementRecord("S1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("L1", "p1", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("S3", "p2", EntryType.CAPTURE, 5000, "USD"),
            SettlementRecord("S9", "p9", EntryType.CAPTURE, 700, "USD"),
        ]
        report = self._run(statement)
        missing_in_stmt = [
            d for d in report.discrepancies
            if d.discrepancy == DiscrepancyType.MISSING_IN_STATEMENT
        ]
        missing_in_ledger = [
            d for d in report.discrepancies
            if d.discrepancy == DiscrepancyType.MISSING_IN_LEDGER
        ]
        self.assertEqual(len(missing_in_stmt), 1)
        self.assertEqual(missing_in_stmt[0].external_ref, "S2")
        self.assertEqual(missing_in_stmt[0].ledger_amount, 1000)
        self.assertIsNone(missing_in_stmt[0].statement_amount)
        self.assertEqual(len(missing_in_ledger), 1)
        self.assertEqual(missing_in_ledger[0].external_ref, "S9")
        self.assertEqual(missing_in_ledger[0].statement_amount, 700)
        self.assertIsNone(missing_in_ledger[0].ledger_amount)

    def test_duplicate_external_ref_in_statement_is_identified(self):
        seed_with_refs(self.service)
        statement = full_statement() + [
            SettlementRecord("S1", "p1", EntryType.CAPTURE, 4000, "USD")
        ]
        report = self._run(statement)
        dupes = [
            d for d in report.discrepancies
            if d.discrepancy == DiscrepancyType.DUPLICATE_EXTERNAL_REF
        ]
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0].external_ref, "S1")
        # The first occurrence still pairs and matches.
        self.assertIn("S1", {m.external_ref for m in report.matched})

    # -- scopes: window / incremental vs full ---------------------------

    def _seed_ref_only(self):
        """All money entries carry refs: p1 S1(4000), S2(3000); p2 S3(5000, mismatched)."""
        svc = self.service
        log_request("seed-authorize", "a1", {"payment_id": "p1", "amount": 10000, "currency": "USD"})
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "a1")
        log_request("seed-capture", "c1", {"payment_id": "p1", "amount": 4000, "currency": "USD", "external_ref": "S1"})
        svc.capture(CaptureRequest("p1", 4000, "USD", external_ref="S1"), "c1")
        log_request("seed-capture", "c2", {"payment_id": "p1", "amount": 3000, "currency": "USD", "external_ref": "S2"})
        svc.capture(CaptureRequest("p1", 3000, "USD", external_ref="S2"), "c2")
        log_request("seed-authorize", "a2", {"payment_id": "p2", "amount": 5000, "currency": "USD"})
        svc.authorize(AuthorizeRequest("p2", 5000, "USD"), "a2")
        log_request("seed-capture", "c3", {"payment_id": "p2", "amount": 5000, "currency": "USD", "external_ref": "S3"})
        svc.capture(CaptureRequest("p2", 5000, "USD", external_ref="S3"), "c3")

    def _ref_statement(self):
        return [
            SettlementRecord("S1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("S2", "p1", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("S3", "p2", EntryType.CAPTURE, 4999, "USD"),  # mismatch
        ]

    def test_window_run_consistent_with_full_run(self):
        self._seed_ref_only()
        entries = self.service.get_payment("p1").entries
        seq_s1 = entries[1].seq  # first capture

        full = self._run(self._ref_statement(), tag="full")
        window = self._run(
            [self._ref_statement()[0]],
            ReconciliationScope(to_seq=seq_s1),
            tag="window",
        )
        # The window conclusion equals the full conclusion restricted to
        # the same entries (here: only S1 is in scope).
        full_in_window = [m for m in full.matched if m.external_ref == "S1"]
        self.assertEqual(signatures(window.matched), signatures(full_in_window))
        self.assertEqual(window.discrepancies, [])
        # And the out-of-window discrepancy does not leak into the window.
        self.assertEqual(
            [d.external_ref for d in full.discrepancies], ["S3"]
        )

    def test_incremental_run_consistent_with_full_run(self):
        self._seed_ref_only()
        entries = self.service.get_payment("p1").entries
        seq_s2 = entries[2].seq  # second capture of p1

        stmt = self._ref_statement()
        run1 = self.reconciler.reconcile_and_record(
            stmt[:2], ReconciliationScope(to_seq=seq_s2)
        )
        log.info("RECORDED run_id=%s scope=%s", run1.run_id, run1.scope)
        run2 = self.reconciler.reconcile_and_record(
            stmt[2:], ReconciliationScope(incremental=True)
        )
        log.info("RECORDED run_id=%s scope=%s", run2.run_id, run2.scope)
        self.assertEqual(run2.scope.from_seq, seq_s2)

        full = self._run(stmt, tag="full")
        # Incremental conclusions union to the full conclusion.
        self.assertEqual(
            signatures(run1.matched) + signatures(run2.matched),
            signatures(full.matched),
        )
        self.assertEqual(
            signatures(run1.discrepancies) + signatures(run2.discrepancies),
            signatures(full.discrepancies),
        )

    def test_time_window_run(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "a1")
        svc.capture(CaptureRequest("p1", 4000, "USD", external_ref="S1"), "c1")
        time.sleep(0.02)
        svc.capture(CaptureRequest("p1", 3000, "USD", external_ref="S2"), "c2")
        time.sleep(0.02)
        svc.capture(CaptureRequest("p1", 2000, "USD", external_ref="S3"), "c3")

        entries = svc.get_payment("p1").entries
        t2 = entries[2].created_at  # the S2 capture timestamp
        report = self._run(
            [SettlementRecord("S2", "p1", EntryType.CAPTURE, 3000, "USD")],
            ReconciliationScope(from_time=t2, to_time=t2),
            tag="time-window",
        )
        self.assertEqual([m.external_ref for m in report.matched], ["S2"])
        self.assertEqual(report.discrepancies, [])

    # -- recorded runs: diff, restart, concurrency ----------------------

    def test_consecutive_runs_diff_new_persisting_resolved(self):
        self._seed_ref_only()
        stmt1 = self._ref_statement() + [
            SettlementRecord("S9", "p9", EntryType.CAPTURE, 700, "USD"),
        ]
        run1 = self.reconciler.reconcile_and_record(stmt1)
        self._log_report("run1", run1)

        # Second run: S3 fixed (resolved), S9 still unsettled (persisting),
        # S1 now mismatches (new).
        stmt2 = [
            SettlementRecord("S1", "p1", EntryType.CAPTURE, 4001, "USD"),
            SettlementRecord("S2", "p1", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("S3", "p2", EntryType.CAPTURE, 5000, "USD"),
            SettlementRecord("S9", "p9", EntryType.CAPTURE, 700, "USD"),
        ]
        run2 = self.reconciler.reconcile_and_record(stmt2)
        self._log_report("run2", run2)

        diff = self.reconciler.diff_runs(run1.run_id, run2.run_id)
        log.info(
            "DIFF older=%s newer=%s new=%s persisting=%s resolved=%s",
            diff.older_run_id, diff.newer_run_id,
            [item_signature(d) for d in diff.new_discrepancies],
            [item_signature(d) for d in diff.persisting_discrepancies],
            [item_signature(d) for d in diff.resolved_discrepancies],
        )
        self.assertEqual(
            [d.external_ref for d in diff.new_discrepancies], ["S1"]
        )
        self.assertEqual(
            [d.external_ref for d in diff.persisting_discrepancies], ["S9"]
        )
        self.assertEqual(
            [d.external_ref for d in diff.resolved_discrepancies], ["S3"]
        )

    def test_recorded_run_reproducible_after_restart(self):
        self._seed_ref_only()
        stmt = self._ref_statement()
        scope = ReconciliationScope()
        run = self.reconciler.reconcile_and_record(stmt, scope)

        restarted = Reconciler(reopen_service(self.db_path).repo)
        reloaded = restarted.get_run(run.run_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.statement_digest, run.statement_digest)
        self.assertEqual(signatures(reloaded.matched), signatures(run.matched))
        self.assertEqual(signatures(reloaded.discrepancies), signatures(run.discrepancies))

        # Same inputs after restart -> same conclusion.
        replay = restarted.reconcile(stmt, scope)
        self._log_report("replay-after-restart", replay)
        self.assertEqual(signatures(replay.matched), signatures(run.matched))
        self.assertEqual(signatures(replay.discrepancies), signatures(run.discrepancies))

    def test_concurrent_recorded_runs_are_isolated_and_read_only(self):
        seed_with_refs(self.service)
        stmt = full_statement()
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

        threads = 8
        log.info("CONCURRENT-RECONCILE threads=%d", threads)
        with ThreadPoolExecutor(max_workers=threads) as pool:
            runs = list(
                pool.map(lambda _: self.reconciler.reconcile_and_record(stmt), range(threads))
            )

        # Every run recorded its own row; all conclusions identical.
        run_ids = {r.run_id for r in runs}
        self.assertEqual(len(run_ids), threads)
        expected_matched = signatures(runs[0].matched)
        expected_disc = signatures(runs[0].discrepancies)
        for r in runs:
            self.assertEqual(signatures(r.matched), expected_matched)
            self.assertEqual(signatures(r.discrepancies), expected_disc)
        persisted = self.reconciler.list_runs()
        self.assertEqual(len(persisted), threads)

        # Fund movements untouched by the concurrent reconciliation.
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
        log.info("CONCURRENT-RECONCILE verified: %d identical runs, ledger unchanged", threads)


if __name__ == "__main__":
    unittest.main()
