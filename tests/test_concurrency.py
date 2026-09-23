"""Concurrent duplicate submission tests.

Guarantee under test: no matter how many threads fire the same
idempotency key at the same time, exactly one fund movement lands and
every caller observes the same first conclusion.
"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

from payment_ledger.errors import (
    AmountExceededError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    LedgerError,
)
from payment_ledger.models import AuthorizeRequest, CaptureRequest, EntryType

from .support import LedgerTestCase, log, log_outcome, log_request


class ConcurrentDuplicateTests(LedgerTestCase):
    def test_concurrent_same_key_capture_applies_once(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("pc", 10000, "USD"), "k-auth")
        threads = 16
        log_request(
            "concurrent-capture",
            "k-cap",
            {"payment_id": "pc", "amount": 4000, "threads": threads},
        )

        results: list = []
        errors: list = []

        def fire():
            try:
                r = svc.capture(CaptureRequest("pc", 4000, "USD"), "k-cap")
                results.append(r)
            except LedgerError as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = [pool.submit(fire) for _ in range(threads)]
            for f in as_completed(futures):
                f.result()

        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(len(errors), 0, f"unexpected errors: {errors}")
        self.assertTrue(all(r.amount == 4000 for r in results))
        self.assertTrue(all(r.captured_amount == 4000 for r in results))
        winners = [r for r in results if not r.replayed]
        replayed = [r for r in results if r.replayed]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(replayed), threads - 1)

        # Ledger ground truth: exactly one capture entry.
        payment = svc.get_payment("pc")
        captures = [e for e in payment.entries if e.entry_type == EntryType.CAPTURE]
        self.assertEqual(len(captures), 1)
        self.assertEqual(payment.captured_amount, 4000)
        log_request(
            "concurrent-capture-verified",
            "k-cap",
            {"winners": len(winners), "replays": len(replayed), "ledger_captures": len(captures)},
        )
        log_request(
            "concurrent-capture-verified",
            "k-cap",
            {"winners": len(winners), "replays": len(replayed), "ledger_captures": len(captures)},
        )

    def test_concurrent_authorize_same_payment_same_key(self):
        threads = 12
        results, errors = [], []

        def fire():
            # Fresh service per thread is not needed; shared service.
            try:
                results.append(
                    self.service.authorize(
                        AuthorizeRequest("pa", 5000, "USD"), "k-auth-pa"
                    )
                )
            except LedgerError as exc:
                errors.append(exc)

        log_request("concurrent-authorize", "k-auth-pa", {"threads": threads})
        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(as_completed([pool.submit(fire) for _ in range(threads)]))

        self.assertEqual(len(errors), 0)
        self.assertEqual(len([r for r in results if not r.replayed]), 1)
        self.assertEqual(len([r for r in results if r.replayed]), threads - 1)
        payment = self.service.get_payment("pa")
        self.assertEqual(payment.authorized_amount, 5000)
        self.assertEqual(len(payment.entries), 1)

    def test_concurrent_conflicting_payloads_one_wins_rest_rejected(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("px", 10000, "USD"), "k-auth")
        threads = 10

        # Half of the threads use the original body (4000), half a
        # different body (4001). Exactly one 4000 must land; the
        # divergent callers get idempotency_conflict and see no effect.
        outcomes = {"ok": [], "conflict": [], "other": []}
        lock = threading.Lock()

        def fire(i):
            amount = 4000 if i % 2 == 0 else 4001
            try:
                r = svc.capture(CaptureRequest("px", amount, "USD"), "k-shared")
                with lock:
                    outcomes["ok"].append(r)
            except IdempotencyConflictError as exc:
                log_outcome(False, error=exc)
                with lock:
                    outcomes["conflict"].append(exc)
            except LedgerError as exc:
                with lock:
                    outcomes["other"].append(exc)

        log_request("concurrent-conflict", "k-shared", {"amounts": [4000, 4001]})
        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(as_completed([pool.submit(fire, i) for i in range(threads)]))

        self.assertEqual(len(outcomes["other"]), 0)
        self.assertGreaterEqual(len(outcomes["ok"]), 1)
        # All accepted callers saw the identical 4000 result.
        for r in outcomes["ok"]:
            self.assertEqual(r.amount, 4000)
            self.assertEqual(r.captured_amount, 4000)
        self.assertEqual(len([r for r in outcomes["ok"] if not r.replayed]), 1)
        self.assertEqual(len(outcomes["conflict"]), threads - len(outcomes["ok"]))

        payment = svc.get_payment("px")
        self.assertEqual(payment.captured_amount, 4000)
        self.assertEqual(
            len([e for e in payment.entries if e.entry_type == EntryType.CAPTURE]), 1
        )

    def test_distinct_keys_apply_independently_under_concurrency(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("pd", 10000, "USD"), "k-auth")
        n = 8

        def fire(i):
            return svc.capture(CaptureRequest("pd", 1000, "USD"), f"k-cap-{i}")

        with ThreadPoolExecutor(max_workers=n) as pool:
            results = [f.result() for f in as_completed([pool.submit(fire, i) for i in range(n)])]
        self.assertEqual(len(results), n)
        self.assertTrue(all(not r.replayed for r in results))
        payment = svc.get_payment("pd")
        self.assertEqual(payment.captured_amount, 8000)
        self.assertEqual(
            len([e for e in payment.entries if e.entry_type == EntryType.CAPTURE]), n
        )

    def test_concurrent_over_captures_never_move_funds_beyond_authorized(self):
        svc = self.service
        svc.authorize(AuthorizeRequest("pe", 1000, "USD"), "k-auth")
        n = 6

        # Each thread uses its own key but asks for the full 1000:
        # exactly one can succeed; losers are rejected either because
        # their request exceeds the remaining capturable amount, or
        # because nothing is capturable after the winner commits.
        outcomes = {"ok": [], "rejected": [], "other": []}
        lock = threading.Lock()

        def fire(i):
            try:
                r = svc.capture(CaptureRequest("pe", 1000, "USD"), f"k-cap-{i}")
                with lock:
                    outcomes["ok"].append(r)
            except (AmountExceededError, InvalidStateTransitionError) as exc:
                with lock:
                    outcomes["rejected"].append(exc)
            except LedgerError as exc:
                with lock:
                    outcomes["other"].append(exc)

        with ThreadPoolExecutor(max_workers=n) as pool:
            list(as_completed([pool.submit(fire, i) for i in range(n)]))

        self.assertEqual(len(outcomes["ok"]), 1)
        self.assertEqual(len(outcomes["other"]), 0)
        self.assertEqual(len(outcomes["rejected"]), n - 1)
        log_request(
            "concurrent-over-capture-verified",
            "k-cap-{i}",
            {"rejected_codes": sorted({e.code for e in outcomes["rejected"]})},
        )
        self.assertEqual(svc.get_payment("pe").captured_amount, 1000)


if __name__ == "__main__":
    unittest.main()
