"""External settlement reference registration on capture/refund.

Covers: registering a settlement reference on fund movements, global
uniqueness across payments, backward compatibility of ref-less calls,
and replay of idempotency keys persisted before the capability existed.
"""
from __future__ import annotations

import logging
import unittest

from payment_ledger.errors import (
    DuplicateExternalRefError,
    IdempotencyConflictError,
    InvalidAmountError,
)
from payment_ledger.models import (
    AuthorizeRequest,
    CaptureRequest,
    EntryType,
    RefundRequest,
)

from .support import LedgerTestCase, log_outcome, log_request, reopen_service

log = logging.getLogger("payment_ledger.tests")


class ExternalRefRegistrationTests(LedgerTestCase):
    def test_capture_and_refund_register_external_ref(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")

        log_request("capture-with-ref", "k-c1",
                    {"payment_id": "p1", "amount": 4000, "currency": "USD", "external_ref": "ST-100"})
        c = svc.capture(CaptureRequest("p1", 4000, "USD", "ST-100"), "k-c1")
        log_outcome(True, c)
        self.assertEqual(c.external_ref, "ST-100")

        log_request("refund-with-ref", "k-r1",
                    {"payment_id": "p1", "amount": 1000, "currency": "USD", "external_ref": "ST-101"})
        r = svc.refund(RefundRequest("p1", 1000, "USD", "ST-101"), "k-r1")
        log_outcome(True, r)
        self.assertEqual(r.external_ref, "ST-101")

        entries = svc.get_payment("p1").entries
        by_type = [(e.entry_type, e.external_ref) for e in entries]
        self.assertIn((EntryType.CAPTURE, "ST-100"), by_type)
        self.assertIn((EntryType.REFUND, "ST-101"), by_type)
        # AUTHORIZE never carries a settlement reference.
        self.assertIsNone(entries[0].external_ref)

    def test_ref_less_calls_still_work(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 5000, "USD"), "k-a")
        log_request("capture-no-ref", "k-c1", {"payment_id": "p1", "amount": 5000, "currency": "USD"})
        c = svc.capture(CaptureRequest("p1", 5000, "USD"), "k-c1")
        log_outcome(True, c)
        self.assertIsNone(c.external_ref)
        entries = svc.get_payment("p1").entries
        self.assertTrue(all(e.external_ref is None for e in entries))

    def test_same_ref_rejected_on_second_movement_same_payment(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")
        svc.capture(CaptureRequest("p1", 4000, "USD", "ST-200"), "k-c1")
        log_request("capture-dup-ref", "k-c2",
                    {"payment_id": "p1", "amount": 1000, "currency": "USD", "external_ref": "ST-200"})
        self.assert_rejected(
            DuplicateExternalRefError,
            lambda: svc.capture(CaptureRequest("p1", 1000, "USD", "ST-200"), "k-c2"),
        )

    def test_same_ref_rejected_across_payments_and_operation_types(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a1")
        svc.authorize(AuthorizeRequest("p2", 10000, "USD"), "k-a2")
        svc.capture(CaptureRequest("p1", 4000, "USD", "ST-300"), "k-c1")
        svc.capture(CaptureRequest("p2", 4000, "USD"), "k-c2")

        # different payment, same reference
        log_request("capture-cross-payment-dup-ref", "k-c3",
                    {"payment_id": "p2", "amount": 1000, "currency": "USD", "external_ref": "ST-300"})
        self.assert_rejected(
            DuplicateExternalRefError,
            lambda: svc.capture(CaptureRequest("p2", 1000, "USD", "ST-300"), "k-c3"),
        )
        # refund reusing a capture's reference
        log_request("refund-cross-type-dup-ref", "k-r1",
                    {"payment_id": "p2", "amount": 1000, "currency": "USD", "external_ref": "ST-300"})
        self.assert_rejected(
            DuplicateExternalRefError,
            lambda: svc.refund(RefundRequest("p2", 1000, "USD", "ST-300"), "k-r1"),
        )

    def test_rejected_duplicate_ref_does_not_consume_idempotency_key(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")
        svc.capture(CaptureRequest("p1", 4000, "USD", "ST-400"), "k-c1")
        self.assert_rejected(
            DuplicateExternalRefError,
            lambda: svc.capture(CaptureRequest("p1", 1000, "USD", "ST-400"), "k-c2"),
        )
        # The failed attempt wrote no idempotency record: the same key
        # may be retried with a corrected (unique) reference.
        log_request("capture-retry-fixed-ref", "k-c2",
                    {"payment_id": "p1", "amount": 1000, "currency": "USD", "external_ref": "ST-401"})
        c = svc.capture(CaptureRequest("p1", 1000, "USD", "ST-401"), "k-c2")
        log_outcome(True, c)
        self.assertEqual(c.external_ref, "ST-401")

    def test_blank_external_ref_is_rejected(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 1000, "USD"), "k-a")
        self.assert_rejected(
            InvalidAmountError,
            lambda: svc.capture(CaptureRequest("p1", 1000, "USD", "  "), "k-c1"),
        )

    def test_idempotent_replay_with_ref_returns_same_conclusion(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")
        first = svc.capture(CaptureRequest("p1", 4000, "USD", "ST-500"), "k-c1")
        log_request("capture-replay", "k-c1",
                    {"payment_id": "p1", "amount": 4000, "currency": "USD", "external_ref": "ST-500"})
        replay = svc.capture(CaptureRequest("p1", 4000, "USD", "ST-500"), "k-c1")
        log_outcome(True, replay)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.external_ref, "ST-500")
        self.assertEqual(replay.amount, first.amount)
        self.assertEqual(replay.captured_amount, first.captured_amount)
        # exactly one capture entry landed
        entries = [e for e in svc.get_payment("p1").entries if e.entry_type == EntryType.CAPTURE]
        self.assertEqual(len(entries), 1)

    def test_same_key_with_different_ref_is_a_conflict(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")
        svc.capture(CaptureRequest("p1", 4000, "USD", "ST-600"), "k-c1")
        log_request("capture-same-key-other-ref", "k-c1",
                    {"payment_id": "p1", "amount": 4000, "currency": "USD", "external_ref": "ST-601"})
        self.assert_rejected(
            IdempotencyConflictError,
            lambda: svc.capture(CaptureRequest("p1", 4000, "USD", "ST-601"), "k-c1"),
        )


class PreUpgradeIdempotencyCompatTests(LedgerTestCase):
    """Idempotency records written by ref-less (pre-upgrade) calls must
    replay with the exact same conclusion after the upgrade."""

    def test_legacy_key_replays_identically_after_restart(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")
        log_request("capture-legacy-no-ref", "k-c1",
                    {"payment_id": "p1", "amount": 4000, "currency": "USD"})
        first = svc.capture(CaptureRequest("p1", 4000, "USD"), "k-c1")
        log_outcome(True, first)

        # Simulate a deploy/restart between the original call and replay.
        svc2 = reopen_service(self.db_path)
        log_request("capture-replay-after-restart", "k-c1",
                    {"payment_id": "p1", "amount": 4000, "currency": "USD"})
        replay = svc2.capture(CaptureRequest("p1", 4000, "USD"), "k-c1")
        log_outcome(True, replay)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.amount, first.amount)
        self.assertEqual(replay.captured_amount, first.captured_amount)
        self.assertEqual(replay.status, first.status)
        self.assertIsNone(replay.external_ref)

        # Still exactly one capture entry after the replay.
        entries = [e for e in svc2.get_payment("p1").entries if e.entry_type == EntryType.CAPTURE]
        self.assertEqual(len(entries), 1)

    def test_legacy_key_still_conflicts_with_different_content(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")
        svc.capture(CaptureRequest("p1", 4000, "USD"), "k-c1")
        svc2 = reopen_service(self.db_path)
        # Same legacy key, but now carrying a reference -> conflict.
        log_request("capture-legacy-key-with-ref", "k-c1",
                    {"payment_id": "p1", "amount": 4000, "currency": "USD", "external_ref": "ST-700"})
        self.assert_rejected(
            IdempotencyConflictError,
            lambda: svc2.capture(CaptureRequest("p1", 4000, "USD", "ST-700"), "k-c1"),
        )


if __name__ == "__main__":
    unittest.main()
