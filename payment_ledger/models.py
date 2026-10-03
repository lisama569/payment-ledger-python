"""Core domain types: payment, entries and request models.

All amounts are integers in the smallest currency unit (e.g. cents).
Floating point types must never be used for money.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PaymentStatus(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    PARTIALLY_CAPTURED = "PARTIALLY_CAPTURED"
    CAPTURED = "CAPTURED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    VOIDED = "VOIDED"


class EntryType(str, Enum):
    AUTHORIZE = "AUTHORIZE"
    CAPTURE = "CAPTURE"
    REFUND = "REFUND"


@dataclass
class LedgerEntry:
    seq: int
    payment_id: str
    entry_type: EntryType
    amount: int
    currency: str
    idempotency_key: str
    created_at: str
    # External settlement reference registered at capture/refund time.
    # None for entries recorded before this capability existed.
    external_ref: str | None = None


@dataclass
class Payment:
    payment_id: str
    currency: str
    authorized_amount: int
    captured_amount: int = 0
    refunded_amount: int = 0
    status: PaymentStatus = PaymentStatus.AUTHORIZED
    version: int = 0
    entries: list[LedgerEntry] = field(default_factory=list)


@dataclass
class AuthorizeRequest:
    payment_id: str
    amount: int
    currency: str


@dataclass
class CaptureRequest:
    payment_id: str
    amount: int | None  # None => capture the remaining authorized amount
    currency: str
    # Settlement-side reference for this fund movement; optional so that
    # callers upgraded before this field existed keep working unchanged.
    external_ref: str | None = None


@dataclass
class RefundRequest:
    payment_id: str
    amount: int | None  # None => refund the full captured amount
    currency: str
    external_ref: str | None = None


@dataclass
class OperationResult:
    payment_id: str
    status: PaymentStatus
    entry_type: EntryType
    amount: int
    currency: str
    captured_amount: int
    refunded_amount: int
    idempotency_key: str
    replayed: bool
