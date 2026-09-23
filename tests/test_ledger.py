"""State transitions, partial amount boundaries and idempotency replay."""
from __future__ import annotations

import unittest

from payment_ledger.errors import (
    AmountExceededError,
    CurrencyMismatchError,
    IdempotencyConflictError,
    InvalidAmountError,
    InvalidStateTransitionError,
    PaymentNotFoundError,
)
from payment_ledger.models import (
    AuthorizeRequest,
    CaptureRequest,
    EntryType,
    PaymentStatus,
    RefundRequest,
)

from .support import LedgerTestCase, log_outcome, log_request, reopen_service


class StateTransitionTests(LedgerTestCase):
    def test_authorize_partial_capture_partial_refund_full_flow(self):
        svc = self.service
        log_request("authorize", "k-a", {"payment_id": "p1", "amount": 10000, "currency": "USD"})
        a = svc.authorize(AuthorizeRequest("p1", 10000, "USD"), "k-a")
        log_outcome(True, a)
        self.assertEqual(a.status, PaymentStatus.AUTHORIZED)

        log_request("capture-partial", "k-c1", {"payment_id": "p1", "amount": 3000, "currency": "USD"})
        c1 = svc.capture(CaptureRequest("p1", 3000, "USD"), "k-c1")
        log_outcome(True, c1)
        self.assertEqual(c1.status, PaymentStatus.PARTIALLY_CAPTURED)
        self.assertEqual(c1.captured_amount, 3000)

        log_request("capture-rest-implicit", "k-c2", {"payment_id": "p1", "amount": None, "currency": "USD"})
        c2 = svc.capture(CaptureRequest("p1", None, "USD"), "k-c2")
        log_outcome(True, c2)
        self.assertEqual(c2.status, PaymentStatus.CAPTURED)
        self.assertEqual(c2.amount, 7000)
        self.assertEqual(c2.captured_amount, 10000)

        log_request("refund-partial", "k-r1", {"payment_id": "p1", "amount": 2500, "currency": "USD"})
        r1 = svc.refund(RefundRequest("p1", 2500, "USD"), "k-r1")
        log_outcome(True, r1)
        self.assertEqual(r1.status, PaymentStatus.PARTIALLY_REFUNDED)

        log_request("refund-rest-implicit", "k-r2", {"payment_id": "p1", "amount": None, "currency": "USD"})
        r2 = svc.refund(RefundRequest("p1", None, "USD"), "k-r2")
        log_outcome(True, r2)
        self.assertEqual(r2.status, PaymentStatus.REFUNDED)
        self.assertEqual(r2.amount, 7500)

        # ledger entries are append-only and explain the whole movement
        payment = svc.get_payment("p1")
        self.assertEqual(
            [(e.entry_type, e.amount) for e in payment.entries],
            [
                (EntryType.AUTHORIZE, 10000),
                (EntryType.CAPTURE, 3000),
                (EntryType.CAPTURE, 7000),
                (EntryType.REFUND, 2500),
                (EntryType.REFUND, 7500),
            ],
        )

    def test_capture_then_refund_partial_keeps_partially_refunded(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p2", 5000, "USD"), "k-a")
        svc.capture(CaptureRequest("p2", 5000, "USD"), "k-c")
        r = svc.refund(RefundRequest("p2", 1, "USD"), "k-r")  # minimum unit boundary
        self.assertEqual(r.status, PaymentStatus.PARTIALLY_REFUNDED)
        self.assertEqual(r.refunded_amount, 1)

    def test_partial_amount_boundaries(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p3", 100, "USD"), "k-a")

        # capture exactly the authorized amount succeeds
        ok = svc.capture(CaptureRequest("p3", 100, "USD"), "k-c")
        self.assertEqual(ok.status, PaymentStatus.CAPTURED)

        # capturing 1 more over the authorized amount is rejected
        log_request("capture-over", "k-cx", {"payment_id": "p3", "amount": 1})
        self.assert_rejected(
            InvalidStateTransitionError,
            lambda: svc.capture(CaptureRequest("p3", 1, "USD"), "k-cx"),
        )

    def test_over_capture_on_partial_authorization_is_rejected(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p4", 1000, "USD"), "k-a")
        svc.capture(CaptureRequest("p4", 600, "USD"), "k-c1")
        log_request("capture-exceed-remaining", "k-c2", {"payment_id": "p4", "amount": 401})
        err = self.assert_rejected(
            AmountExceededError,
            lambda: svc.capture(CaptureRequest("p4", 401, "USD"), "k-c2"),
        )
        self.assertIn("exceeds capturable 400", str(err))

    def test_over_refund_is_rejected(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p5", 1000, "USD"), "k-a")
        svc.capture(CaptureRequest("p5", 300, "USD"), "k-c")
        log_request("refund-exceed", "k-r", {"payment_id": "p5", "amount": 301})
        self.assert_rejected(
            AmountExceededError,
            lambda: svc.refund(RefundRequest("p5", 301, "USD"), "k-r"),
        )
        svc.refund(RefundRequest("p5", 300, "USD"), "k-r2")
        log_request("refund-after-full", "k-r3", {"payment_id": "p5", "amount": 1})
        self.assert_rejected(
            InvalidStateTransitionError,
            lambda: svc.refund(RefundRequest("p5", 1, "USD"), "k-r3"),
        )

    def test_refund_before_any_capture_is_illegal_transition(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p6", 1000, "USD"), "k-a")
        log_request("refund-uncaptured", "k-r", {"payment_id": "p6", "amount": 1})
        self.assert_rejected(
            InvalidStateTransitionError,
            lambda: svc.refund(RefundRequest("p6", 1, "USD"), "k-r"),
        )

    def test_currency_mismatch_is_rejected_with_distinct_code(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p7", 1000, "USD"), "k-a")
        log_request("capture-wrong-currency", "k-c", {"payment_id": "p7", "currency": "EUR"})
        self.assert_rejected(
            CurrencyMismatchError,
            lambda: svc.capture(CaptureRequest("p7", 100, "EUR"), "k-c"),
        )

    def test_unknown_payment_is_not_found(self):
        log_request("capture-unknown", "k-c", {"payment_id": "nope"})
        self.assert_rejected(
            PaymentNotFoundError,
            lambda: self.service.capture(CaptureRequest("nope", 1, "USD"), "k-c"),
        )


class AmountValidationTests(LedgerTestCase):
    def test_zero_negative_and_non_integer_amounts_rejected(self):
        svc = self.service
        for bad in (0, -1):
            log_request("authorize-bad", f"k-a-{bad}", {"amount": bad})
            self.assert_rejected(
                InvalidAmountError,
                lambda bad=bad: svc.authorize(AuthorizeRequest("p", bad, "USD"), f"k-a-{bad}"),
            )
        for bad in ("usd", "US", "USDX", ""):
            self.assert_rejected(
                InvalidAmountError,
                lambda bad=bad: svc.authorize(AuthorizeRequest("p2", 1, bad), f"k-cur-{bad}"),
            )
        # bool must not sneak through as int 0/1
        self.assert_rejected(
            InvalidAmountError,
            lambda: svc.authorize(AuthorizeRequest("p3", True, "USD"), "k-bool"),
        )


class IdempotencyTests(LedgerTestCase):
    def test_replay_returns_identical_conclusion_without_double_effect(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p8", 1000, "USD"), "k-a")
        first = svc.capture(CaptureRequest("p8", 400, "USD"), "k-c")
        log_outcome(True, first)
        replay = svc.capture(CaptureRequest("p8", 400, "USD"), "k-c")
        log_outcome(True, replay)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.amount, first.amount)
        self.assertEqual(replay.status, first.status)
        self.assertEqual(replay.captured_amount, first.captured_amount)
        payment = svc.get_payment("p8")
        # only one CAPTURE entry exists despite the replay
        captures = [e for e in payment.entries if e.entry_type == EntryType.CAPTURE]
        self.assertEqual(len(captures), 1)

    def test_same_key_different_payload_is_explicitly_rejected(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p9", 1000, "USD"), "k-a")
        svc.capture(CaptureRequest("p9", 400, "USD"), "k-c")
        log_request(
            "capture-same-key-different-body", "k-c", {"payment_id": "p9", "amount": 401}
        )
        err = self.assert_rejected(
            IdempotencyConflictError,
            lambda: svc.capture(CaptureRequest("p9", 401, "USD"), "k-c"),
        )
        self.assertEqual(err.code, "idempotency_conflict")

    def test_business_failure_is_not_persisted_and_key_can_retry(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p10", 1000, "USD"), "k-a")
        # an over-capture must fail and not consume the idempotency key
        self.assert_rejected(
            AmountExceededError,
            lambda: svc.capture(CaptureRequest("p10", 2000, "USD"), "k-c"),
        )
        ok = svc.capture(CaptureRequest("p10", 1000, "USD"), "k-c")
        self.assertEqual(ok.status, PaymentStatus.CAPTURED)

    def test_idempotent_result_and_conflict_survive_restart(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("p11", 1000, "USD"), "k-a")
        svc.capture(CaptureRequest("p11", 250, "USD"), "k-c")
        # Same key with a different body must be rejected and recorded.
        self.assert_rejected(
            IdempotencyConflictError,
            lambda: svc.capture(CaptureRequest("p11", 999, "USD"), "k-c"),
        )

        # Simulate process restart: reopen the same database file.
        svc2 = reopen_service(self.db_path)
        replay = svc2.capture(CaptureRequest("p11", 250, "USD"), "k-c")
        log_outcome(True, replay)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.captured_amount, 250)
        self.assertEqual(
            self.assert_rejected(
                IdempotencyConflictError,
                lambda: svc2.capture(CaptureRequest("p11", 999, "USD"), "k-c"),
            ).code,
            "idempotency_conflict",
        )
        payment = svc2.get_payment("p11")
        self.assertEqual(
            sum(e.amount for e in payment.entries if e.entry_type == EntryType.CAPTURE),
            250,
        )


if __name__ == "__main__":
    unittest.main()
