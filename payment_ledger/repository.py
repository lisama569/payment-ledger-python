"""SQLite-backed append-only ledger repository.

Persists payments, ledger entries and idempotency records so that
results and idempotency rejections survive process restarts.

Concurrency model
-----------------
* Writes run inside ``BEGIN IMMEDIATE`` transactions guarded by a
  process-local lock, serializing in-process writers. SQLite's write
  lock plus ``busy_timeout`` serializes writers across processes.
* The ``idempotency`` table has a PRIMARY KEY on the idempotency key,
  which is the hard guarantee that a key can only land once even under
  races.
* Reconciliation opens its own ``mode=ro`` connection, so it is
  physically incapable of modifying committed data.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .models import (
    AuthorizeRequest,
    EntryType,
    LedgerEntry,
    OperationResult,
    Payment,
    PaymentStatus,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id        TEXT PRIMARY KEY,
    currency          TEXT NOT NULL,
    authorized_amount INTEGER NOT NULL,
    captured_amount   INTEGER NOT NULL,
    refunded_amount   INTEGER NOT NULL,
    status            TEXT NOT NULL,
    version           INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id      TEXT NOT NULL,
    entry_type      TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_payment ON ledger_entries(payment_id);

CREATE TABLE IF NOT EXISTS idempotency (
    idempotency_key TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- Reconciliation run journal: every run's conclusion is persisted so
-- results survive restarts and consecutive runs can be diffed.
-- This table is append-only and never touches ledger_entries.
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    scope_json       TEXT NOT NULL,
    statement_hash   TEXT NOT NULL,
    report_json      TEXT NOT NULL,
    max_seq          INTEGER NOT NULL,
    discrepancy_keys TEXT NOT NULL
);
"""

# Columns added after the initial schema, applied idempotently.
_MIGRATIONS = (
    # Settlement reference registered on fund-moving entries.
    "ALTER TABLE ledger_entries ADD COLUMN external_ref TEXT",
)

