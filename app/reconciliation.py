"""日终对账：只读内部资金流水，与外部结算流水比对。

严格只读
--------
本模块只调用 ``store.load()`` 获取状态快照，不调用 ``save``，
不 append/modify/delete 任何流水或支付单。

对账范围
--------
仅对真正产生资金出入账的 CAPTURE / REFUND 流水进行对账；
AUTHORIZE 只是额度冻结，不产生结算。外部流水平方以 ``txn_id`` 关联。

差异分类（可区分）
------------------
- MISSING_SETTLEMENT      内部有资金流水，外部完全无对应入账
- AMOUNT_MISMATCH         同一笔金额不一致
- SETTLEMENT_CURRENCY_MISMATCH 同一笔币种不一致
- DUPLICATE_SETTLEMENT    外部同一 txn_id 出现多笔（重复入账）
- ORPHAN_SETTLEMENT       外部流水在内部找不到对应资金流水
"""

from __future__ import annotations

import logging
from collections import defaultdict

from app.models import (
    DiscrepancyType as DType,
    ReconcileItem,
    ReconcileReport,
    SettlementEntry,
    Transaction,
    TxnType,
)
from app.store import JsonStore

logger = logging.getLogger("payment_ledger.reconciliation")

_SETTLED_TYPES = (TxnType.CAPTURE, TxnType.REFUND)


def _make_item(
    txn_id: str,
    discrepancy: DType,
    ledger: Transaction | None,
    settlement: SettlementEntry | None,
    detail: str,
) -> ReconcileItem:
    return ReconcileItem(
        txn_id=txn_id,
        discrepancy=discrepancy,
        ledger_amount=ledger.amount if ledger else None,
        settlement_amount=settlement.amount if settlement else None,
        ledger_currency=ledger.currency if ledger else None,
        settlement_currency=settlement.currency if settlement else None,
        detail=detail,
    )


def reconcile(
    store: JsonStore, settlements: list[SettlementEntry]
) -> ReconcileReport:
    # 只读快照：绝不写回
    state = store.load()
    ledger_by_id: dict[str, Transaction] = {
        t.txn_id: t for t in state.transactions if t.txn_type in _SETTLED_TYPES
    }
    settlement_groups: dict[str, list[SettlementEntry]] = defaultdict(list)
    for entry in settlements:
        settlement_groups[entry.txn_id].append(entry)

    items: list[ReconcileItem] = []
    matched_count = 0

    # 1) 以内部资金流水为基准
    for txn_id, ledger in ledger_by_id.items():
        group = settlement_groups.get(txn_id, [])
        if not group:
            logger.info(
                "对账 txn_id=%s type=%s 内部金额=%s/%s 外部=无 判定=%s",
                txn_id, ledger.txn_type.value, ledger.amount,
                ledger.currency, DType.MISSING_SETTLEMENT.value,
            )
            items.append(_make_item(
                txn_id, DType.MISSING_SETTLEMENT, ledger, None,
                "内部存在资金流水但外部无结算入账",
            ))
            continue

        if len(group) > 1:
            # 确定性排序，保证可复现：金额、币种升序后取第一笔做核对
            ordered = sorted(group, key=lambda e: (e.amount, e.currency))
            primary, rest = ordered[0], ordered[1:]
            primary_item, primary_match = _compare_one(ledger, primary)
            if primary_item is not None:
                items.append(primary_item)
            for dup in rest:
                logger.info(
                    "对账 txn_id=%s 外部重复金额=%s/%s 判定=%s",
                    txn_id, dup.amount, dup.currency,
                    DType.DUPLICATE_SETTLEMENT.value,
                )
                items.append(_make_item(
                    txn_id, DType.DUPLICATE_SETTLEMENT, ledger, dup,
                    f"外部对同一笔流水出现 {len(group)} 笔结算",
                ))
            if primary_match:
                matched_count += 1
            continue

        item, is_match = _compare_one(ledger, group[0])
        if item is not None:
            items.append(item)
        if is_match:
            matched_count += 1

    # 2) 外部有、内部没有对应资金流水
    for txn_id, group in settlement_groups.items():
        if txn_id in ledger_by_id:
            continue
        for entry in group:
            logger.info(
                "对账 txn_id=%s 外部金额=%s/%s 内部=无 判定=%s",
                txn_id, entry.amount, entry.currency,
                DType.ORPHAN_SETTLEMENT.value,
            )
            items.append(_make_item(
                txn_id, DType.ORPHAN_SETTLEMENT, None, entry,
                "外部结算流水在内部无对应 CAPTURE/REFUND 资金流水",
            ))

    # 确定性输出顺序，保证结果可复现
    items.sort(key=lambda i: (i.txn_id, i.discrepancy.value))
    report = ReconcileReport(
        matched_count=matched_count,
        ledger_txn_count=len(ledger_by_id),
        settlement_entry_count=len(settlements),
        items=items,
    )
    logger.info(
        "对账完成 内部流水=%d 外部流水=%d 匹配=%d 差异=%d",
        report.ledger_txn_count, report.settlement_entry_count,
        report.matched_count, len(items),
    )
    return report


def _compare_one(
    ledger: Transaction, settlement: SettlementEntry
) -> tuple[ReconcileItem | None, bool]:
    """逐笔比较，返回 (差异项或 None, 是否完全匹配)。币种优先于金额判定。"""
    if ledger.currency != settlement.currency:
        logger.info(
            "对账 txn_id=%s 内部=%s/%s 外部=%s/%s 判定=%s",
            ledger.txn_id, ledger.amount, ledger.currency,
            settlement.amount, settlement.currency,
            DType.SETTLEMENT_CURRENCY_MISMATCH.value,
        )
        return _make_item(
            ledger.txn_id, DType.SETTLEMENT_CURRENCY_MISMATCH,
            ledger, settlement, "币种不一致",
        ), False
    if ledger.amount != settlement.amount:
        logger.info(
            "对账 txn_id=%s 内部=%s/%s 外部=%s/%s 判定=%s",
            ledger.txn_id, ledger.amount, ledger.currency,
            settlement.amount, settlement.currency,
            DType.AMOUNT_MISMATCH.value,
        )
        return _make_item(
            ledger.txn_id, DType.AMOUNT_MISMATCH,
            ledger, settlement,
            f"金额不一致: 内部 {ledger.amount} 外部 {settlement.amount}",
        ), False
    logger.info(
        "对账 txn_id=%s 金额=%s/%s 判定=MATCH",
        ledger.txn_id, ledger.amount, ledger.currency,
    )
    return None, True
