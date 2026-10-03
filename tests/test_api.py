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


class ApiV2Tests(unittest.TestCase):
    """HTTP surface for external_ref and recorded reconciliation runs."""

    def setUp(self) -> None:
        setup_logging()
        self.tmp = tempfile.NamedTemporaryFile(prefix="api_v2_", suffix=".db", delete=False)
        self.tmp.close()
        Path(self.tmp.name).unlink()
        self.client = TestClient(create_app(self.tmp.name))

    def tearDown(self) -> None:
        Path(self.tmp.name).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(self.tmp.name + suffix).unlink(missing_ok=True)

    def _authorize_and_capture(self, pid: str, ref: str | None, key_prefix: str) -> None:
        c = self.client
        c.post(
            "/v1/payments/authorize",
            json={"payment_id": pid, "amount": 10000, "currency": "USD"},
            headers={"Idempotency-Key": f"{key_prefix}-a"},
        )
        body = {"amount": 4000, "currency": "USD"}
        if ref is not None:
            body["external_ref"] = ref
        log.info("HTTP capture idempotency_key=%s-c request=%s", key_prefix, body)
        r = c.post(
            f"/v1/payments/{pid}/capture",
            json=body,
            headers={"Idempotency-Key": f"{key_prefix}-c"},
        )
        self.assertEqual(r.status_code, 200)

    def test_external_ref_http_flow_and_duplicate_rejection(self):
        c = self.client
        self._authorize_and_capture("p1", "S-1", "k1")
        # The reference is visible on the ledger entry.
        entries = c.get("/v1/payments/p1").json()["entries"]
        capture = [e for e in entries if e["entry_type"] == "CAPTURE"][0]
        self.assertEqual(capture["external_ref"], "S-1")

        # Reusing the same reference on another payment -> 409.
        self._authorize_and_capture("p2", None, "k2")
        r = c.post(
            "/v1/payments/p2/capture",
            json={"amount": 100, "currency": "USD", "external_ref": "S-1"},
            headers={"Idempotency-Key": "k2-c2"},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "duplicate_external_ref")
        log.info("HTTP duplicate external_ref rejected code=duplicate_external_ref")

    def test_reconcile_record_and_diff_over_http(self):
        c = self.client
        self._authorize_and_capture("p1", "S-1", "k1")
        statement = [
            {"external_ref": "S-1", "payment_id": "p1", "entry_type": "CAPTURE",
             "amount": 4000, "currency": "USD"},
            {"external_ref": "S-9", "payment_id": "p9", "entry_type": "CAPTURE",
             "amount": 4000, "currency": "USD"},
        ]
        r = c.post("/v1/reconcile", json={"statement": statement, "record": True})
        self.assertEqual(r.status_code, 200)
        run1 = r.json()
        self.assertEqual(len(run1["matched"]), 1)
        self.assertEqual(
            [d["discrepancy"] for d in run1["discrepancies"]], ["MISSING_IN_LEDGER"]
        )

        # Second run: S-9 settled with a matching booking, nothing one-sided.
        self._authorize_and_capture("p9", "S-9", "k9")
        r = c.post("/v1/reconcile", json={"statement": statement, "record": True})
        run2 = r.json()
        self.assertEqual(run2["discrepancies"], [])

        r = c.get("/v1/reconcile/runs")
        self.assertEqual(len(r.json()["runs"]), 2)
        r = c.get(f"/v1/reconcile/runs/{run1['run_id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["run_id"], run1["run_id"])

        r = c.get("/v1/reconcile/diff", params={"older": run1["run_id"], "newer": run2["run_id"]})
        self.assertEqual(r.status_code, 200)
        diff = r.json()
        self.assertEqual(diff["new_discrepancies"], [])
        self.assertEqual(diff["persisting_discrepancies"], [])
        self.assertEqual(
            [d["external_ref"] for d in diff["resolved_discrepancies"]], ["S-9"]
        )
        log.info("HTTP reconcile record+diff verified runs=%s", [run1["run_id"], run2["run_id"]])
