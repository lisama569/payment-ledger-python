"""FastAPI HTTP layer exposing the ledger service."""
from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .errors import (
    AmountExceededError,
    CurrencyMismatchError,
    DuplicateExternalRefError,
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
from .reconciliation_types import ReconcileScope, SettlementRecord
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
    DuplicateExternalRefError: 409,
}


class AuthorizeBody(BaseModel):
    payment_id: str = Field(min_length=1)
    amount: int = Field(strict=True)
    currency: str


class CaptureBody(BaseModel):
    amount: int | None = Field(default=None, strict=True)
    currency: str
    # Optional settlement-system reference for this fund movement.
    external_ref: str | None = None


class RefundBody(BaseModel):
    amount: int | None = Field(default=None, strict=True)
    currency: str
    # Optional settlement-system reference for this fund movement.
    external_ref: str | None = None


class SettlementLine(BaseModel):
    external_ref: str
    payment_id: str
    entry_type: EntryType
    amount: int = Field(strict=True)
    currency: str


class ReconcileBody(BaseModel):
    statement: list[SettlementLine]
    # Optional scoping: restrict which ledger entries this run looks at.
    since_seq: int | None = None
    until_seq: int | None = None
    since_time: str | None = None
    until_time: str | None = None
    # "incremental" reconciles only entries after the previous run.
    mode: str = "full"


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
                CaptureRequest(payment_id, body.amount, body.currency, body.external_ref),
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
                RefundRequest(payment_id, body.amount, body.currency, body.external_ref),
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
                    "external_ref": e.external_ref,
                }
                for e in payment.entries

            ],
        }

    def item_to_dict(item) -> dict:
        return {
            "discrepancy": item.discrepancy.value if item.discrepancy else None,
            "external_ref": item.external_ref,
            "payment_id": item.payment_id,
            "entry_type": item.entry_type.value,
            "ledger_amount": item.ledger_amount,
            "statement_amount": item.statement_amount,
            "currency": item.currency,
            "detail": item.detail,
            "ledger_seq": item.ledger_seq,
        }

    def report_to_dict(report) -> dict:
        return {
            "run_id": report.run_id,
            "matched": [item_to_dict(m) for m in report.matched],
            "discrepancies": [item_to_dict(d) for d in report.discrepancies],
            "new_discrepancies": [item_to_dict(d) for d in report.new_discrepancies],
            "ongoing_discrepancies": [
                item_to_dict(d) for d in report.ongoing_discrepancies
            ],
            "resolved_discrepancies": [
                item_to_dict(d) for d in report.resolved_discrepancies
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
        if body.mode == "incremental":
            report = reconciler.reconcile_incremental(statement)
        elif body.mode == "full":
            scope = ReconcileScope(
                since_seq=body.since_seq,
                until_seq=body.until_seq,
                since_time=body.since_time,
                until_time=body.until_time,
            )
            report = reconciler.reconcile(statement, scope=scope)
        else:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_amount", "message": f"unknown reconcile mode {body.mode!r}"},
            )
        return report_to_dict(report)

    @app.get("/v1/reconcile/runs")
    def list_reconcile_runs():
        return {"runs": reconciler.list_runs()}

    @app.get("/v1/reconcile/runs/{run_id}")
    def get_reconcile_run(run_id: int):
        run = reconciler.get_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "payment_not_found", "message": f"reconcile run {run_id} not found"},
            )
        return {
            "run_id": run["run_id"],
            "created_at": run["created_at"],
            "scope": run["scope"],
            "statement_hash": run["statement_hash"],
            "max_seq": run["max_seq"],
            "matched": [item_to_dict(m) for m in run["matched"]],
            "discrepancies": [item_to_dict(d) for d in run["discrepancies"]],
            "new_discrepancies": [item_to_dict(d) for d in run["new_discrepancies"]],
            "ongoing_discrepancies": [item_to_dict(d) for d in run["ongoing_discrepancies"]],
            "resolved_discrepancies": [item_to_dict(d) for d in run["resolved_discrepancies"]],
        }

    return app
