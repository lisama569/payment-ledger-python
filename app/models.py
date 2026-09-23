"""领域类型定义。

金额一律使用最小货币单位的正整数（例如人民币“分”），
任何路径上都不允许使用浮点金额。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TxnType(str, Enum):
    """资金流水类型。"""

    AUTHORIZE = "AUTHORIZE"
    CAPTURE = "CAPTURE"
    REFUND = "REFUND"


class PaymentStatus(str, Enum):
    """对外暴露的支付单状态（由金额字段派生）。"""

    AUTHORIZED = "AUTHORIZED"          # 已授权，未捕获未退款
    PARTIALLY_CAPTURED = "PARTIALLY_CAPTURED"  # 部分捕获
    CAPTURED = "CAPTURED"              # 全额捕获
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"  # 发生过退款（仍可继续退）
    REFUNDED = "REFUNDED"              # 已捕获金额全额退款


@dataclass
class Transaction:
    """不可变资金流水（append-only，一旦落地不得修改/删除）。"""

    txn_id: str
    payment_id: str
    txn_type: TxnType
    amount: int
    currency: str
    created_at_ms: int
    idem_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "payment_id": self.payment_id,
            "txn_type": self.txn_type.value,
            "amount": self.amount,
            "currency": self.currency,
            "created_at_ms": self.created_at_ms,
            "idem_key": self.idem_key,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Transaction:
        return cls(
            txn_id=d["txn_id"],
            payment_id=d["payment_id"],
            txn_type=TxnType(d["txn_type"]),
            amount=int(d["amount"]),
            currency=d["currency"],
            created_at_ms=int(d["created_at_ms"]),
            idem_key=d["idem_key"],
        )


@dataclass
class Payment:
    """支付授权单及其累计金额。"""

    payment_id: str
    authorized_amount: int
    currency: str
    created_at_ms: int
    captured_amount: int = 0
    refunded_amount: int = 0
    refund_started: bool = False  # 一旦开始退款，不允许再捕获

    @property
    def remaining_capturable(self) -> int:
        return self.authorized_amount - self.captured_amount

    @property
    def remaining_refundable(self) -> int:
        return self.captured_amount - self.refunded_amount

    @property
    def status(self) -> PaymentStatus:
        if self.refunded_amount > 0:
            if self.refunded_amount == self.captured_amount:
                return PaymentStatus.REFUNDED
            return PaymentStatus.PARTIALLY_REFUNDED
        if self.captured_amount == 0:
            return PaymentStatus.AUTHORIZED
        if self.captured_amount < self.authorized_amount:
            return PaymentStatus.PARTIALLY_CAPTURED
        return PaymentStatus.CAPTURED

    def to_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "authorized_amount": self.authorized_amount,
            "currency": self.currency,
            "created_at_ms": self.created_at_ms,
            "captured_amount": self.captured_amount,
            "refunded_amount": self.refunded_amount,
            "refund_started": self.refund_started,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Payment:
        return cls(
            payment_id=d["payment_id"],
            authorized_amount=int(d["authorized_amount"]),
            currency=d["currency"],
            created_at_ms=int(d["created_at_ms"]),
            captured_amount=int(d.get("captured_amount", 0)),
            refunded_amount=int(d.get("refunded_amount", 0)),
            refund_started=bool(d.get("refund_started", False)),
        )


@dataclass
class AuthorizeCommand:
    idem_key: str
    amount: int
    currency: str


@dataclass
class CaptureCommand:
    idem_key: str
    payment_id: str
    amount: int | None  # None 表示全额捕获
    currency: str


@dataclass
class RefundCommand:
    idem_key: str
    payment_id: str
    amount: int | None  # None 表示全额退已捕获金额
    currency: str


@dataclass
class OperationResult:
    """一次写操作（或其幂等重放）的结论。"""

    idem_key: str
    replayed: bool
    txn_id: str
    payment_id: str
    txn_type: TxnType
    amount: int
    currency: str
    payment_status: PaymentStatus
    captured_amount: int
    refunded_amount: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "idem_key": self.idem_key,
            "replayed": self.replayed,
            "txn_id": self.txn_id,
            "payment_id": self.payment_id,
            "txn_type": self.txn_type.value,
            "amount": self.amount,
            "currency": self.currency,
            "payment_status": self.payment_status.value,
            "captured_amount": self.captured_amount,
            "refunded_amount": self.refunded_amount,
        }


@dataclass
class IdempotencyRecord:
    """幂等键的首次结论，落地持久化，重启后可复现。"""

    idem_key: str
    txn_id: str
    payment_id: str
    txn_type: TxnType
    amount: int
    currency: str
    success: bool
    error_code: str | None
    created_at_ms: int
    fingerprint: str = ""  # 首次请求内容的规范化指纹，用于检测同键异参

    def to_dict(self) -> dict[str, Any]:
        return {
            "idem_key": self.idem_key,
            "txn_id": self.txn_id,
            "payment_id": self.payment_id,
            "txn_type": self.txn_type.value,
            "amount": self.amount,
            "currency": self.currency,
            "success": self.success,
            "error_code": self.error_code,
            "created_at_ms": self.created_at_ms,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IdempotencyRecord:
        return cls(
            idem_key=d["idem_key"],
            txn_id=d["txn_id"],
            payment_id=d["payment_id"],
            txn_type=TxnType(d["txn_type"]),
            amount=int(d["amount"]),
            currency=d["currency"],
            success=bool(d["success"]),
            error_code=d.get("error_code"),
            created_at_ms=int(d["created_at_ms"]),
            fingerprint=d.get("fingerprint", ""),
        )


@dataclass
class State:
    """持久化的全部状态。"""

    payments: dict[str, Payment] = field(default_factory=dict)
    transactions: list[Transaction] = field(default_factory=list)
    idempotency: dict[str, IdempotencyRecord] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "payments": {pid: p.to_dict() for pid, p in self.payments.items()},
            "transactions": [t.to_dict() for t in self.transactions],
            "idempotency": {k: r.to_dict() for k, r in self.idempotency.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> State:
        return cls(
            payments={
                pid: Payment.from_dict(pd)
                for pid, pd in d.get("payments", {}).items()
            },
            transactions=[
                Transaction.from_dict(td) for td in d.get("transactions", [])
            ],
            idempotency={
                k: IdempotencyRecord.from_dict(rd)
                for k, rd in d.get("idempotency", {}).items()
            },
        )


@dataclass
class SettlementEntry:
    """外部结算流水（对账输入，只读）。"""

    txn_id: str
    amount: int
    currency: str


class DiscrepancyType(str, Enum):
    MISSING_SETTLEMENT = "MISSING_SETTLEMENT"    # 内部有资金影响，外部无结算
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"          # 金额不一致
    DUPLICATE_SETTLEMENT = "DUPLICATE_SETTLEMENT"  # 外部同一笔重复入账
    ORPHAN_SETTLEMENT = "ORPHAN_SETTLEMENT"      # 外部有，内部无对应资金流水
    SETTLEMENT_CURRENCY_MISMATCH = "SETTLEMENT_CURRENCY_MISMATCH"  # 币种不一致


@dataclass
class ReconcileItem:
    txn_id: str
    discrepancy: DiscrepancyType
    ledger_amount: int | None
    settlement_amount: int | None
    ledger_currency: str | None
    settlement_currency: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "discrepancy": self.discrepancy.value,
            "ledger_amount": self.ledger_amount,
            "settlement_amount": self.settlement_amount,
            "ledger_currency": self.ledger_currency,
            "settlement_currency": self.settlement_currency,
            "detail": self.detail,
        }


@dataclass
class ReconcileReport:
    matched_count: int
    ledger_txn_count: int
    settlement_entry_count: int
    items: list[ReconcileItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_count": self.matched_count,
            "ledger_txn_count": self.ledger_txn_count,
            "settlement_entry_count": self.settlement_entry_count,
            "discrepancies": [i.to_dict() for i in self.items],
        }


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"
