"""Business logic for storing runs and working exceptions through to resolution."""

from __future__ import annotations

import sqlite3
from datetime import date

from recon import ReconConfig, ReconResult

from .db import now_iso

OPEN, IN_REVIEW, RESOLVED, IGNORED = "OPEN", "IN_REVIEW", "RESOLVED", "IGNORED"
STATUSES = [OPEN, IN_REVIEW, RESOLVED, IGNORED]

# Allowed status changes. Closing an exception (RESOLVED / IGNORED) always
# needs a note so the audit trail explains *why*.
TRANSITIONS: dict[str, set[str]] = {
    OPEN: {IN_REVIEW, RESOLVED, IGNORED},
    IN_REVIEW: {OPEN, RESOLVED, IGNORED},
    RESOLVED: {OPEN},
    IGNORED: {OPEN},
}
NOTE_REQUIRED = {RESOLVED, IGNORED}


class WorkflowError(ValueError):
    pass


def _str(value) -> str | None:
    return None if value is None else str(value)


def save_run(db: sqlite3.Connection, result: ReconResult, config: ReconConfig, as_of: date, label: str) -> int:
    created = now_iso()
    cur = db.execute(
        """INSERT INTO runs (created_at, label, as_of, settlement_window, total_orders, total_payments,
                             total_settlements, fully_matched_orders, pending_settlements, exception_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (created, label, as_of.isoformat(), config.settlement_window_days, result.total_orders,
         result.total_payments, result.total_settlements, result.fully_matched_orders,
         result.pending_settlements, len(result.exceptions)),
    )
    run_id = cur.lastrowid
    for exc in result.exceptions:
        cur = db.execute(
            """INSERT INTO exceptions (run_id, type, severity, message, order_id, payment_id, settlement_id,
                                       expected, actual, difference, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, exc.type.value, exc.severity.value, exc.message, exc.order_id, exc.payment_id,
             exc.settlement_id, _str(exc.expected), _str(exc.actual), _str(exc.difference), OPEN,
             created, created),
        )
        db.execute(
            "INSERT INTO audit_log (exception_id, action, to_status, actor, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (cur.lastrowid, "CREATED", OPEN, "system", f"Detected by reconciliation run #{run_id}", created),
        )
    db.commit()
    return run_id


def list_runs(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT r.*,
                  (SELECT COUNT(*) FROM exceptions e WHERE e.run_id = r.id AND e.status IN ('OPEN','IN_REVIEW')) AS open_count
           FROM runs r ORDER BY r.id DESC"""
    ).fetchall()


def get_run(db: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def run_breakdown(db: sqlite3.Connection, run_id: int) -> dict:
    by_type = db.execute(
        "SELECT type, severity, COUNT(*) AS n FROM exceptions WHERE run_id = ? GROUP BY type, severity ORDER BY n DESC, type",
        (run_id,),
    ).fetchall()
    by_status = {row["status"]: row["n"] for row in db.execute(
        "SELECT status, COUNT(*) AS n FROM exceptions WHERE run_id = ? GROUP BY status", (run_id,))}
    by_severity = {row["severity"]: row["n"] for row in db.execute(
        "SELECT severity, COUNT(*) AS n FROM exceptions WHERE run_id = ? GROUP BY severity", (run_id,))}
    return {"by_type": by_type, "by_status": by_status, "by_severity": by_severity}


def list_exceptions(db: sqlite3.Connection, run_id: int, status: str | None = None,
                    type_: str | None = None, severity: str | None = None, q: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM exceptions WHERE run_id = ?"
    params: list = [run_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if type_:
        sql += " AND type = ?"
        params.append(type_)
    if severity:
        sql += " AND severity = ?"
        params.append(severity)
    if q:
        sql += " AND (order_id LIKE ? OR payment_id LIKE ? OR settlement_id LIKE ? OR message LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like, like]
    sql += " ORDER BY CASE severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, id"
    return db.execute(sql, params).fetchall()


def get_exception(db: sqlite3.Connection, exception_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM exceptions WHERE id = ?", (exception_id,)).fetchone()


def audit_history(db: sqlite3.Connection, exception_id: int) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM audit_log WHERE exception_id = ? ORDER BY id", (exception_id,)
    ).fetchall()


def change_status(db: sqlite3.Connection, exception_id: int, to_status: str, actor: str, note: str = "") -> None:
    exc = get_exception(db, exception_id)
    if exc is None:
        raise WorkflowError(f"Exception {exception_id} not found")
    actor = (actor or "").strip()
    note = (note or "").strip()
    if not actor:
        raise WorkflowError("Your name is required for the audit trail")
    if to_status not in STATUSES:
        raise WorkflowError(f"Unknown status {to_status!r}")
    current = exc["status"]
    if to_status not in TRANSITIONS[current]:
        raise WorkflowError(f"Cannot move from {current} to {to_status}")
    if to_status in NOTE_REQUIRED and not note:
        raise WorkflowError(f"A note is required to mark an exception {to_status}")
    ts = now_iso()
    db.execute("UPDATE exceptions SET status = ?, updated_at = ? WHERE id = ?", (to_status, ts, exception_id))
    action = "REOPENED" if to_status == OPEN else "STATUS_CHANGED"
    db.execute(
        """INSERT INTO audit_log (exception_id, action, from_status, to_status, actor, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (exception_id, action, current, to_status, actor, note or None, ts),
    )
    db.commit()


def assign(db: sqlite3.Connection, exception_id: int, assignee: str, actor: str) -> None:
    exc = get_exception(db, exception_id)
    if exc is None:
        raise WorkflowError(f"Exception {exception_id} not found")
    actor = (actor or "").strip()
    assignee = (assignee or "").strip()
    if not actor:
        raise WorkflowError("Your name is required for the audit trail")
    ts = now_iso()
    db.execute("UPDATE exceptions SET assignee = ?, updated_at = ? WHERE id = ?", (assignee or None, ts, exception_id))
    db.execute(
        "INSERT INTO audit_log (exception_id, action, actor, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (exception_id, "ASSIGNED", actor, f"Assigned to {assignee}" if assignee else "Unassigned", ts),
    )
    db.commit()


def add_comment(db: sqlite3.Connection, exception_id: int, actor: str, note: str) -> None:
    if get_exception(db, exception_id) is None:
        raise WorkflowError(f"Exception {exception_id} not found")
    actor, note = (actor or "").strip(), (note or "").strip()
    if not actor or not note:
        raise WorkflowError("Name and comment are both required")
    db.execute(
        "INSERT INTO audit_log (exception_id, action, actor, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (exception_id, "COMMENT", actor, note, now_iso()),
    )
    db.commit()
