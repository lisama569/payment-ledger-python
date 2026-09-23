"""日终对账：金额不一致 / 缺失入账 / 重复入账等差异识别与只读保证。"""

from __future__ import annotations

import json

from app.models import (
    AuthorizeCommand,
    CaptureCommand,
    DiscrepancyType,
    RefundCommand,
    SettlementEntry,
)
from app.reconciliation import reconcile


def _build_ledger(service):
    """造 4 笔会结算的流水（3 capture + 1 refund），返回其 txn_id。"""
    pid = service.authorize(
        AuthorizeCommand("k-a", 1000, "CNY")).payment_id
    cap_ok = service.capture(
        CaptureCommand("k-c1", pid, 200, "CNY")).txn_id
    cap_amt = service.capture(
        CaptureCommand("k-c2", pid, 300, "CNY")).txn_id
    cap_missing = service.capture(
        CaptureCommand("k-c3", pid, 100, "CNY")).txn_id
    cap_dup = service.capture(
        CaptureCommand("k-c4", pid, 50, "CNY")).txn_id
    refund_ok = service.refund(
        RefundCommand("k-r1", pid, 50, "CNY")).txn_id
    return {
        "cap_ok": cap_ok,
        "cap_amt": cap_amt,
        "cap_missing": cap_missing,
        "cap_dup": cap_dup,
        "refund_ok": refund_ok,
    }


def _by_type(report):
    grouped: dict[str, list] = {}
    for item in report.items:
        grouped.setdefault(item.discrepancy.value, []).append(item)
    return grouped


def test_reconcile_matched_baseline(service, store):
    ids = _build_ledger(service)
    entries = [
        SettlementEntry(ids["cap_ok"], 200, "CNY"),
        SettlementEntry(ids["cap_amt"], 300, "CNY"),
        SettlementEntry(ids["cap_missing"], 100, "CNY"),
        SettlementEntry(ids["cap_dup"], 50, "CNY"),
        SettlementEntry(ids["refund_ok"], 50, "CNY"),
    ]
    report = reconcile(store, entries)
    assert report.matched_count == 5
    assert report.items == []


def test_amount_mismatch(service, store):
    ids = _build_ledger(service)
    report = reconcile(store, [SettlementEntry(ids["cap_amt"], 299, "CNY")])
    grouped = _by_type(report)
    assert "AMOUNT_MISMATCH" in grouped
    item = grouped["AMOUNT_MISMATCH"][0]
    assert item.txn_id == ids["cap_amt"]
    assert item.ledger_amount == 300 and item.settlement_amount == 299
    # 其余内部流水缺失入账
    assert len(grouped["MISSING_SETTLEMENT"]) == 4


def test_missing_settlement(service, store):
    ids = _build_ledger(service)
    # 只结算其中两笔 -> 其余应判缺失
    report = reconcile(store, [
        SettlementEntry(ids["cap_ok"], 200, "CNY"),
        SettlementEntry(ids["refund_ok"], 50, "CNY"),
    ])
    grouped = _by_type(report)
    missing_ids = {i.txn_id for i in grouped["MISSING_SETTLEMENT"]}
    assert missing_ids == {
        ids["cap_amt"], ids["cap_missing"], ids["cap_dup"],
    }


def test_duplicate_settlement(service, store):
    ids = _build_ledger(service)
    report = reconcile(store, [
        SettlementEntry(ids["cap_dup"], 50, "CNY"),
        SettlementEntry(ids["cap_dup"], 50, "CNY"),
    ])
    grouped = _by_type(report)
    dups = grouped["DUPLICATE_SETTLEMENT"]
    assert len(dups) == 1
    assert dups[0].txn_id == ids["cap_dup"]
    assert dups[0].settlement_amount == 50
    # 主笔仍计为匹配
    assert report.matched_count == 1


def test_orphan_settlement(service, store):
    _build_ledger(service)
    report = reconcile(store, [
        SettlementEntry("txn_ghost", 999, "CNY"),
    ])
    grouped = _by_type(report)
    orphans = grouped["ORPHAN_SETTLEMENT"]
    assert len(orphans) == 1
    assert orphans[0].txn_id == "txn_ghost"
    assert orphans[0].settlement_amount == 999


def test_settlement_currency_mismatch(service, store):
    ids = _build_ledger(service)
    report = reconcile(store, [
        SettlementEntry(ids["cap_ok"], 200, "USD"),
    ])
    grouped = _by_type(report)
    items = grouped["SETTLEMENT_CURRENCY_MISMATCH"]
    assert len(items) == 1
    assert items[0].ledger_currency == "CNY"
    assert items[0].settlement_currency == "USD"


def test_all_three_required_discrepancies_together(service, store):
    """需求点名的三类：金额不一致 / 缺失入账 / 重复入账，同一份输入同时出现。"""
    ids = _build_ledger(service)
    entries = [
        SettlementEntry(ids["cap_ok"], 200, "CNY"),      # 匹配
        SettlementEntry(ids["cap_amt"], 999, "CNY"),     # 金额不一致
        # cap_missing 缺失
        SettlementEntry(ids["cap_dup"], 50, "CNY"),
        SettlementEntry(ids["cap_dup"], 50, "CNY"),      # 重复入账
        SettlementEntry(ids["refund_ok"], 50, "CNY"),    # 匹配
    ]
    report = reconcile(store, entries)
    kinds = {i.discrepancy for i in report.items}
    assert DiscrepancyType.AMOUNT_MISMATCH in kinds
    assert DiscrepancyType.MISSING_SETTLEMENT in kinds
    assert DiscrepancyType.DUPLICATE_SETTLEMENT in kinds
    assert report.matched_count == 3


def test_reconcile_is_strictly_read_only(service, store):
    ids = _build_ledger(service)
    before = json.dumps(store.load().to_dict(), sort_keys=True)
    file_text = store.path.read_text(encoding="utf-8")

    reconcile(store, [
        SettlementEntry(ids["cap_amt"], 1, "CNY"),
        SettlementEntry(ids["cap_dup"], 50, "CNY"),
        SettlementEntry(ids["cap_dup"], 50, "CNY"),
        SettlementEntry("txn_ghost", 7, "CNY"),
    ])

    after = json.dumps(store.load().to_dict(), sort_keys=True)
    assert after == before
    assert store.path.read_text(encoding="utf-8") == file_text


def test_reconcile_is_deterministic(service, store):
    ids = _build_ledger(service)
    entries = [
        SettlementEntry(ids["cap_dup"], 50, "CNY"),
        SettlementEntry(ids["cap_dup"], 50, "CNY"),
        SettlementEntry(ids["cap_amt"], 1, "CNY"),
    ]
    r1 = reconcile(store, entries).to_dict()
    # 打乱输入顺序再跑一次
    r2 = reconcile(store, list(reversed(entries))).to_dict()
    assert r1 == r2


def test_authorize_entries_excluded_from_reconciliation(service, store):
    service.authorize(AuthorizeCommand("k-only-auth", 100, "CNY"))
    report = reconcile(store, [])
    assert report.ledger_txn_count == 0
    assert report.items == []
