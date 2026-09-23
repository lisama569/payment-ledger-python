"""支付资金流转核心服务：授权 / 捕获 / 退款 + 幂等提交。

并发与重放语义
--------------
所有写操作在同一把存储锁内完成“查幂等键 -> 校验 -> 落地流水 -> 写幂等结论”，
因此同一幂等键的并发请求只会有一个线程真正产生资金影响，其余线程读到
已落地的首次结论并原样返回（成功）或原样抛出（失败）。

幂等结论（含业务失败结论）持久化到磁盘，服务重启后重放仍得到一致结论；
同键携带不同请求内容（指纹不同）一律抛 IDEMPOTENCY_CONFLICT 且不产生任何影响。

金额
----
金额全部为最小货币单位的 ``int``；``float``/``bool`` 类型直接拒绝。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Callable

from app.errors import ErrorCode, LedgerError
from app.models import (
    AuthorizeCommand,
    CaptureCommand,
    IdempotencyRecord,
    OperationResult,
    Payment,
    RefundCommand,
    State,
    Transaction,
    TxnType,
    new_id,
    now_ms,
)
from app.store import JsonStore

logger = logging.getLogger("payment_ledger.service")


def _coerce_amount(value: object, field_name: str = "amount") -> int:
    """金额必须是正整数；拒绝 float（精度风险）与 bool（int 子类陷阱）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise LedgerError(
            ErrorCode.INVALID_AMOUNT,
            f"{field_name} 必须是最小货币单位的整数，收到: {value!r}",
        )
    if value <= 0:
        raise LedgerError(
            ErrorCode.INVALID_AMOUNT,
            f"{field_name} 必须为正整数，收到: {value}",
        )
    return value


