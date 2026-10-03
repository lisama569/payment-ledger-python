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
from .reconciliation_types import ReconciliationScope, SettlementRecord
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
    external_ref: str | None = None


class RefundBody(BaseModel):
    amount: int | None = Field(default=None, strict=True)
    currency: str
    external_ref: str | None = None


class SettlementLine(BaseModel):
    external_ref: str
    payment_id: str
    entry_type: EntryType
    amount: int = Field(strict=True)
    currency: str


class ReconcileScopeBody(BaseModel):
    from_seq: int | None = None
    to_seq: int | None = None
    from_time: str | None = None
    to_time: str | None = None
    incremental: bool = False


class ReconcileBody(BaseModel):
    statement: list[SettlementLine]
    scope: ReconcileScopeBody | None = None
    # When true, the conclusion is persisted as a recorded run that can
    # be listed, re-fetched after restart and diffed against other runs.
    record: bool = False


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
                    "external_ref": e.external_ref,
                    "created_at": e.created_at,
                }
                for e in payment.entries

            ],
        }

    def item_to_dict(item) -> dict:
        return {
            **asdict(item),
            "entry_type": item.entry_type.value,
            "discrepancy": item.discrepancy.value if item.discrepancy else None,
        }

    def scope_to_dict(scope: ReconciliationScope) -> dict:
        return asdict(scope)

    def run_to_dict(run) -> dict:
        return {
            "run_id": run.run_id,
            "created_at": run.created_at,
            "scope": scope_to_dict(run.scope),
            "statement_digest": run.statement_digest,
            "matched": [item_to_dict(m) for m in run.matched],
            "discrepancies": [item_to_dict(d) for d in run.discrepancies],
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
        scope = None
        if body.scope is not None:
            scope = ReconciliationScope(
                from_seq=body.scope.from_seq,
                to_seq=body.scope.to_seq,
                from_time=body.scope.from_time,
                to_time=body.scope.to_time,
                incremental=body.scope.incremental,
            )
        if body.record:
            run = reconciler.reconcile_and_record(statement, scope)
            return {"run_id": run.run_id, **run_to_dict(run)}
        report = reconciler.reconcile(statement, scope)
        return {
            "matched": [item_to_dict(m) for m in report.matched],
            "discrepancies": [item_to_dict(d) for d in report.discrepancies],
        }

    @app.get("/v1/reconcile/runs")
    def list_runs():
        return {"runs": [run_to_dict(r) for r in reconciler.list_runs()]}

    @app.get("/v1/reconcile/runs/{run_id}")
    def get_run(run_id: int):
        run = reconciler.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": f"reconciliation run {run_id} not found"})
        return run_to_dict(run)

    @app.get("/v1/reconcile/diff")
    def diff_runs(older: int, newer: int):
        try:
            diff = reconciler.diff_runs(older, newer)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": str(exc)})
        return {
            "older_run_id": diff.older_run_id,
            "newer_run_id": diff.newer_run_id,
            "new_discrepancies": [item_to_dict(d) for d in diff.new_discrepancies],
            "persisting_discrepancies": [item_to_dict(d) for d in diff.persisting_discrepancies],
            "resolved_discrepancies": [item_to_dict(d) for d in diff.resolved_discrepancies],
        }

    return app
