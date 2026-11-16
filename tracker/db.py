"""SQLite storage for reconciliation runs, exceptions and the audit trail."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at           TEXT    NOT NULL,
    label                TEXT    NOT NULL,
    as_of                TEXT    NOT NULL,
    settlement_window    INTEGER NOT NULL,
    total_orders         INTEGER NOT NULL,
    total_payments       INTEGER NOT NULL,
    total_settlements    INTEGER NOT NULL,
    fully_matched_orders INTEGER NOT NULL,
    pending_settlements  INTEGER NOT NULL,
    exception_count      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS exceptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    type          TEXT    NOT NULL,
    severity      TEXT    NOT NULL,
    message       TEXT    NOT NULL,
    order_id      TEXT,
    payment_id    TEXT,
    settlement_id TEXT,
    expected      TEXT,
    actual        TEXT,
    difference    TEXT,
    status        TEXT    NOT NULL DEFAULT 'OPEN',
    assignee      TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_id INTEGER NOT NULL REFERENCES exceptions(id) ON DELETE CASCADE,
    action       TEXT    NOT NULL,
    from_status  TEXT,
    to_status    TEXT,
    actor        TEXT    NOT NULL,
    note         TEXT,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exceptions_run    ON exceptions(run_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_status ON exceptions(status);
CREATE INDEX IF NOT EXISTS idx_audit_exception   ON audit_log(exception_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
