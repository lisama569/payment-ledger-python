"""Reconciliation with settlement references, scoping and run journal.

Covers: mixed ref/no-ref matching, the two one-sided discrepancy
directions, reference reuse detection, window/incremental vs full-run
consistency, run-to-run discrepancy diffs, restart determinism and
concurrent reconcile runs.
"""
from __future__ import annotations

import logging
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

from payment_ledger.models import (
    AuthorizeRequest,
    CaptureRequest,
    EntryType,
    RefundRequest,
)
from payment_ledger.reconciliation import Reconciler, _match
from payment_ledger.reconciliation_types import (
    DiscrepancyType,
    ReconcileScope,
    SettlementRecord,
)
from payment_ledger.repository import LedgerRepository

from .support import LedgerTestCase, log, reopen_service


def seed_mixed(svc) -> None:
    """p1: ref-registered capture+refund; p2: legacy ref-less captures."""
    svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "a1")
    svc.capture(CaptureRequest("p1", 4000, "USD", "ST-1"), "c1")
    svc.refund(RefundRequest("p1", 1000, "USD", "ST-2"), "r1")
    svc.authorize(AuthorizeRequest("p2", 5000, "USD"), "a2")
    svc.capture(CaptureRequest("p2", 2000, "USD"), "c2")
    svc.capture(CaptureRequest("p2", 3000, "USD"), "c3")


def kinds(report):
    return [d.discrepancy for d in report.discrepancies]


