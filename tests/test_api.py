"""HTTP 端到端：状态码、错误原因码、幂等重放与对账接口。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import create_app


def make_client(tmp_path):
    path = str(tmp_path / "ledger.json")
    app = create_app(__import__("app.store", fromlist=["JsonStore"]).JsonStore(path))
    return TestClient(app)


def test_full_lifecycle_via_http(tmp_path):
    client = make_client(tmp_path)

    r = client.post("/v1/payments/authorize", json={
        "idem_key": "auth-1", "amount": 1000, "currency": "CNY"})
    assert r.status_code == 200
    pid = r.json()["result"]["payment_id"]

    r = client.post("/v1/payments/capture", json={
        "idem_key": "cap-1", "payment_id": pid,
        "amount": 400, "currency": "CNY"})
    assert r.status_code == 200
    assert r.json()["result"]["payment_status"] == "PARTIALLY_CAPTURED"

    # 重放：结论一致且 replayed=true
    r2 = client.post("/v1/payments/capture", json={
        "idem_key": "cap-1", "payment_id": pid,
        "amount": 400, "currency": "CNY"})
    assert r2.json()["result"]["replayed"] is True
    assert r2.json()["result"]["txn_id"] == r.json()["result"]["txn_id"]

    r = client.post("/v1/payments/refund", json={
        "idem_key": "ref-1", "payment_id": pid,
        "amount": None, "currency": "CNY"})
    assert r.json()["result"]["amount"] == 400
    assert r.json()["result"]["payment_status"] == "REFUNDED"

    g = client.get(f"/v1/payments/{pid}")
    assert g.json()["result"]["status"] == "REFUNDED"


def test_error_codes_and_http_status(tmp_path):
    client = make_client(tmp_path)
    pid = client.post("/v1/payments/authorize", json={
        "idem_key": "a", "amount": 100, "currency": "CNY"}) .json()[
        "result"]["payment_id"]

    # 超额捕获 -> 422 OVER_CAPTURE
    r = client.post("/v1/payments/capture", json={
        "idem_key": "c", "payment_id": pid, "amount": 101,
        "currency": "CNY"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OVER_CAPTURE"

    # 币种不一致
    r = client.post("/v1/payments/capture", json={
        "idem_key": "c2", "payment_id": pid, "amount": 1,
        "currency": "USD"})
    assert r.json()["error"]["code"] == "CURRENCY_MISMATCH"

    # 不存在 -> 404
    r = client.post("/v1/payments/capture", json={
        "idem_key": "c3", "payment_id": "pay_x", "amount": 1,
        "currency": "CNY"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PAYMENT_NOT_FOUND"

    # 幂等冲突 -> 409
    client.post("/v1/payments/capture", json={
        "idem_key": "c4", "payment_id": pid, "amount": 10,
        "currency": "CNY"})
    r = client.post("/v1/payments/capture", json={
        "idem_key": "c4", "payment_id": pid, "amount": 11,
        "currency": "CNY"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_float_amount_rejected_by_schema(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/v1/payments/authorize", json={
        "idem_key": "f", "amount": 1.5, "currency": "CNY"})
    assert r.status_code == 422  # pydantic strict int


def test_reconcile_endpoint(tmp_path):
    client = make_client(tmp_path)
    pid = client.post("/v1/payments/authorize", json={
        "idem_key": "a", "amount": 1000, "currency": "CNY"}).json()[
        "result"]["payment_id"]
    good = client.post("/v1/payments/capture", json={
        "idem_key": "c1", "payment_id": pid, "amount": 200,
        "currency": "CNY"}).json()["result"]["txn_id"]
    bad = client.post("/v1/payments/capture", json={
        "idem_key": "c2", "payment_id": pid, "amount": 300,
        "currency": "CNY"}).json()["result"]["txn_id"]

    r = client.post("/v1/reconcile", json={"settlements": [
        {"txn_id": good, "amount": 200, "currency": "CNY"},
        {"txn_id": bad, "amount": 250, "currency": "CNY"},   # 金额不一致
        {"txn_id": "txn_ghost", "amount": 1, "currency": "CNY"},
    ]})
    assert r.status_code == 200
    report = r.json()["report"]
    kinds = {d["discrepancy"] for d in report["discrepancies"]}
    assert "AMOUNT_MISMATCH" in kinds
    assert "ORPHAN_SETTLEMENT" in kinds
    assert report["matched_count"] == 1


def test_health(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/health").json() == {"status": "ok"}
