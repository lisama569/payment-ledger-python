"""Shared test helpers: fresh file-backed DBs and structured request logging.

Tests deliberately use file-backed SQLite so that every scenario runs
against the same persistence path used in production (and so restart
replay tests can reopen the same file).
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from payment_ledger.models import EntryType, PaymentStatus
from payment_ledger.reconciliation import Reconciler
from payment_ledger.repository import LedgerRepository
from payment_ledger.service import PaymentLedgerService

log = logging.getLogger("payment_ledger.tests")


def setup_logging() -> None:
    if not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def new_service() -> tuple[PaymentLedgerService, Path]:
    fd, path = tempfile.mkstemp(prefix="ledger_", suffix=".db")
    Path(path).unlink(missing_ok=True)  # repository creates the file
    import os

    os.close(fd)
    repo = LedgerRepository(path)
    return PaymentLedgerService(repo), Path(path)


def reopen_service(path: Path) -> PaymentLedgerService:
    """Simulate a service restart against the same database file."""
    return PaymentLedgerService(LedgerRepository(str(path)))


def log_request(step: str, idempotency_key: str, params: dict) -> None:
    log.info("STEP=%s | idempotency_key=%s | request=%s", step, idempotency_key, params)


def log_outcome(ok: bool, result=None, error=None) -> None:
    if ok and result is not None:
        log.info(
            "JUDGEMENT=ACCEPTED | result=%s",
            {
                "payment_id": result.payment_id,
                "entry_type": result.entry_type.value,
                "amount": result.amount,
                "currency": result.currency,
                "status": result.status.value,
                "captured": result.captured_amount,
                "refunded": result.refunded_amount,
                "replayed": result.replayed,
                "idempotency_key": result.idempotency_key,
            },
        )
    else:
        log.info("JUDGEMENT=REJECTED | reason_code=%s | message=%s", error.code, error)


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        setup_logging()
        self.service, self.db_path = new_service()

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            self.db_path.with_name(self.db_path.name + suffix).unlink(missing_ok=True)

    def assert_rejected(self, exc_class, fn):
        with self.assertRaises(exc_class) as ctx:
            fn()
        log_outcome(False, error=ctx.exception)
        return ctx.exception
