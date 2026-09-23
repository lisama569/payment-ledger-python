"""FastAPI HTTP 接口：授权 / 捕获 / 退款 / 查询 / 对账。

错误以统一 JSON 返回，``error.code`` 为稳定原因码；HTTP 状态码：
- 409 IDEMPOTENCY_CONFLICT
- 404 PAYMENT_NOT_FOUND
- 422 其余业务规则拒绝（金额、超额、币种、非法状态迁移）
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.errors import LedgerError
from app.models import (
    AuthorizeCommand,
    CaptureCommand,
    RefundCommand,
    SettlementEntry,
)
from app.reconciliation import reconcile
from app.service import PaymentService
from app.store import JsonStore

logger = logging.getLogger("payment_ledger.api")
DEFAULT_LEDGER_PATH = os.environ.get("LEDGER_FILE", "data/ledger.json")

_CONFLICT = {"IDEMPOTENCY_CONFLICT"}
_NOT_FOUND = {"PAYMENT_NOT_FOUND"}


class AuthorizeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idem_key: str = Field(min_length=1)
    amount: int = Field(strict=True)  # strict: 1.0 之类的 float 直接拒绝
    currency: str = Field(min_length=1)


class CaptureBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idem_key: str = Field(min_length=1)
    payment_id: str = Field(min_length=1)
    amount: int | None = Field(default=None, strict=True)  # null=全额
    currency: str = Field(min_length=1)


class RefundBody(CaptureBody):
    pass


class SettlementBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    txn_id: str
    amount: int = Field(strict=True)
    currency: str


class ReconcileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settlements: list[SettlementBody]


def create_app(store: JsonStore | None = None) -> FastAPI:
    store = store or JsonStore(DEFAULT_LEDGER_PATH)
    service = PaymentService(store)
    app = FastAPI(title="payment-ledger", version="0.1.0")
    app.state.store = store
    app.state.service = service

    @app.exception_handler(LedgerError)
    async def ledger_error_handler(request: Request, exc: LedgerError):
        if exc.code in _CONFLICT:
            status = 409
        elif exc.code in _NOT_FOUND:
            status = 404
        else:
            status = 422
        logger.info(
            "HTTP 拒绝 path=%s reason=%s message=%s",
            request.url.path, exc.code, exc.message,
        )
        return JSONResponse(
            status_code=status,
            content={"ok": False, "error": {"code": exc.code,
                                            "message": exc.message}},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/payments/authorize")
    def authorize(body: AuthorizeBody) -> dict[str, Any]:
        result = service.authorize(
            AuthorizeCommand(body.idem_key, body.amount, body.currency)
        )
        return {"ok": True, "result": result.to_dict()}

    @app.post("/v1/payments/capture")
    def capture(body: CaptureBody) -> dict[str, Any]:
        result = service.capture(
            CaptureCommand(
                body.idem_key, body.payment_id, body.amount, body.currency
            )
        )
        return {"ok": True, "result": result.to_dict()}

    @app.post("/v1/payments/refund")
    def refund(body: RefundBody) -> dict[str, Any]:
        result = service.refund(
            RefundCommand(
                body.idem_key, body.payment_id, body.amount, body.currency
            )
        )
        return {"ok": True, "result": result.to_dict()}

    @app.get("/v1/payments/{payment_id}")
    def get_payment(payment_id: str) -> dict[str, Any]:
        payment = service.get_payment(payment_id)
        return {
            "ok": True,
            "result": {
                **payment.to_dict(),
                "status": payment.status.value,
                "remaining_capturable": payment.remaining_capturable,
                "remaining_refundable": payment.remaining_refundable,
            },
        }

    @app.post("/v1/reconcile")
    def reconcile_endpoint(body: ReconcileBody) -> dict[str, Any]:
        entries = [
            SettlementEntry(e.txn_id, e.amount, e.currency)
            for e in body.settlements
        ]
        report = reconcile(store, entries)
        return {"ok": True, "report": report.to_dict()}

    return app


app = create_app()