class MixedRefReconciliationTests(LedgerTestCase):
    def _run(self, statement, **kw):
        log.info("RECONCILE statement=%s scope=%s", [s.__dict__ for s in statement], kw)
        report = Reconciler(self.service.repo).reconcile(statement, **kw)
        for m in report.matched:
            log.info("RECONCILE judgement=MATCHED payment=%s type=%s amount=%s ref=%s",
                     m.payment_id, m.entry_type.value, m.ledger_amount, m.external_ref)
        for d in report.discrepancies:
            log.info("RECONCILE judgement=REJECTED discrepancy=%s payment=%s ref=%s reason=%s",
                     d.discrepancy.value, d.payment_id, d.external_ref, d.detail)
        return report

    def test_mixed_ref_and_legacy_entries_reconcile_together(self):
        seed_mixed(self.service)
        statement = [
            SettlementRecord("ST-1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("ST-2", "p1", EntryType.REFUND, 1000, "USD"),
            # legacy entries pair positionally with unregistered refs
            SettlementRecord("E-1", "p2", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E-2", "p2", EntryType.CAPTURE, 3000, "USD"),
        ]
        report = self._run(statement)
        self.assertEqual(len(report.matched), 4)
        self.assertEqual(report.discrepancies, [])

    def test_ref_matching_is_by_reference_not_position(self):
        seed_mixed(self.service)
        # Statement lines arrive in a scrambled order and with amounts
        # that would misalign under positional matching; references
        # must still pair each line with its own ledger entry.
        statement = [
            SettlementRecord("ST-2", "p1", EntryType.REFUND, 1000, "USD"),
            SettlementRecord("ST-1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("E-2", "p2", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("E-1", "p2", EntryType.CAPTURE, 2000, "USD"),
        ]
        report = self._run(statement)
        self.assertEqual(len(report.matched), 4)
        self.assertEqual(report.discrepancies, [])

    def test_amount_mismatch_detected_on_ref_pair(self):
        seed_mixed(self.service)
        statement = [
            SettlementRecord("ST-1", "p1", EntryType.CAPTURE, 3999, "USD"),
            SettlementRecord("ST-2", "p1", EntryType.REFUND, 1000, "USD"),
            SettlementRecord("E-1", "p2", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E-2", "p2", EntryType.CAPTURE, 3000, "USD"),
        ]
        report = self._run(statement)
        mism = [d for d in report.discrepancies
                if d.discrepancy == DiscrepancyType.AMOUNT_MISMATCH]
        self.assertEqual(len(mism), 1)
        self.assertEqual(mism[0].external_ref, "ST-1")
        self.assertEqual((mism[0].ledger_amount, mism[0].statement_amount), (4000, 3999))

    def test_two_one_sided_directions_are_distinguishable(self):
        seed_mixed(self.service)
        statement = [
            # ST-1 settled with wrong... no: ST-1 settled fine.
            SettlementRecord("ST-1", "p1", EntryType.CAPTURE, 4000, "USD"),
            # ST-2 (ledger refund) is NOT in the statement -> ledger only.
            # ST-9 settled outside but never booked -> statement only.
            SettlementRecord("ST-9", "p9", EntryType.CAPTURE, 700, "USD"),
            SettlementRecord("E-1", "p2", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E-2", "p2", EntryType.CAPTURE, 3000, "USD"),
        ]
        report = self._run(statement)
        missing_in_ledger = [d for d in report.discrepancies
                             if d.discrepancy == DiscrepancyType.MISSING_IN_LEDGER]
        missing_in_statement = [d for d in report.discrepancies
                                if d.discrepancy == DiscrepancyType.MISSING_IN_STATEMENT]
        self.assertEqual(len(missing_in_ledger), 1)
        self.assertEqual(missing_in_ledger[0].external_ref, "ST-9")
        self.assertIsNone(missing_in_ledger[0].ledger_amount)
        self.assertEqual(missing_in_ledger[0].statement_amount, 700)
        self.assertEqual(len(missing_in_statement), 1)
        self.assertEqual(missing_in_statement[0].external_ref, "ST-2")
        self.assertEqual(missing_in_statement[0].ledger_amount, 1000)
        self.assertIsNone(missing_in_statement[0].statement_amount)

    def test_ref_reused_in_statement_feed_is_flagged(self):
        seed_mixed(self.service)
        statement = [
            SettlementRecord("ST-1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("ST-1", "p1", EntryType.CAPTURE, 4000, "USD"),  # fed twice
            SettlementRecord("ST-2", "p1", EntryType.REFUND, 1000, "USD"),
            SettlementRecord("E-1", "p2", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E-2", "p2", EntryType.CAPTURE, 3000, "USD"),
        ]
        report = self._run(statement)
        dupes = [d for d in report.discrepancies
                 if d.discrepancy == DiscrepancyType.DUPLICATE_IN_LEDGER
                 and d.external_ref == "ST-1"]
        self.assertEqual(len(dupes), 1)
        self.assertIn("appears 2 times", dupes[0].detail)

    def test_ref_reused_across_ledger_entries_is_flagged(self):
        # The unique index prevents this through the service, but legacy
        # data imported before the index existed can contain it; the
        # matcher must still recognise the reuse.
        from payment_ledger.models import LedgerEntry
        entries = [
            LedgerEntry(1, "p1", EntryType.CAPTURE, 4000, "USD", "k1", "t1", "ST-X"),
            LedgerEntry(2, "p1", EntryType.CAPTURE, 4000, "USD", "k2", "t2", "ST-X"),
        ]
        statement = [SettlementRecord("ST-X", "p1", EntryType.CAPTURE, 4000, "USD")]
        matched, discrepancies = _match(entries, statement)
        log.info("RECONCILE ledger-ref-reuse discrepancies=%s",
                 [(d.discrepancy.value, d.detail) for d in discrepancies])
        self.assertEqual(len(matched), 1)
        dupes = [d for d in discrepancies
                 if d.discrepancy == DiscrepancyType.DUPLICATE_IN_LEDGER]
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0].external_ref, "ST-X")
        self.assertEqual(dupes[0].ledger_seq, 2)

    def test_legacy_data_produces_no_new_discrepancies(self):
        # Pure pre-upgrade data (no references anywhere) must reconcile
        # exactly as before the reference capability existed.
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "a1")
        svc.capture(CaptureRequest("p1", 4000, "USD"), "c1")
        svc.capture(CaptureRequest("p1", 3000, "USD"), "c2")
        svc.refund(RefundRequest("p1", 1000, "USD"), "r1")
        statement = [
            SettlementRecord("E1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("E2", "p1", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("E3", "p1", EntryType.REFUND, 1000, "USD"),
        ]
        report = self._run(statement)
        self.assertEqual(len(report.matched), 3)
        self.assertEqual(report.discrepancies, [])


class ScopedReconciliationTests(LedgerTestCase):
    def _seed_two_days(self):
        """Day 1: p1 captures (refs ST-D1-*). Day 2: p2 captures (ST-D2-*)."""
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "a1")
        svc.capture(CaptureRequest("p1", 4000, "USD", "ST-D1-1"), "c1")
        svc.capture(CaptureRequest("p1", 3000, "USD", "ST-D1-2"), "c2")
        day1_max_seq = max(e.seq for e in svc.get_payment("p1").entries)
        svc.authorize(AuthorizeRequest("p2", 9000, "USD"), "a2")
        svc.capture(CaptureRequest("p2", 2000, "USD", "ST-D2-1"), "c3")
        svc.capture(CaptureRequest("p2", 1000, "USD", "ST-D2-2"), "c4")
        return day1_max_seq

    def test_window_run_matches_full_run_restricted_to_window(self):
        day1_max = self._seed_two_days()
        full_statement = [
            SettlementRecord("ST-D1-1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("ST-D1-2", "p1", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("ST-D2-1", "p2", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("ST-D2-2", "p2", EntryType.CAPTURE, 999, "USD"),  # mismatch
            SettlementRecord("ST-D2-9", "p9", EntryType.CAPTURE, 50, "USD"),   # not booked
        ]
        rec = Reconciler(self.service.repo)
        full = rec.reconcile(full_statement)
        day2_statement = full_statement[2:]
        window = rec.reconcile(day2_statement, scope=ReconcileScope(since_seq=day1_max))

        # Full run restricted to day-2 references must equal the window run.
        day2_refs = {"ST-D2-1", "ST-D2-2", "ST-D2-9"}
        full_matched = [m for m in full.matched if m.external_ref in day2_refs]
        full_disc = [d for d in full.discrepancies if d.external_ref in day2_refs]
        self.assertEqual(
            [(m.external_ref, m.ledger_amount) for m in full_matched],
            [(m.external_ref, m.ledger_amount) for m in window.matched],
        )
        self.assertEqual(
            [(d.discrepancy, d.external_ref, d.ledger_amount, d.statement_amount)
             for d in full_disc],
            [(d.discrepancy, d.external_ref, d.ledger_amount, d.statement_amount)
             for d in window.discrepancies],
        )
        # And the window run really saw the mismatch + missing booking.
        self.assertIn(DiscrepancyType.AMOUNT_MISMATCH, kinds(window))
        self.assertIn(DiscrepancyType.MISSING_IN_LEDGER, kinds(window))

    def test_time_window_run(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "a1")
        svc.capture(CaptureRequest("p1", 4000, "USD", "ST-T1"), "c1")
        svc.authorize(AuthorizeRequest("p2", 9000, "USD"), "a2")
        # Boundary strictly between day 1 and day 2: p2's authorize ts.
        boundary = svc.get_payment("p2").entries[0].created_at
        svc.capture(CaptureRequest("p2", 2000, "USD", "ST-T2"), "c2")

        rec = Reconciler(self.service.repo)
        statement = [SettlementRecord("ST-T2", "p2", EntryType.CAPTURE, 2000, "USD")]
        report = rec.reconcile(statement, scope=ReconcileScope(since_time=boundary))
        log.info("RECONCILE time-window since=%s matched=%s discrepancies=%s",
                 boundary, len(report.matched), len(report.discrepancies))
        self.assertEqual(len(report.matched), 1)
        self.assertEqual(report.matched[0].external_ref, "ST-T2")
        self.assertEqual(report.discrepancies, [])

    def test_incremental_run_covers_only_new_entries(self):
        day1_max = self._seed_two_days()
        rec = Reconciler(self.service.repo)
        # First (full) run over day 1 + day 2 statement.
        rec.reconcile([
            SettlementRecord("ST-D1-1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("ST-D1-2", "p1", EntryType.CAPTURE, 3000, "USD"),
        ])
        # Day 3 arrives after the first run.
        svc = self.service
        svc.authorize(AuthorizeRequest("p3", 5000, "USD"), "a3")
        svc.capture(CaptureRequest("p3", 5000, "USD", "ST-D3-1"), "c5")

        inc = rec.reconcile_incremental([
            SettlementRecord("ST-D3-1", "p3", EntryType.CAPTURE, 5000, "USD"),
        ])
        log.info("RECONCILE incremental matched=%s discrepancies=%s",
                 len(inc.matched), len(inc.discrepancies))
        self.assertEqual(len(inc.matched), 1)
        self.assertEqual(inc.matched[0].external_ref, "ST-D3-1")
        self.assertEqual(inc.discrepancies, [])
        # The incremental run only looked at entries after the first run.
        self.assertTrue(all(m.ledger_seq > day1_max for m in inc.matched))

        # A full run restricted to the same new entries agrees.
        full = rec.reconcile([
            SettlementRecord("ST-D1-1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("ST-D1-2", "p1", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("ST-D3-1", "p3", EntryType.CAPTURE, 5000, "USD"),
        ])
        new_refs = {"ST-D3-1"}
        self.assertEqual(
            [(m.external_ref, m.ledger_amount) for m in full.matched if m.external_ref in new_refs],
            [(m.external_ref, m.ledger_amount) for m in inc.matched],
        )


class RunJournalTests(LedgerTestCase):
    def test_run_is_persisted_and_reproducible_after_restart(self):
        seed_mixed(self.service)
        statement = [
            SettlementRecord("ST-1", "p1", EntryType.CAPTURE, 4001, "USD"),  # mismatch
            SettlementRecord("ST-2", "p1", EntryType.REFUND, 1000, "USD"),
            SettlementRecord("E-1", "p2", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E-2", "p2", EntryType.CAPTURE, 3000, "USD"),
        ]
        rec = Reconciler(self.service.repo)
        first = rec.reconcile(statement)
        self.assertIsNotNone(first.run_id)

        # Restart the service against the same DB file and re-run with
        # the same input: the conclusion must be identical.
        svc2 = reopen_service(self.db_path)
        rec2 = Reconciler(svc2.repo)
        second = rec2.reconcile(statement)
        self.assertEqual(
            [(m.external_ref, m.ledger_amount, m.statement_amount) for m in first.matched],
            [(m.external_ref, m.ledger_amount, m.statement_amount) for m in second.matched],
        )
        self.assertEqual(
            [(d.discrepancy, d.external_ref, d.ledger_amount, d.statement_amount)
             for d in first.discrepancies],
            [(d.discrepancy, d.external_ref, d.ledger_amount, d.statement_amount)
             for d in second.discrepancies],
        )

        # The first run's conclusion is retrievable from the journal.
        persisted = rec2.get_run(first.run_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(
            [(d.discrepancy, d.external_ref) for d in persisted["discrepancies"]],
            [(d.discrepancy, d.external_ref) for d in first.discrepancies],
        )
        runs = rec2.list_runs()
        self.assertEqual([r["run_id"] for r in runs], [first.run_id, second.run_id])
        log.info("JOURNAL runs=%s", runs)

    def test_consecutive_runs_show_new_ongoing_resolved(self):
        seed_mixed(self.service)
        rec = Reconciler(self.service.repo)
        # Run 1: one amount mismatch (ST-1) and one missing booking (ST-9).
        rec.reconcile([
            SettlementRecord("ST-1", "p1", EntryType.CAPTURE, 4001, "USD"),
            SettlementRecord("ST-2", "p1", EntryType.REFUND, 1000, "USD"),
            SettlementRecord("ST-9", "p9", EntryType.CAPTURE, 700, "USD"),
            SettlementRecord("E-1", "p2", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E-2", "p2", EntryType.CAPTURE, 3000, "USD"),
        ])
        # Run 2: ST-1 fixed (resolved), ST-9 still missing (ongoing),
        # and a new currency mismatch appears on ST-2.
        second = rec.reconcile([
            SettlementRecord("ST-1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("ST-2", "p1", EntryType.REFUND, 1000, "EUR"),
            SettlementRecord("ST-9", "p9", EntryType.CAPTURE, 700, "USD"),
            SettlementRecord("E-1", "p2", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E-2", "p2", EntryType.CAPTURE, 3000, "USD"),
        ])
        log.info("DIFF new=%s ongoing=%s resolved=%s",
                 [(d.discrepancy.value, d.external_ref) for d in second.new_discrepancies],
                 [(d.discrepancy.value, d.external_ref) for d in second.ongoing_discrepancies],
                 [(d.discrepancy.value, d.external_ref) for d in second.resolved_discrepancies])
        self.assertEqual(
            [(d.discrepancy, d.external_ref) for d in second.new_discrepancies],
            [(DiscrepancyType.CURRENCY_MISMATCH, "ST-2")],
        )
        self.assertEqual(
            [(d.discrepancy, d.external_ref) for d in second.ongoing_discrepancies],
            [(DiscrepancyType.MISSING_IN_LEDGER, "ST-9")],
        )
        self.assertEqual(
            [(d.discrepancy, d.external_ref) for d in second.resolved_discrepancies],
            [(DiscrepancyType.AMOUNT_MISMATCH, "ST-1")],
        )

    def test_concurrent_reconcile_runs_do_not_interfere(self):
        seed_mixed(self.service)
        statement = [
            SettlementRecord("ST-1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("ST-2", "p1", EntryType.REFUND, 1000, "USD"),
            SettlementRecord("ST-9", "p9", EntryType.CAPTURE, 700, "USD"),
            SettlementRecord("E-1", "p2", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E-2", "p2", EntryType.CAPTURE, 3000, "USD"),
        ]
        before = [
            (e.seq, e.entry_type.value, e.amount, e.external_ref)
            for pid in ("p1", "p2")
            for e in self.service.get_payment(pid).entries
        ]
        rec = Reconciler(self.service.repo)
        threads = 8
        log.info("RECONCILE concurrent threads=%d", threads)

        reports = []
        errors = []

        def fire():
            try:
                reports.append(rec.reconcile(statement))
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = [pool.submit(fire) for _ in range(threads)]
            for f in as_completed(futures):
                f.result()

        self.assertEqual(errors, [])
        self.assertEqual(len(reports), threads)
        # Every run reached the same conclusion.
        signature = [
            (
                [(m.external_ref, m.ledger_amount) for m in r.matched],
                [(d.discrepancy, d.external_ref) for d in r.discrepancies],
            )
            for r in reports
        ]
        for sig in signature[1:]:
            self.assertEqual(sig, signature[0])
        # Each run got its own journal id.
        self.assertEqual(len({r.run_id for r in reports}), threads)
        # Fund movements were never touched.
        after = [
            (e.seq, e.entry_type.value, e.amount, e.external_ref)
            for pid in ("p1", "p2")
            for e in self.service.get_payment(pid).entries
        ]
        self.assertEqual(before, after)
        log.info("CONCURRENT-RECONCILE verified: %d identical conclusions, ledger unchanged", threads)

    def test_reconcile_journal_does_not_touch_ledger_entries(self):
        seed_mixed(self.service)
        before = [
            (e.seq, e.entry_type.value, e.amount, e.currency, e.idempotency_key, e.external_ref)
            for pid in ("p1", "p2")
            for e in self.service.get_payment(pid).entries
        ]
        rec = Reconciler(self.service.repo)
        rec.reconcile([SettlementRecord("ST-1", "p1", EntryType.CAPTURE, 1, "USD")])
        rec.reconcile([SettlementRecord("ST-2", "p1", EntryType.REFUND, 2, "EUR")])
        after = [
            (e.seq, e.entry_type.value, e.amount, e.currency, e.idempotency_key, e.external_ref)
            for pid in ("p1", "p2")
            for e in self.service.get_payment(pid).entries
        ]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
