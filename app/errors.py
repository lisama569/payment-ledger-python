"""可区分的业务失败原因。

所有业务非法操作均抛出 :class:`LedgerError`，并携带稳定的 ``code``，
调用方（HTTP 层、测试、日志）可据此区分失败原因。
"""

from __future__ import annotations


class ErrorCode:
    """业务失败原因码（稳定字符串，供程序化判定与日志检索）。"""

    PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    OVER_CAPTURE = "OVER_CAPTURE"
    OVER_REFUND = "OVER_REFUND"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


class LedgerError(Exception):
    """携带稳定原因码的业务异常。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"[{self.code}] {self.message}"
