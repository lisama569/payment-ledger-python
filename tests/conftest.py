"""pytest 共享夹具与日志配置。

为了在测试日志中打印“幂等键 / 请求参数 / 判定结果 / 失败原因”，
这里为 payment_ledger 包的 logger 挂一个 INFO 级 StreamHandler。
"""

from __future__ import annotations

import logging

import pytest

from app.service import PaymentService
from app.store import JsonStore


@pytest.fixture
def store(tmp_path):
    return JsonStore(str(tmp_path / "ledger.json"))


@pytest.fixture
def service(store):
    return PaymentService(store)


@pytest.fixture(autouse=True)
def _enable_payment_logs(caplog):
    caplog.set_level(logging.INFO, logger="payment_ledger")
    yield
