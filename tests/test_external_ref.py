"""External settlement reference registration on capture/refund.

Covers: registering a reference, global uniqueness (across payments and
across capture/refund), old-style calls without a reference, and replay
of idempotency keys recorded before the field existed.
"""
from __future__ import annotations

import logging
import unittest

from payment_ledger.errors import DuplicateExternalRefError
from payment_ledger.models import (
    AuthorizeRequest,
    CaptureRequest,
    EntryType,
    OperationResult,
    PaymentStatus,
    RefundRequest,
)
from payment_ledger.service import canonical_request_hash

from .support import LedgerTestCase, log_outcome, log_request, reopen_service

log = logging.getLogger("payment_ledger.tests")


class ExternalRefRegistrationTests(LedgerTestCase):
    def test_capture_and_refund_register_external_ref(self):
        svc = self.service
        log_request("authorize", "k-a", {"payment_id": "p1", "amount": 10000, "currency": "USD"})
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")

        log_request("capture", "k-c1", {"payment_id": "p1", "amount": 4000, "currency": "USD", "external_ref": "S-100"})
        c = svc.capture(CaptureRequest("p1", 4000, "USD", external_ref="S-100"), "k-c1")
        log_outcome(True, c)

        log_request("refund", "k-r1", {"payment_id": "p1", "amount": 1000, "currency": "USD", "external_ref": "S-101"})
        r = svc.refund(RefundRequest("p1", 1000, "USD", external_ref="S-101"), "k-r1")
        log_outcome(True, r)

        entries = svc.get_payment("p1").entries
        by_type = {e.entry_type: e for e in entries}
        self.assertEqual(by_type[EntryType.CAPTURE].external_ref, "S-100")
        self.assertEqual(by_type[EntryType.REFUND].external_ref, "S-101")
        self.assertIsNone(by_type[EntryType.AUTHORIZE].external_ref)

    def test_same_ref_rejected_on_same_and_across_payments(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a1")
        svc.authorize(AuthorizeRequest("p2", 10000, "USD"), "k-a2")
        svc.capture(CaptureRequest("p1", 4000, "USD", external_ref="S-200"), "k-c1")

        # Same payment, second capture reusing the reference.
        log_request("capture-dup-same-payment", "k-c2", {"payment_id": "p1", "amount": 1000, "currency": "USD", "external_ref": "S-200"})
        self.assert_rejected(
            DuplicateExternalRefError,
            lambda: svc.capture(CaptureRequest("p1", 1000, "USD", external_ref="S-200"), "k-c2"),
        )
        # Different payment, same reference.
        log_request("capture-dup-cross-payment", "k-c3", {"payment_id": "p2", "amount": 1000, "currency": "USD", "external_ref": "S-200"})
        self.assert_rejected(
            DuplicateExternalRefError,
            lambda: svc.capture(CaptureRequest("p2", 1000, "USD", external_ref="S-200"), "k-c3"),
        )
        # A refund may not reuse a capture's reference either.
        log_request("refund-dup-ref", "k-r1", {"payment_id": "p1", "amount": 500, "currency": "USD", "external_ref": "S-200"})
        self.assert_rejected(
            DuplicateExternalRefError,
            lambda: svc.refund(RefundRequest("p1", 500, "USD", external_ref="S-200"), "k-r1"),
        )

        # The rejected attempts changed nothing: p1 still has 4000
        # capturable-refund state and the failed keys were not consumed.
        payment = svc.get_payment("p1")
        self.assertEqual((payment.captured_amount, payment.refunded_amount), (4000, 0))
        log_request("capture-reuse-key-after-reject", "k-c2", {"payment_id": "p1", "amount": 1000, "currency": "USD", "external_ref": "S-201"})
        c = svc.capture(CaptureRequest("p1", 1000, "USD", external_ref="S-201"), "k-c2")
        log_outcome(True, c)
        self.assertFalse(c.replayed)
        self.assertEqual(svc.get_payment("p1").captured_amount, 5000)

    def test_old_style_calls_without_ref_still_work(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")
        log_request("capture-no-ref", "k-c", {"payment_id": "p1", "amount": 4000, "currency": "USD"})
        c = svc.capture(CaptureRequest("p1", 4000, "USD"), "k-c")
        log_outcome(True, c)
        log_request("refund-no-ref", "k-r", {"payment_id": "p1", "amount": 1000, "currency": "USD"})
        r = svc.refund(RefundRequest("p1", 1000, "USD"), "k-r")
        log_outcome(True, r)
        entries = [e for e in svc.get_payment("p1").entries if e.entry_type != EntryType.AUTHORIZE]
        self.assertEqual([e.external_ref for e in entries], [None, None])

    def test_idempotency_key_recorded_before_upgrade_replays_identically(self):
        """Simulate a key persisted by the old version (payload hash
        without external_ref) and replay it against the upgraded code."""
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")

        # What the old version would have stored for
        # capture(p1, 4000, USD) under key "k-legacy".
        old_payload = {"payment_id": "p1", "amount": 4000, "currency": "USD"}
        old_hash = canonical_request_hash("capture", old_payload)
        old_result = OperationResult(
            payment_id="p1",
            status=PaymentStatus.PARTIALLY_CAPTURED,
            entry_type=EntryType.CAPTURE,
            amount=4000,
            currency="USD",
            captured_amount=4000,
            refunded_amount=0,
            idempotency_key="k-legacy",
            replayed=False,
        )
        from payment_ledger.repository import utc_now_iso

        with svc.repo.transaction() as conn:
            svc.repo.insert_entry(conn, "p1", EntryType.CAPTURE, 4000, "USD", "k-legacy", utc_now_iso())
            svc.repo.save_idempotency(conn, "k-legacy", old_hash, svc._serialize_result(old_result), utc_now_iso())
            payment = svc.repo.get_payment(conn, "p1")
            payment.captured_amount = 4000
            svc.repo.update_payment(conn, payment)

        # Replay the exact old request against the upgraded service.
        log_request("capture-replay-legacy-key", "k-legacy", old_payload)
        replayed = svc.capture(CaptureRequest("p1", 4000, "USD"), "k-legacy")
        log_outcome(True, replayed)
        self.assertTrue(replayed.replayed)
        self.assertEqual(replayed.amount, 4000)
        self.assertEqual(replayed.captured_amount, 4000)
        # No second fund movement landed.
        self.assertEqual(svc.get_payment("p1").captured_amount, 4000)

    def test_restart_replay_with_and_without_ref(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")
        svc.capture(CaptureRequest("p1", 4000, "USD", external_ref="S-300"), "k-c-ref")
        svc.capture(CaptureRequest("p1", 1000, "USD"), "k-c-noref")

        restarted = reopen_service(self.db_path)
        log_request("capture-replay-with-ref", "k-c-ref", {"payment_id": "p1", "amount": 4000, "currency": "USD", "external_ref": "S-300"})
        r1 = restarted.capture(CaptureRequest("p1", 4000, "USD", external_ref="S-300"), "k-c-ref")
        log_outcome(True, r1)
        log_request("capture-replay-no-ref", "k-c-noref", {"payment_id": "p1", "amount": 1000, "currency": "USD"})
        r2 = restarted.capture(CaptureRequest("p1", 1000, "USD"), "k-c-noref")
        log_outcome(True, r2)
        self.assertTrue(r1.replayed)
        self.assertTrue(r2.replayed)
        self.assertEqual(restarted.get_payment("p1").captured_amount, 5000)


if __name__ == "__main__":
    unittest.main()
