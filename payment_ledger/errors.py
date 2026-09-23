"""Domain error types with distinguishable failure reasons."""


class LedgerError(Exception):
    """Base class for all ledger errors."""

    code = "ledger_error"


class PaymentNotFoundError(LedgerError):
    code = "payment_not_found"


class InvalidStateTransitionError(LedgerError):
    code = "invalid_state_transition"


class AmountExceededError(LedgerError):
    code = "amount_exceeded"


class CurrencyMismatchError(LedgerError):
    code = "currency_mismatch"


class InvalidAmountError(LedgerError):
    code = "invalid_amount"


class IdempotencyConflictError(LedgerError):
    code = "idempotency_conflict"
