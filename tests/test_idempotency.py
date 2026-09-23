"""幂等提交：并发去重、重放一致、同键异参冲突、重启复现。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import ErrorCode, LedgerError
from app.models import (
    AuthorizeCommand,
    CaptureCommand,
    RefundCommand,
    Transaction,
    TxnType,
)
from app.service import PaymentService
from app.store import JsonStore


def test_concurrent_duplicate_authorize_single_effect(store, caplog):
    caplog.set_level(logging.INFO)
    service = PaymentService(store)

    def submit(_):
        return service.authorize(AuthorizeCommand("idem-concurrent", 500, "CNY"))

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(submit, range(32)))

    # 只有一笔真实资金影响
    state = store.load()
    auth_txns = [t for t in state.transactions
                 if t.txn_type == TxnType.AUTHORIZE]
    assert len(auth_txns) == 1
    assert len(state.payments) == 1

    txn_ids = {r.txn_id for r in results}
    assert txn_ids == {auth_txns[0].txn_id}
    firsts = [r for r in results if not r.replayed]
    replays = [r for r in results if r.replayed]
    assert len(firsts) == 1 and len(replays) == 31
    # 所有结论金额、支付单完全一致
    assert all(r.amount == 500 for r in results)
    assert len({r.payment_id for r in results}) == 1

    # 日志必须包含幂等键与重放判定
    joined = "\n".join(r.message for r in caplog.records)
    assert "idem-concurrent" in joined
    assert "重放" in joined


def test_concurrent_duplicate_capture_single_effect(store):
    service = PaymentService(store)
    pid = service.authorize(AuthorizeCommand("k-a", 1000, "CNY")).payment_id

    def submit(_):
        return service.capture(
            CaptureCommand("idem-cap", pid, 400, "CNY"))

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(submit, range(24)))

    payment = service.get_payment(pid)
    assert payment.captured_amount == 400  # 绝不能被重复累加
    captures = [t for t in store.load().transactions
                if t.txn_type == TxnType.CAPTURE]
    assert len(captures) == 1
    assert sum(1 for r in results if r.replayed) == 23


def test_replay_returns_identical_conclusion(service, store):
    pid = service.authorize(AuthorizeCommand("k-a", 1000, "CNY")).payment_id
    first = service.capture(CaptureCommand("k-c", pid, 250, "CNY"))
    second = service.capture(CaptureCommand("k-c", pid, 250, "CNY"))
    assert second.replayed is True
    assert second.txn_id == first.txn_id
    assert second.amount == first.amount
    assert second.payment_id == first.payment_id


def test_same_key_different_payload_conflict(service, caplog):
    pid = service.authorize(AuthorizeCommand("k-a", 1000, "CNY")).payment_id
    service.capture(CaptureCommand("k-c", pid, 200, "CNY"))
    # 同键、不同金额 -> 明确拒绝
    with pytest.raises(LedgerError) as ei:
        service.capture(CaptureCommand("k-c", pid, 201, "CNY"))
    assert ei.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    # 同键、不同币种也视为不同请求
    with pytest.raises(LedgerError) as ei:
        service.capture(CaptureCommand("k-c", pid, 200, "USD"))
    assert ei.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    # 冲突不产生任何资金影响
    assert service.get_payment(pid).captured_amount == 200
    assert any("幂等冲突" in r.message for r in caplog.records)


def test_none_full_amount_semantics_is_part_of_fingerprint(service):
    pid = service.authorize(AuthorizeCommand("k-a", 1000, "CNY")).payment_id
    service.capture(CaptureCommand("k-c", pid, 300, "CNY"))
    # 同键但从“部分捕获 300”改成“全额捕获” -> 冲突
    with pytest.raises(LedgerError) as ei:
        service.capture(CaptureCommand("k-c", pid, None, "CNY"))
    assert ei.value.code == ErrorCode.IDEMPOTENCY_CONFLICT


def test_business_failure_replays_same_failure(service, store):
    pid = service.authorize(AuthorizeCommand("k-a", 100, "CNY")).payment_id
    # 首次：超额捕获，业务失败但幂等结论落地
    with pytest.raises(LedgerError) as ei:
        service.capture(CaptureCommand("k-bad", pid, 101, "CNY"))
    assert ei.value.code == ErrorCode.OVER_CAPTURE
    # 重放：即使把金额改“合法”，也因指纹不同被判冲突
    with pytest.raises(LedgerError) as ei:
        service.capture(CaptureCommand("k-bad", pid, 50, "CNY"))
    assert ei.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    # 相同内容重放：复现首次失败原因
    with pytest.raises(LedgerError) as ei:
        service.capture(CaptureCommand("k-bad", pid, 101, "CNY"))
    assert ei.value.code == ErrorCode.OVER_CAPTURE
    # 失败的操作不得留下资金流水
    assert all(t.txn_type != TxnType.CAPTURE
               for t in store.load().transactions)


def test_conclusions_survive_restart(store):
    service = PaymentService(store)
    pid = service.authorize(AuthorizeCommand("k-a", 1000, "CNY")).payment_id
    first = service.capture(CaptureCommand("k-c", pid, 300, "CNY"))
    with pytest.raises(LedgerError):
        service.capture(CaptureCommand("k-c", pid, 999, "CNY"))

    # 模拟服务重启：新 service + 丢弃内存缓存重新读盘
    restarted = PaymentService(store)
    store.reload()

    replayed = restarted.capture(CaptureCommand("k-c", pid, 300, "CNY"))
    assert replayed.replayed is True
    assert replayed.txn_id == first.txn_id

    with pytest.raises(LedgerError) as ei:
        restarted.capture(CaptureCommand("k-c", pid, 999, "CNY"))
    assert ei.value.code == ErrorCode.IDEMPOTENCY_CONFLICT


def test_logs_contain_key_params_decision_and_reason(service, caplog):
    caplog.set_level(logging.INFO)
    service.authorize(AuthorizeCommand("log-key", 10, "CNY"))
    with pytest.raises(LedgerError):
        service.authorize(AuthorizeCommand("log-key", 11, "CNY"))

    messages = [r.message for r in caplog.records]
    joined = "\n".join(messages)
    assert "log-key" in joined
    assert "params=" in joined          # 请求参数
    assert "判定=" in joined            # 判定结果
    assert "IDEMPOTENCY_CONFLICT" in joined  # 失败原因
