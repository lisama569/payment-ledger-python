"""状态流转、部分金额边界与可区分失败原因。"""

from __future__ import annotations

import pytest

from app.errors import ErrorCode, LedgerError
from app.models import AuthorizeCommand, CaptureCommand, RefundCommand


def auth(service, key="k-a", amount=1000, ccy="CNY"):
    return service.authorize(AuthorizeCommand(key, amount, ccy))


def test_authorize_and_status_flow(service):
    r = auth(service)
    assert r.payment_status.value == "AUTHORIZED"
    assert r.amount == 1000
    assert r.replayed is False


def test_partial_then_full_capture(service):
    pid = auth(service).payment_id
    r1 = service.capture(CaptureCommand("k-c1", pid, 300, "CNY"))
    assert r1.amount == 300
    assert r1.payment_status.value == "PARTIALLY_CAPTURED"
    assert r1.captured_amount == 300

    # amount=None 表示捕获剩余全部可捕获金额
    r2 = service.capture(CaptureCommand("k-c2", pid, None, "CNY"))
    assert r2.amount == 700
    assert r2.payment_status.value == "CAPTURED"
    assert r2.captured_amount == 1000


def test_capture_boundary_equals_remaining_is_allowed(service):
    pid = auth(service).payment_id
    r = service.capture(CaptureCommand("k-c", pid, 1000, "CNY"))
    assert r.payment_status.value == "CAPTURED"


def test_over_capture_rejected(service):
    pid = auth(service).payment_id
    service.capture(CaptureCommand("k-c1", pid, 600, "CNY"))
    with pytest.raises(LedgerError) as ei:
        service.capture(CaptureCommand("k-c2", pid, 401, "CNY"))
    assert ei.value.code == ErrorCode.OVER_CAPTURE


def test_capture_after_full_capture_rejected(service):
    pid = auth(service).payment_id
    service.capture(CaptureCommand("k-c1", pid, 1000, "CNY"))
    with pytest.raises(LedgerError) as ei:
        service.capture(CaptureCommand("k-c2", pid, 1, "CNY"))
    assert ei.value.code == ErrorCode.INVALID_STATE_TRANSITION


def test_partial_refund_and_full_refund(service):
    pid = auth(service).payment_id
    service.capture(CaptureCommand("k-c", pid, 1000, "CNY"))
    r1 = service.refund(RefundCommand("k-r1", pid, 300, "CNY"))
    assert r1.payment_status.value == "PARTIALLY_REFUNDED"
    r2 = service.refund(RefundCommand("k-r2", pid, None, "CNY"))
    assert r2.amount == 700
    assert r2.payment_status.value == "REFUNDED"


def test_over_refund_rejected(service):
    pid = auth(service).payment_id
    service.capture(CaptureCommand("k-c", pid, 1000, "CNY"))
    service.refund(RefundCommand("k-r1", pid, 600, "CNY"))
    with pytest.raises(LedgerError) as ei:
        service.refund(RefundCommand("k-r2", pid, 401, "CNY"))
    assert ei.value.code == ErrorCode.OVER_REFUND


def test_refund_without_capture_rejected(service):
    pid = auth(service).payment_id
    with pytest.raises(LedgerError) as ei:
        service.refund(RefundCommand("k-r", pid, None, "CNY"))
    assert ei.value.code == ErrorCode.INVALID_STATE_TRANSITION


def test_no_capture_after_refund_started(service):
    pid = auth(service).payment_id
    service.capture(CaptureCommand("k-c1", pid, 500, "CNY"))
    service.refund(RefundCommand("k-r1", pid, 100, "CNY"))
    # 开始退款后，即使还有可捕获余额也不允许再捕获
    with pytest.raises(LedgerError) as ei:
        service.capture(CaptureCommand("k-c2", pid, 1, "CNY"))
    assert ei.value.code == ErrorCode.INVALID_STATE_TRANSITION


def test_currency_mismatch_on_capture_and_refund(service):
    pid = auth(service).payment_id
    with pytest.raises(LedgerError) as ei:
        service.capture(CaptureCommand("k-c", pid, 100, "USD"))
    assert ei.value.code == ErrorCode.CURRENCY_MISMATCH

    service.capture(CaptureCommand("k-c2", pid, 100, "CNY"))
    with pytest.raises(LedgerError) as ei:
        service.refund(RefundCommand("k-r", pid, 1, "EUR"))
    assert ei.value.code == ErrorCode.CURRENCY_MISMATCH


def test_payment_not_found(service):
    with pytest.raises(LedgerError) as ei:
        service.capture(CaptureCommand("k-c", "pay_nope", 1, "CNY"))
    assert ei.value.code == ErrorCode.PAYMENT_NOT_FOUND


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_non_positive_amount_rejected(service, bad):
    with pytest.raises(LedgerError) as ei:
        service.authorize(AuthorizeCommand("k-bad", bad, "CNY"))
    assert ei.value.code == ErrorCode.INVALID_AMOUNT


def test_float_and_bool_amount_rejected_at_service_layer(service):
    with pytest.raises(LedgerError) as ei:
        service.authorize(AuthorizeCommand("k-f", 1.5, "CNY"))  # type: ignore[arg-type]
    assert ei.value.code == ErrorCode.INVALID_AMOUNT
    with pytest.raises(LedgerError) as ei:
        service.authorize(AuthorizeCommand("k-b", True, "CNY"))  # type: ignore[arg-type]
    assert ei.value.code == ErrorCode.INVALID_AMOUNT


def test_append_only_ledger_entries_never_change(service, store):
    pid = auth(service).payment_id
    t1 = service.capture(CaptureCommand("k-c1", pid, 300, "CNY"))
    service.capture(CaptureCommand("k-c2", pid, 300, "CNY"))
    service.refund(RefundCommand("k-r1", pid, 100, "CNY"))
    state = store.load()
    # 每笔操作各产生一条流水，AUTHORIZE 也在其中，顺序不可变
    types = [t.txn_type.value for t in state.transactions]
    assert types == ["AUTHORIZE", "CAPTURE", "CAPTURE", "REFUND"]
    first = state.transactions[1]
    assert first.txn_id == t1.txn_id and first.amount == 300
