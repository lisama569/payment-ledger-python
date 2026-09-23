"""FastAPI HTTP layer exposing the ledger service."""
from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .errors import (
    AmountExceededError,
    CurrencyMismatchError,
    IdempotencyConflictError,
    InvalidAmountError,
    InvalidStateTransitionError,
    LedgerError,
    PaymentNotFoundError,
)
from .models import (
    AuthorizeRequest,
    CaptureRequest,
    EntryType,
    RefundRequest,
)
from .reconciliation import Reconciler
from .reconciliation_types import SettlementRecord
from .repository import LedgerRepository
from .service import PaymentLedgerService

DEFAULT_DB_PATH = os.environ.get("PAYMENT_LEDGER_DB", "payment_ledger.db")

_HTTP_STATUS = {
    PaymentNotFoundError: 404,
    InvalidStateTransitionError: 409,
    AmountExceededError: 422,
    CurrencyMismatchError: 422,
    InvalidAmountError: 400,
    IdempotencyConflictError: 409,
}


class AuthorizeBody(BaseModel):
    payment_id: str = Field(min_length=1)
    amount: int = Field(strict=True)
    currency: str


class CaptureBody(BaseModel):
    amount: int | None = Field(default=None, strict=True)
    currency: str


class RefundBody(BaseModel):
    amount: int | None = Field(default=None, strict=True)
    currency: str


class SettlementLine(BaseModel):
    external_ref: str
    payment_id: str
    entry_type: EntryType
    amount: int = Field(strict=True)
    currency: str


class ReconcileBody(BaseModel):
    statement: list[SettlementLine]


def create_app(db_path: str | None = None) -> FastAPI:
    path = DEFAULT_DB_PATH if db_path is None else db_path
    repository = LedgerRepository(path)
    service = PaymentLedgerService(repository)
    reconciler = Reconciler(repository)

    app = FastAPI(title="Payment Ledger", version="0.1.0")
    app.state.repository = repository
    app.state.service = service

    def raise_ledger_error(exc: LedgerError) -> None:
        status = _HTTP_STATUS.get(type(exc), 400)
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})

    def result_to_dict(result) -> dict:
        data = asdict(result)
        data["status"] = result.status.value
        data["entry_type"] = result.entry_type.value
        return data

    @app.post("/v1/payments/authorize")
    def authorize(body: AuthorizeBody, idempotency_key: str = Header(..., alias="Idempotency-Key")):
        try:
            result = service.authorize(
                AuthorizeRequest(body.payment_id, body.amount, body.currency),
                idempotency_key,
            )
        except LedgerError as exc:
            raise_ledger_error(exc)
        return result_to_dict(result)

    @app.post("/v1/payments/{payment_id}/capture")
    def capture(
        payment_id: str,
        body: CaptureBody,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        try:
            result = service.capture(
                CaptureRequest(payment_id, body.amount, body.currency),
                idempotency_key,
            )
        except LedgerError as exc:
            raise_ledger_error(exc)
        return result_to_dict(result)

    @app.post("/v1/payments/{payment_id}/refund")
    def refund(
        payment_id: str,
        body: RefundBody,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        try:
            result = service.refund(
                RefundRequest(payment_id, body.amount, body.currency),
                idempotency_key,
            )
        except LedgerError as exc:
            raise_ledger_error(exc)
        return result_to_dict(result)

    @app.get("/v1/payments/{payment_id}")
    def get_payment(payment_id: str):
        try:
            payment = service.get_payment(payment_id)
        except PaymentNotFoundError as exc:
            raise_ledger_error(exc)
        return {
            "payment_id": payment.payment_id,
            "currency": payment.currency,
            "authorized_amount": payment.authorized_amount,
            "captured_amount": payment.captured_amount,
            "refunded_amount": payment.refunded_amount,
            "status": payment.status.value,
            "version": payment.version,
            "entries": [
                {
                    "seq": e.seq,
                    "entry_type": e.entry_type.value,
                    "amount": e.amount,
                    "currency": e.currency,
                    "idempotency_key": e.idempotency_key,
                    "created_at": e.created_at,
                }
                for e in payment.entries

            ],
        }

    @app.post("/v1/reconcile")
    def reconcile(body: ReconcileBody):
        statement = [
            SettlementRecord(
                external_ref=line.external_ref,
                payment_id=line.payment_id,
                entry_type=line.entry_type,
                amount=line.amount,
                currency=line.currency,
            )
            for line in body.statement
        ]
        report = reconciler.reconcile(statement)
        return {
            "matched": [asdict(m) | {"entry_type": m.entry_type.value, "discrepancy": None} for m in report.matched],
            "discrepancies": [
                {
                    **asdict(d),
                    "entry_type": d.entry_type.value,
                    "discrepancy": d.discrepancy.value if d.discrepancy else None,
                }
                for d in report.discrepancies
            ],
        }

    return app