_POST_MIGRATION_SCHEMA = """
-- A settlement reference may be attached to at most one ledger entry,
-- across all payments. SQLite unique indexes treat NULLs as distinct,
-- so legacy rows without a reference are unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_external_ref
    ON ledger_entries(external_ref);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LedgerRepository:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        if db_path == ":memory:":
            # Shared-cache in-memory DB with a unique name per repository
            # so independent instances never share data, while read-only
            # reconciliation connections still see this instance's data.
            mem_name = f"payment_ledger_{uuid.uuid4().hex}"
            self._uri = f"file:{mem_name}?mode=memory&cache=shared"
            self._read_uri = f"file:{mem_name}?mode=ro&cache=shared"
            self._conn = sqlite3.connect(
                self._uri, uri=True, check_same_thread=False, isolation_level=None
            )
        else:
            self._uri = f"file:{db_path}"
            self._read_uri = f"file:{db_path}?mode=ro"
            self._conn = sqlite3.connect(
                db_path, check_same_thread=False, isolation_level=None
            )
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    # -- schema / transactions -----------------------------------------

    def init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(_SCHEMA)
            self._apply_migrations()

    def _apply_migrations(self) -> None:
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(ledger_entries)")
        }
        for statement in _MIGRATIONS:
            # Each migration names its column after ADD COLUMN.
            column = statement.rsplit(" ", 2)[-2]
            if column not in existing:
                self._conn.execute(statement)
        self._conn.executescript(_POST_MIGRATION_SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize in-process writers and open an immediate tx."""
        conn = self._conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def read_connection(self) -> sqlite3.Connection:
        """Open a strictly read-only connection (rejects any write).

        File databases use SQLite's URI ``mode=ro``; in-memory shared
        cache databases cannot be opened ro, so ``query_only`` is used,
        which makes every write fail with "readonly database".
        """
        if self.db_path == ":memory:":
            conn = sqlite3.connect(
                self._uri, uri=True, check_same_thread=False
            )
            conn.execute("PRAGMA query_only = ON")
        else:
            conn = sqlite3.connect(
                self._read_uri, uri=True, check_same_thread=False
            )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    # -- payments ------------------------------------------------------

    def _row_to_payment(self, row: sqlite3.Row, entries: list[LedgerEntry]) -> Payment:
        return Payment(
            payment_id=row["payment_id"],
            currency=row["currency"],
            authorized_amount=row["authorized_amount"],
            captured_amount=row["captured_amount"],
            refunded_amount=row["refunded_amount"],
            status=PaymentStatus(row["status"]),
            version=row["version"],
            entries=entries,
        )

    def get_payment(self, conn: sqlite3.Connection, payment_id: str) -> Payment | None:
        row = conn.execute(
            "SELECT * FROM payments WHERE payment_id = ?", (payment_id,)
        ).fetchone()
        if row is None:
            return None
        entries = self.list_entries(conn, payment_id)
        return self._row_to_payment(row, entries)

    def insert_payment(
        self, conn: sqlite3.Connection, req: AuthorizeRequest, created_at: str
    ) -> None:
        conn.execute(
            "INSERT INTO payments (payment_id, currency, authorized_amount, "
            "captured_amount, refunded_amount, status, version, created_at) "
            "VALUES (?, ?, ?, 0, 0, ?, 0, ?)",
            (
                req.payment_id,
                req.currency,
                req.amount,
                PaymentStatus.AUTHORIZED.value,
                created_at,
            ),
        )

    def update_payment(self, conn: sqlite3.Connection, payment: Payment) -> None:
        cur = conn.execute(
            "UPDATE payments SET currency = ?, authorized_amount = ?, "
            "captured_amount = ?, refunded_amount = ?, status = ?, "
            "version = version + 1 WHERE payment_id = ? AND version = ?",
            (
                payment.currency,
                payment.authorized_amount,
                payment.captured_amount,
                payment.refunded_amount,
                payment.status.value,
                payment.payment_id,
                payment.version,
            ),
        )
        payment.version += 1
        if cur.rowcount != 1:
            raise sqlite3.IntegrityError("payment version conflict")

    # -- ledger entries (append-only) ----------------------------------

    def insert_entry(
        self,
        conn: sqlite3.Connection,
        payment_id: str,
        entry_type: EntryType,
        amount: int,
        currency: str,
        idempotency_key: str,
        created_at: str,
        external_ref: str | None = None,
    ) -> LedgerEntry:
        cur = conn.execute(
            "INSERT INTO ledger_entries (payment_id, entry_type, amount, currency, "
            "idempotency_key, created_at, external_ref) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payment_id,
                entry_type.value,
                amount,
                currency,
                idempotency_key,
                created_at,
                external_ref,
            ),
        )
        return LedgerEntry(
            seq=cur.lastrowid,
            payment_id=payment_id,
            entry_type=entry_type,
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
            created_at=created_at,
            external_ref=external_ref,
        )

    def find_entry_by_external_ref(
        self, conn: sqlite3.Connection, external_ref: str
    ) -> LedgerEntry | None:
        row = conn.execute(
            "SELECT * FROM ledger_entries WHERE external_ref = ? ORDER BY seq",
            (external_ref,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    def _row_to_entry(self, r: sqlite3.Row) -> LedgerEntry:
        return LedgerEntry(
            seq=r["seq"],
            payment_id=r["payment_id"],
            entry_type=EntryType(r["entry_type"]),
            amount=r["amount"],
            currency=r["currency"],
            idempotency_key=r["idempotency_key"],
            created_at=r["created_at"],
            external_ref=r["external_ref"],
        )

    def list_entries(
        self, conn: sqlite3.Connection, payment_id: str | None = None
    ) -> list[LedgerEntry]:
        if payment_id is None:
            rows = conn.execute(
                "SELECT * FROM ledger_entries ORDER BY seq"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ledger_entries WHERE payment_id = ? ORDER BY seq",
                (payment_id,),
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    # -- idempotency ---------------------------------------------------

    def save_idempotency(
        self,
        conn: sqlite3.Connection,
        key: str,
        request_hash: str,
        result_json: str,
        created_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO idempotency (idempotency_key, request_hash, result_json, "
            "created_at) VALUES (?, ?, ?, ?)",
            (key, request_hash, result_json, created_at),
        )

    def get_idempotency(self, conn: sqlite3.Connection, key: str) -> tuple[str, str] | None:
        """Return (request_hash, result_json) or None."""
        row = conn.execute(
            "SELECT request_hash, result_json FROM idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return row["request_hash"], row["result_json"]

    # -- reconciliation run journal (append-only, separate from ledger) --

    def insert_reconciliation_run(
        self,
        conn: sqlite3.Connection,
        created_at: str,
        scope_json: str,
        statement_hash: str,
        report_json: str,
        max_seq: int,
        discrepancy_keys: str,
    ) -> int:
        cur = conn.execute(
            "INSERT INTO reconciliation_runs (created_at, scope_json, statement_hash, "
            "report_json, max_seq, discrepancy_keys) VALUES (?, ?, ?, ?, ?, ?)",
            (created_at, scope_json, statement_hash, report_json, max_seq, discrepancy_keys),
        )
        return cur.lastrowid

    def _row_to_run(self, row: sqlite3.Row) -> dict:
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "scope_json": row["scope_json"],
            "statement_hash": row["statement_hash"],
            "report_json": row["report_json"],
            "max_seq": row["max_seq"],
            "discrepancy_keys": row["discrepancy_keys"],
        }

    def get_latest_run(self, conn: sqlite3.Connection) -> dict | None:
        row = conn.execute(
            "SELECT * FROM reconciliation_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else self._row_to_run(row)

    def get_run(self, conn: sqlite3.Connection, run_id: int) -> dict | None:
        row = conn.execute(
            "SELECT * FROM reconciliation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else self._row_to_run(row)

    def list_runs(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM reconciliation_runs ORDER BY run_id"
        ).fetchall()
        return [self._row_to_run(r) for r in rows]
