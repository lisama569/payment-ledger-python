"""Reconciliation tests: amount mismatch, missing and duplicate bookings.

Also verifies that reconciliation can never mutate committed ledger
data (it runs over a read-only database connection).
"""
from __future__ import annotations

import logging
import unittest

from payment_ledger.models import (
    AuthorizeRequest,
    CaptureRequest,
    EntryType,
    RefundRequest,
)
from payment_ledger.reconciliation import Reconciler
from payment_ledger.reconciliation_types import (
    DiscrepancyType,
    SettlementRecord,
)

from .support import LedgerTestCase, log

log = logging.getLogger("payment_ledger.tests")


def seed_ledger(svc) -> None:
    """p1: captured 4000+3000, refunded 1000; p2: captured 5000."""
    svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "a1")
    svc.capture(CaptureRequest("p1", 4000, "USD"), "c1")
    svc.capture(CaptureRequest("p1", 3000, "USD"), "c2")
    svc.refund(RefundRequest("p1", 1000, "USD"), "r1")
    svc.authorize(AuthorizeRequest("p2", 5000, "USD"), "a2")
    svc.capture(CaptureRequest("p2", 5000, "USD"), "c3")
    # p3: two equal captures (duplicate booking scenario): 2000 + 2000
    svc.authorize(AuthorizeRequest("p3", 9000, "USD"), "a3")
    svc.capture(CaptureRequest("p3", 2000, "USD"), "c4")
    svc.capture(CaptureRequest("p3", 2000, "USD"), "c5")


