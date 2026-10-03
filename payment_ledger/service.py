"""Payment ledger service: authorize / capture / refund with idempotency.

State transitions
-----------------
AUTHORIZED        --capture(part)--> PARTIALLY_CAPTURED
AUTHORIZED        --capture(full)--> CAPTURED
PARTIALLY_CAPTURED --capture(rest)--> CAPTURED
CAPTURED/PART_*   --refund(part)-->  PARTIALLY_REFUNDED
*                 --refund(full of captured)--> REFUNDED
Anything except AUTHORIZED --void--> VOIDED (not exposed yet)

Rules enforced
--------------
* All amounts are positive integers in the smallest currency unit.
* A payment may never be captured above its authorized amount.
* A payment may never be refunded above its captured amount.
* Currency of every operation must match the payment currency.
* Illegal transitions (e.g. capture after full refund) are rejected.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from typing import Callable

from .errors import (
    AmountExceededError,
    CurrencyMismatchError,
    DuplicateExternalRefError,
    IdempotencyConflictError,
    InvalidAmountError,
    InvalidStateTransitionError,
    PaymentNotFoundError,
)
from .models import (
    AuthorizeRequest,
    CaptureRequest,
    EntryType,
    OperationResult,
    Payment,
    PaymentStatus,
    RefundRequest,
)
from .repository import LedgerRepository, utc_now_iso

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def canonical_request_hash(operation: str, payload: dict) -> str:
    """Stable SHA-256 over (operation, canonical JSON) for conflict detection."""
    canonical = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    import hashlib

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_money(amount: int, field: str) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidAmountError(f"{field} must be an integer amount, got {type(amount).__name__}")
    if amount <= 0:
        raise InvalidAmountError(f"{field} must be a positive integer, got {amount}")


def _validate_currency(currency: str) -> None:
    if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
        raise InvalidAmountError(f"currency must be a 3-letter uppercase ISO code, got {currency!r}")


def _validate_external_ref(external_ref: str | None) -> None:
    if external_ref is None:
        return
    if not isinstance(external_ref, str) or not external_ref.strip():
        raise InvalidAmountError("external_ref must be a non-empty string when provided")


def _derive_status(payment: Payment) -> PaymentStatus:
    if payment.refunded_amount == payment.captured_amount:
        return PaymentStatus.REFUNDED
    if payment.refunded_amount > 0:
        return PaymentStatus.PARTIALLY_REFUNDED
    if payment.captured_amount == payment.authorized_amount:
        return PaymentStatus.CAPTURED
    return PaymentStatus.PARTIALLY_CAPTURED


def _result_from(
    payment: Payment,
    entry_type: EntryType,
    amount: int,
    key: str,
    replayed: bool,
    external_ref: str | None = None,
) -> OperationResult:
    return OperationResult(
        payment_id=payment.payment_id,
        status=payment.status,
        entry_type=entry_type,
        amount=amount,
        currency=payment.currency,
        captured_amount=payment.captured_amount,
        refunded_amount=payment.refunded_amount,
        idempotency_key=key,
        replayed=replayed,
        external_ref=external_ref,
    )


class PaymentLedgerService:
    def __init__(self, repository: LedgerRepository) -> None:
        self.repo = repository

    # -- idempotency scaffolding --------------------------------------

    def _serialize_result(self, result: OperationResult) -> str:
        data = asdict(result)
        data["status"] = result.status.value
        data["entry_type"] = result.entry_type.value
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _deserialize_result(self, raw: str) -> OperationResult:
        data = json.loads(raw)
        return OperationResult(
            payment_id=data["payment_id"],
            status=PaymentStatus(data["status"]),
            entry_type=EntryType(data["entry_type"]),
            amount=data["amount"],
            currency=data["currency"],
            captured_amount=data["captured_amount"],
            refunded_amount=data["refunded_amount"],
            idempotency_key=data["idempotency_key"],
            replayed=True,
            # Records persisted before external_ref existed simply lack
            # the key; replaying them must yield the original conclusion.
            external_ref=data.get("external_ref"),
        )

    def _run_idempotent(
        self,
        operation: str,
        key: str,
        payload: dict,
        execute: Callable[[sqlite3.Connection], tuple[Payment, EntryType, int, str | None]],
    ) -> OperationResult:
        request_hash = canonical_request_hash(operation, payload)

        # Fast path: already committed (also the restart-replay path).
        with self.repo.transaction() as conn:
            existing = self.repo.get_idempotency(conn, key)
        if existing is not None:
            stored_hash, stored_result = existing
            if stored_hash != request_hash:
                raise IdempotencyConflictError(
                    f"idempotency key {key!r} was already used with a different request"
                )
            return self._deserialize_result(stored_result)

        try:
            with self.repo.transaction() as conn:
                # Re-check inside the write transaction: another writer
                # may have committed the same key between our two tx.
                existing = self.repo.get_idempotency(conn, key)
                if existing is not None:
                    stored_hash, stored_result = existing
                    if stored_hash != request_hash:
                        raise IdempotencyConflictError(
                            f"idempotency key {key!r} was already used with a different request"
                        )
                    # Cannot COMMIT a replay from inside the outer tx
                    # semantics; raise a sentinel handled below instead.
                    return self._deserialize_result(stored_result)

                payment, entry_type, amount, external_ref = execute(conn)
                result = _result_from(
                    payment, entry_type, amount, key, replayed=False,
                    external_ref=external_ref,
                )
                now = utc_now_iso()
                self.repo.insert_entry(
                    conn, payment.payment_id, entry_type, amount, payment.currency,
                    key, now, external_ref=external_ref,
                )
                self.repo.save_idempotency(
                    conn, key, request_hash, self._serialize_result(result), now
                )
                return result
        except IdempotencyConflictError:
            raise
        except sqlite3.IntegrityError as exc:
            # The unique index on ledger_entries.external_ref is the hard
            # backstop against a settlement reference being registered on
            # two fund movements (e.g. a cross-process race).
            if "external_ref" in str(exc):
                raise DuplicateExternalRefError(
                    "external_ref was already registered on another ledger entry"
                ) from exc
            # Lost a race on the PRIMARY KEY of idempotency: replay the
            # winner's result if identical, otherwise reject.
            with self.repo.transaction() as conn:
                existing = self.repo.get_idempotency(conn, key)
            if existing is None:
                raise
            stored_hash, stored_result = existing
            if stored_hash != request_hash:
                raise IdempotencyConflictError(
                    f"idempotency key {key!r} was already used with a different request"
                ) from exc
            return self._deserialize_result(stored_result)

    # -- public operations --------------------------------------------

    def authorize(self, req: AuthorizeRequest, idempotency_key: str) -> OperationResult:
        _validate_money(req.amount, "amount")
        _validate_currency(req.currency)
        if not req.payment_id or not isinstance(req.payment_id, str):
            raise InvalidAmountError("payment_id must be a non-empty string")
        if not idempotency_key:
            raise InvalidAmountError("idempotency_key is required")

        def execute(conn: sqlite3.Connection):
            now = utc_now_iso()
            try:
                self.repo.insert_payment(conn, req, now)
            except sqlite3.IntegrityError as exc:
                raise InvalidStateTransitionError(
                    f"payment {req.payment_id!r} already exists; cannot authorize twice"
                ) from exc
            payment = self.repo.get_payment(conn, req.payment_id)
            return payment, EntryType.AUTHORIZE, req.amount, None

        return self._run_idempotent(
            "authorize",
            idempotency_key,
            {"payment_id": req.payment_id, "amount": req.amount, "currency": req.currency},
            execute,
        )

    def capture(self, req: CaptureRequest, idempotency_key: str) -> OperationResult:
        if req.amount is not None:
            _validate_money(req.amount, "amount")
        _validate_currency(req.currency)
        _validate_external_ref(req.external_ref)
        if not idempotency_key:
            raise InvalidAmountError("idempotency_key is required")

        def execute(conn: sqlite3.Connection):
            payment = self.repo.get_payment(conn, req.payment_id)
            if payment is None:
                raise PaymentNotFoundError(f"payment {req.payment_id!r} not found")
            if req.currency != payment.currency:
                raise CurrencyMismatchError(
                    f"request currency {req.currency!r} != payment currency {payment.currency!r}"
                )
            if req.external_ref is not None and (
                self.repo.find_entry_by_external_ref(conn, req.external_ref) is not None
            ):
                raise DuplicateExternalRefError(
                    f"external_ref {req.external_ref!r} is already registered "
                    "on another ledger entry"
                )
            capturable = payment.authorized_amount - payment.captured_amount
            if payment.status in (PaymentStatus.REFUNDED, PaymentStatus.VOIDED):
                raise InvalidStateTransitionError(
                    f"cannot capture payment in status {payment.status.value}"
                )
            if capturable <= 0:
                raise InvalidStateTransitionError(
                    f"payment {payment.payment_id!r} is already fully captured"
                )
            amount = capturable if req.amount is None else req.amount
            if amount > capturable:
                raise AmountExceededError(
                    f"capture {amount} exceeds capturable {capturable} "
                    f"(authorized={payment.authorized_amount}, "
                    f"already captured={payment.captured_amount})"
                )
            payment.captured_amount += amount
            payment.status = _derive_status(payment)
            self.repo.update_payment(conn, payment)
            return payment, EntryType.CAPTURE, amount, req.external_ref

        # Backward compatibility: the request fingerprint only carries
        # external_ref when one is supplied, so idempotency records
        # written before this field existed still hash identically.
        payload = {
            "payment_id": req.payment_id,
            "amount": req.amount,
            "currency": req.currency,
        }
        if req.external_ref is not None:
            payload["external_ref"] = req.external_ref
        return self._run_idempotent("capture", idempotency_key, payload, execute)

    def refund(self, req: RefundRequest, idempotency_key: str) -> OperationResult:
        if req.amount is not None:
            _validate_money(req.amount, "amount")
        _validate_currency(req.currency)
        _validate_external_ref(req.external_ref)
        if not idempotency_key:
            raise InvalidAmountError("idempotency_key is required")

        def execute(conn: sqlite3.Connection):
            payment = self.repo.get_payment(conn, req.payment_id)
            if payment is None:
                raise PaymentNotFoundError(f"payment {req.payment_id!r} not found")
            if req.currency != payment.currency:
                raise CurrencyMismatchError(
                    f"request currency {req.currency!r} != payment currency {payment.currency!r}"
                )
            if req.external_ref is not None and (
                self.repo.find_entry_by_external_ref(conn, req.external_ref) is not None
            ):
                raise DuplicateExternalRefError(
                    f"external_ref {req.external_ref!r} is already registered "
                    "on another ledger entry"
                )
            refundable = payment.captured_amount - payment.refunded_amount
            if refundable <= 0:
                raise InvalidStateTransitionError(
                    f"payment {payment.payment_id!r} has no captured amount left to refund"
                )
            amount = refundable if req.amount is None else req.amount
            if amount > refundable:
                raise AmountExceededError(
                    f"refund {amount} exceeds refundable {refundable} "
                    f"(captured={payment.captured_amount}, "
                    f"already refunded={payment.refunded_amount})"
                )
            payment.refunded_amount += amount
            payment.status = _derive_status(payment)
            self.repo.update_payment(conn, payment)
            return payment, EntryType.REFUND, amount, req.external_ref

        # See capture(): fingerprint stays identical for ref-less calls.
        payload = {
            "payment_id": req.payment_id,
            "amount": req.amount,
            "currency": req.currency,
        }
        if req.external_ref is not None:
            payload["external_ref"] = req.external_ref
        return self._run_idempotent("refund", idempotency_key, payload, execute)

    # -- reads ---------------------------------------------------------

    def get_payment(self, payment_id: str) -> Payment:
        with self.repo.transaction() as conn:
            payment = self.repo.get_payment(conn, payment_id)
        if payment is None:
            raise PaymentNotFoundError(f"payment {payment_id!r} not found")
        return payment
