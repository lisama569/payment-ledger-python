"""HTTP integration tests via FastAPI TestClient."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from payment_ledger.api import create_app
from payment_ledger.models import EntryType

from .support import log, setup_logging


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        setup_logging()
        self.tmp = tempfile.NamedTemporaryFile(prefix="api_", suffix=".db", delete=False)
        self.tmp.close()
        Path(self.tmp.name).unlink()
        self.client = TestClient(create_app(self.tmp.name))

    def tearDown(self) -> None:
        Path(self.tmp.name).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(self.tmp.name + suffix).unlink(missing_ok=True)

    def test_full_http_lifecycle_with_error_codes(self):
        c = self.client
        log.info("HTTP authorize idempotency_key=k-a request=%s", {"payment_id": "p1", "amount": 10000, "currency": "USD"})
        r = c.post(
            "/v1/payments/authorize",
            json={"payment_id": "p1", "amount": 10000, "currency": "USD"},
            headers={"Idempotency-Key": "k-a"},
        )
        self.assertEqual(r.status_code, 200)

        # replay
        r = c.post(
            "/v1/payments/authorize",
            json={"payment_id": "p1", "amount": 10000, "currency": "USD"},
            headers={"Idempotency-Key": "k-a"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["replayed"])

        # conflict
        r = c.post(
            "/v1/payments/authorize",
            json={"payment_id": "p1", "amount": 9999, "currency": "USD"},
            headers={"Idempotency-Key": "k-a"},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "idempotency_conflict")

        # over capture -> 422 amount_exceeded
        r = c.post(
            "/v1/payments/p1/capture",
            json={"amount": 20000, "currency": "USD"},
            headers={"Idempotency-Key": "k-c-bad"},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["code"], "amount_exceeded")

        # currency mismatch -> 422 currency_mismatch
        r = c.post(
            "/v1/payments/p1/capture",
            json={"amount": 10, "currency": "EUR"},
            headers={"Idempotency-Key": "k-c-eur"},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["code"], "currency_mismatch")

        # float amount -> 422 pydantic validation
        r = c.post(
            "/v1/payments/p1/capture",
            json={"amount": 10.5, "currency": "USD"},
            headers={"Idempotency-Key": "k-c-float"},
        )
        self.assertEqual(r.status_code, 422)

        # legal partial capture + refund
        r = c.post(
            "/v1/payments/p1/capture",
            json={"amount": 6000, "currency": "USD"},
            headers={"Idempotency-Key": "k-c1"},
        )
        self.assertEqual(r.status_code, 200)
        r = c.post(
            "/v1/payments/p1/refund",
            json={"amount": 2500, "currency": "USD"},
            headers={"Idempotency-Key": "k-r1"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "PARTIALLY_REFUNDED")

        r = c.get("/v1/payments/p1")
        body = r.json()
        self.assertEqual(body["captured_amount"], 6000)
        self.assertEqual(body["refunded_amount"], 2500)
        log.info("HTTP payment view=%s", body)

        # reconcile endpoint reports mismatches and is read-only
        rec = c.post(
            "/v1/reconcile",
            json={
                "statement": [
                    {"external_ref": "E1", "payment_id": "p1", "entry_type": "CAPTURE", "amount": 5999, "currency": "USD"},
                    {"external_ref": "E2", "payment_id": "p1", "entry_type": "REFUND", "amount": 2500, "currency": "USD"},
                    {"external_ref": "E3", "payment_id": "zz", "entry_type": "CAPTURE", "amount": 10, "currency": "USD"},
                ]
            },
        )
        self.assertEqual(rec.status_code, 200)
        codes = {d["discrepancy"] for d in rec.json()["discrepancies"]}
        self.assertEqual(codes, {"AMOUNT_MISMATCH", "MISSING_IN_LEDGER"})

        untouched = c.get("/v1/payments/p1").json()
        self.assertEqual(untouched["captured_amount"], 6000)
        self.assertEqual(untouched["refunded_amount"], 2500)

    def test_missing_idempotency_header_is_rejected(self):
        r = self.client.post(
            "/v1/payments/authorize",
            json={"payment_id": "p2", "amount": 1, "currency": "USD"},
        )
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