class ReconciliationTests(LedgerTestCase):
    def _run(self, statement):
        log.info("RECONCILE statement=%s", [s.__dict__ for s in statement])
        report = Reconciler(self.service.repo).reconcile(statement)
        for m in report.matched:
            log.info(
                "RECONCILE judgement=MATCHED payment=%s type=%s amount=%s ref=%s",
                m.payment_id, m.entry_type.value, m.ledger_amount, m.external_ref,
            )
        for d in report.discrepancies:
            log.info(
                "RECONCILE judgement=REJECTED discrepancy=%s payment=%s type=%s "
                "ledger=%s statement=%s reason=%s",
                d.discrepancy.value,
                d.payment_id,
                d.entry_type.value,
                d.ledger_amount,
                d.statement_amount,
                d.detail,
            )
        return report

    def test_clean_statement_matches_everything(self):
        seed_ledger(self.service)
        # p3 legitimately has two settlements of 2000 -> no duplicate.
        statement = [
            SettlementRecord("E1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("E2", "p1", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("E3", "p1", EntryType.REFUND, 1000, "USD"),
            SettlementRecord("E4", "p2", EntryType.CAPTURE, 5000, "USD"),
            SettlementRecord("E5", "p3", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E6", "p3", EntryType.CAPTURE, 2000, "USD"),
        ]
        report = self._run(statement)
        self.assertEqual(len(report.matched), 6)
        self.assertEqual(report.discrepancies, [])

    def test_amount_mismatch_is_detected_distinguishably(self):
        seed_ledger(self.service)
        statement = [
            SettlementRecord("E1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("E2", "p1", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("E3", "p1", EntryType.REFUND, 1000, "USD"),
            SettlementRecord("E4", "p2", EntryType.CAPTURE, 4999, "USD"),  # off by one unit
            SettlementRecord("E5", "p3", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E6", "p3", EntryType.CAPTURE, 2000, "USD"),
        ]
        report = self._run(statement)
        mismatches = [d for d in report.discrepancies if d.discrepancy == DiscrepancyType.AMOUNT_MISMATCH]
        self.assertEqual(len(mismatches), 1)
        d = mismatches[0]
        self.assertEqual(d.payment_id, "p2")
        self.assertEqual((d.ledger_amount, d.statement_amount), (5000, 4999))

    def test_missing_in_ledger_is_detected_distinguishably(self):
        seed_ledger(self.service)
        # Statement claims a capture for a payment that has no ledger booking.
        statement = [
            SettlementRecord("E1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("E2", "p1", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("E3", "p1", EntryType.REFUND, 1000, "USD"),
            SettlementRecord("E4", "p2", EntryType.CAPTURE, 5000, "USD"),
            SettlementRecord("E5", "p3", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E6", "p3", EntryType.CAPTURE, 2000, "USD"),
            SettlementRecord("E9", "p9", EntryType.CAPTURE, 700, "USD"),
        ]
        report = self._run(statement)
        missing = [d for d in report.discrepancies if d.discrepancy == DiscrepancyType.MISSING_IN_LEDGER]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].payment_id, "p9")
        self.assertEqual(missing[0].statement_amount, 700)
        self.assertIsNone(missing[0].ledger_amount)

    def test_duplicate_in_ledger_is_detected_distinguishably(self):
        seed_ledger(self.service)
        # p3 was captured twice (4000 total) but the external side only
        # settled one 2000 line: the second ledger booking is a duplicate.
        statement = [
            SettlementRecord("E1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("E2", "p1", EntryType.CAPTURE, 3000, "USD"),
            SettlementRecord("E3", "p1", EntryType.REFUND, 1000, "USD"),
            SettlementRecord("E4", "p2", EntryType.CAPTURE, 5000, "USD"),
            SettlementRecord("E5", "p3", EntryType.CAPTURE, 2000, "USD"),
        ]
        report = self._run(statement)
        dupes = [d for d in report.discrepancies if d.discrepancy == DiscrepancyType.DUPLICATE_IN_LEDGER]
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0].payment_id, "p3")
        self.assertEqual(dupes[0].ledger_amount, 2000)
        self.assertIsNone(dupes[0].statement_amount)

    def test_three_discrepancy_kinds_coexist_and_remain_distinguishable(self):
        seed_ledger(self.service)
        statement = [
            # p1 captures match; refund line missing on statement side
            # (informational, not one of the 3 required kinds)
            SettlementRecord("E1", "p1", EntryType.CAPTURE, 4000, "USD"),
            SettlementRecord("E2", "p1", EntryType.CAPTURE, 3000, "USD"),
            # p2: amount mismatch 5000 vs 4900
            SettlementRecord("E4", "p2", EntryType.CAPTURE, 4900, "USD"),
            # p3: single line vs two ledger entries -> duplicate
            SettlementRecord("E5", "p3", EntryType.CAPTURE, 2000, "USD"),
            # p9: settled but no ledger entry -> missing in ledger
            SettlementRecord("E9", "p9", EntryType.REFUND, 123, "USD"),
        ]
        report = self._run(statement)
        kinds = {d.discrepancy for d in report.discrepancies}
        self.assertIn(DiscrepancyType.AMOUNT_MISMATCH, kinds)
        self.assertIn(DiscrepancyType.MISSING_IN_LEDGER, kinds)
        self.assertIn(DiscrepancyType.DUPLICATE_IN_LEDGER, kinds)
        # every discrepancy carries a distinct, stable code
        codes = {k.value for k in kinds}
        self.assertEqual(
            codes & {"AMOUNT_MISMATCH", "MISSING_IN_LEDGER", "DUPLICATE_IN_LEDGER"},
            {"AMOUNT_MISMATCH", "MISSING_IN_LEDGER", "DUPLICATE_IN_LEDGER"},
        )

    def test_currency_mismatch_is_its_own_distinct_kind(self):
        seed_ledger(self.service)
        statement = [
            SettlementRecord("E4", "p2", EntryType.CAPTURE, 5000, "EUR"),
        ]
        report = self._run(statement)
        cur = [d for d in report.discrepancies if d.discrepancy == DiscrepancyType.CURRENCY_MISMATCH]
        self.assertTrue(cur)

    def test_reconciliation_is_strictly_read_only(self):
        seed_ledger(self.service)
        before = {
            (e.seq, e.entry_type.value, e.amount): e.idempotency_key
            for pid in ("p1", "p2", "p3")
            for e in self.service.get_payment(pid).entries
        }
        payment_snapshot = {
            pid: (
                self.service.get_payment(pid).captured_amount,
                self.service.get_payment(pid).refunded_amount,
                self.service.get_payment(pid).status.value,
            )
            for pid in ("p1", "p2", "p3")
        }
        statement = [
            SettlementRecord("E9", "p9", EntryType.CAPTURE, 700, "USD"),
            SettlementRecord("E4", "p2", EntryType.CAPTURE, 1, "USD"),
        ]
        Reconciler(self.service.repo).reconcile(statement)
        # Run it again to be sure nothing was written on the first pass.
        Reconciler(self.service.repo).reconcile(statement)
        after = {
            (e.seq, e.entry_type.value, e.amount): e.idempotency_key
            for pid in ("p1", "p2", "p3")
            for e in self.service.get_payment(pid).entries
        }
        self.assertEqual(before, after)
        for pid in ("p1", "p2", "p3"):
            self.assertEqual(
                (
                    self.service.get_payment(pid).captured_amount,
                    self.service.get_payment(pid).refunded_amount,
                    self.service.get_payment(pid).status.value,
                ),
                payment_snapshot[pid],
            )
        log.info("READ-ONLY verified: ledger unchanged after two reconcile passes")


if __name__ == "__main__":
    unittest.main()