def _fingerprint(txn_type: TxnType, payload: dict) -> str:
    raw = json.dumps(
        {"txn_type": txn_type.value, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PaymentService:
    def __init__(self, store: JsonStore):
        self.store = store

    # ----------------------------- 读取 -----------------------------

    def get_payment(self, payment_id: str) -> Payment:
        state = self.store.load()
        payment = state.payments.get(payment_id)
        if payment is None:
            raise LedgerError(
                ErrorCode.PAYMENT_NOT_FOUND, f"支付单不存在: {payment_id}"
            )
        return payment

    # ----------------------------- 写操作 -----------------------------

    def authorize(self, cmd: AuthorizeCommand) -> OperationResult:
        amount = _coerce_amount(cmd.amount)
        fp = _fingerprint(
            TxnType.AUTHORIZE, {"amount": amount, "currency": cmd.currency}
        )
        return self._execute(
            idem_key=cmd.idem_key,
            txn_type=TxnType.AUTHORIZE,
            amount=amount,
            currency=cmd.currency,
            fingerprint=fp,
            payment_id=None,
            apply=self._apply_authorize,
        )

    def capture(self, cmd: CaptureCommand) -> OperationResult:
        # 显式 None 表示全额；其余情况必须是正整数
        amount = None if cmd.amount is None else _coerce_amount(cmd.amount)
        fp = _fingerprint(
            TxnType.CAPTURE,
            {
                "payment_id": cmd.payment_id,
                "amount": amount,  # None 代表“全额捕获”语义
                "currency": cmd.currency,
            },
        )
        return self._execute(
            idem_key=cmd.idem_key,
            txn_type=TxnType.CAPTURE,
            amount=amount,  # 实际金额在 _apply 中解析
            currency=cmd.currency,
            fingerprint=fp,
            payment_id=cmd.payment_id,
            apply=self._apply_capture,
        )

    def refund(self, cmd: RefundCommand) -> OperationResult:
        amount = None if cmd.amount is None else _coerce_amount(cmd.amount)
        fp = _fingerprint(
            TxnType.REFUND,
            {
                "payment_id": cmd.payment_id,
                "amount": amount,
                "currency": cmd.currency,
            },
        )
        return self._execute(
            idem_key=cmd.idem_key,
            txn_type=TxnType.REFUND,
            amount=amount,
            currency=cmd.currency,
            fingerprint=fp,
            payment_id=cmd.payment_id,
            apply=self._apply_refund,
        )

    # ----------------------------- 幂等骨架 -----------------------------

    def _execute(
        self,
        *,
        idem_key: str,
        txn_type: TxnType,
        amount: int | None,
        currency: str,
        fingerprint: str,
        payment_id: str | None,
        apply: Callable[[State, str, str | None, int, str], tuple[Payment, int]],
    ) -> OperationResult:
        if not isinstance(idem_key, str) or not idem_key:
            raise ValueError("idem_key 必须是非空字符串")

        with self.store.lock():
            state = self.store.load()
            record = state.idempotency.get(idem_key)

            if record is not None:
                if record.fingerprint != fingerprint:
                    logger.warning(
                        "幂等冲突 idem_key=%s type=%s 既有指纹=%s 请求指纹=%s "
                        "判定=拒绝 reason=%s",
                        idem_key,
                        txn_type.value,
                        record.fingerprint[:12],
                        fingerprint[:12],
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                    )
                    raise LedgerError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "同一幂等键携带了不同的请求内容",
                    )
                # 重放：返回与首次完全一致的结论
                return self._replay(record, state)

            # 首次请求：执行业务；业务失败同样落地幂等结论
            txn_id = new_id("txn")
            ts = now_ms()
            try:
                payment, effective_amount = apply(
                    state, txn_id, payment_id, amount or 0, currency
                )
            except LedgerError as exc:
                self._persist_failure(
                    state, idem_key, txn_type, amount, currency, fingerprint,
                    payment_id, txn_id, ts, exc,
                )
                logger.info(
                    "操作失败 idem_key=%s type=%s params=%s 判定=拒绝 reason=%s",
                    idem_key, txn_type.value,
                    {"payment_id": payment_id, "amount": amount,
                     "currency": currency},
                    exc.code,
                )
                raise

            txn = Transaction(
                txn_id=txn_id,
                payment_id=payment.payment_id,
                txn_type=txn_type,
                amount=effective_amount,
                currency=currency,
                created_at_ms=ts,
                idem_key=idem_key,
            )
            state.transactions.append(txn)
            state.idempotency[idem_key] = IdempotencyRecord(
                idem_key=idem_key,
                txn_id=txn_id,
                payment_id=payment.payment_id,
                txn_type=txn_type,
                amount=effective_amount,
                currency=currency,
                success=True,
                error_code=None,
                created_at_ms=ts,
                fingerprint=fingerprint,
            )
            self.store.save(state)
            result = self._build_result(
                state.idempotency[idem_key], payment, replayed=False
            )
            logger.info(
                "操作成功 idem_key=%s type=%s params=%s 判定=首次成功 result=%s",
                idem_key, txn_type.value,
                {"payment_id": payment_id, "amount": amount,
                 "currency": currency},
                result.to_dict(),
            )
            return result

    def _replay(
        self, record: IdempotencyRecord, state: State
    ) -> OperationResult:
        if not record.success:
            logger.info(
                "重放失败结论 idem_key=%s type=%s 判定=重放(失败) reason=%s",
                record.idem_key, record.txn_type.value, record.error_code,
            )
            raise LedgerError(
                record.error_code or "UNKNOWN",
                f"重放首次失败结论: {record.error_code}",
            )
        payment = state.payments[record.payment_id]
        result = self._build_result(record, payment, replayed=True)
        logger.info(
            "重放成功结论 idem_key=%s type=%s 判定=重放(成功) result=%s",
            record.idem_key, record.txn_type.value, result.to_dict(),
        )
        return result

    @staticmethod
    def _build_result(
        record: IdempotencyRecord, payment: Payment, *, replayed: bool
    ) -> OperationResult:
        return OperationResult(
            idem_key=record.idem_key,
            replayed=replayed,
            txn_id=record.txn_id,
            payment_id=record.payment_id,
            txn_type=record.txn_type,
            amount=record.amount,
            currency=record.currency,
            payment_status=payment.status,
            captured_amount=payment.captured_amount,
            refunded_amount=payment.refunded_amount,
        )

    def _persist_failure(
        self, state: State, idem_key: str, txn_type: TxnType,
        amount: int | None, currency: str, fingerprint: str,
        payment_id: str | None, txn_id: str, ts: int, exc: LedgerError,
    ) -> None:
        state.idempotency[idem_key] = IdempotencyRecord(
            idem_key=idem_key,
            txn_id=txn_id,
            payment_id=payment_id or "",
            txn_type=txn_type,
            amount=amount if amount is not None else 0,
            currency=currency,
            success=False,
            error_code=exc.code,
            created_at_ms=ts,
            fingerprint=fingerprint,
        )
        self.store.save(state)

    # ----------------------------- 业务规则 -----------------------------

    def _apply_authorize(
        self,
        state: State,
        txn_id: str,
        payment_id: str | None,
        amount: int,
        currency: str,
    ) -> tuple[Payment, int]:
        new_payment_id = new_id("pay")
        payment = Payment(
            payment_id=new_payment_id,
            authorized_amount=amount,
            currency=currency,
            created_at_ms=now_ms(),
        )
        state.payments[new_payment_id] = payment
        return payment, amount

    def _apply_capture(
        self,
        state: State,
        txn_id: str,
        payment_id: str | None,
        amount: int,
        currency: str,
    ) -> tuple[Payment, int]:
        # amount == 0 表示调用方请求的是“全额捕获”（入口传入 None）。
        payment = self._require_payment(state, payment_id)
        if payment.currency != currency:
            raise LedgerError(
                ErrorCode.CURRENCY_MISMATCH,
                f"币种不一致: 支付单 {payment.currency}，请求 {currency}",
            )
        if payment.refund_started:
            raise LedgerError(
                ErrorCode.INVALID_STATE_TRANSITION,
                "已开始退款的支付单不允许再捕获",
            )
        if payment.remaining_capturable <= 0:
            raise LedgerError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"支付单已全额捕获，当前状态 {payment.status.value}",
            )
        effective = payment.remaining_capturable if amount == 0 else amount
        if effective > payment.remaining_capturable:
            raise LedgerError(
                ErrorCode.OVER_CAPTURE,
                f"超额捕获: 请求 {effective}，可捕获余额 "
                f"{payment.remaining_capturable}",
            )
        payment.captured_amount += effective
        return payment, effective

    def _apply_refund(
        self,
        state: State,
        txn_id: str,
        payment_id: str | None,
        amount: int,
        currency: str,
    ) -> tuple[Payment, int]:
        # amount == 0 表示调用方请求的是“全额退款”（退全部已捕获金额）。
        payment = self._require_payment(state, payment_id)
        if payment.currency != currency:
            raise LedgerError(
                ErrorCode.CURRENCY_MISMATCH,
                f"币种不一致: 支付单 {payment.currency}，请求 {currency}",
            )
        if payment.captured_amount <= 0:
            raise LedgerError(
                ErrorCode.INVALID_STATE_TRANSITION,
                "尚未捕获的支付单不允许退款",
            )
        if payment.remaining_refundable <= 0:
            raise LedgerError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"已捕获金额已全额退款，当前状态 {payment.status.value}",
            )
        effective = payment.remaining_refundable if amount == 0 else amount
        if effective > payment.remaining_refundable:
            raise LedgerError(
                ErrorCode.OVER_REFUND,
                f"超额退款: 请求 {effective}，可退款余额 "
                f"{payment.remaining_refundable}",
            )
        payment.refunded_amount += effective
        payment.refund_started = True
        return payment, effective

    @staticmethod
    def _require_payment(state: State, payment_id: str | None) -> Payment:
        if not payment_id or payment_id not in state.payments:
            raise LedgerError(
                ErrorCode.PAYMENT_NOT_FOUND,
                f"支付单不存在: {payment_id}",
            )
        return state.payments[payment_id]
