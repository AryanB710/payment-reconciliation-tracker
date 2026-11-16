"""HTTP routes: upload + run, dashboard, exception workflow, CSV export, JSON API."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for,
)

from recon import ReconConfig, reconcile
from recon.ingest import parse_date, parse_orders, parse_payments, parse_settlements, RowError, to_csv
from recon.models import ExceptionType, Severity

from . import service
from .db import get_db

bp = Blueprint("tracker", __name__)

EXPORT_COLUMNS = ["id", "type", "severity", "status", "assignee", "order_id", "payment_id",
                  "settlement_id", "expected", "actual", "difference", "message", "created_at", "updated_at"]


def _read_upload(field: str) -> str | None:
    f = request.files.get(field)
    if f is None or not f.filename:
        return None
    try:
        return f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return ""


def _run_reconciliation(orders_text: str, payments_text: str, settlements_text: str,
                        window: int, as_of: date | None, label: str):
    """Parse, validate and reconcile. Returns (run_id, errors)."""
    orders, payments, settlements = (parse_orders(orders_text), parse_payments(payments_text),
                                     parse_settlements(settlements_text))
    errors = orders.errors + payments.errors + settlements.errors
    if errors:
        return None, errors
    config = ReconConfig(settlement_window_days=window, as_of=as_of)
    result = reconcile(orders.records, payments.records, settlements.records, config)
    effective_as_of = as_of or max(
        [o.order_date for o in orders.records] + [p.payment_date for p in payments.records]
        + [s.settlement_date for s in settlements.records] or [date.today()]
    )
    run_id = service.save_run(get_db(), result, config, effective_as_of, label)
    return run_id, []


def _parse_options():
    errors = []
    try:
        window = int(request.form.get("settlement_window", "2"))
        if not 0 <= window <= 30:
            raise ValueError
    except ValueError:
        errors.append("Settlement window must be a whole number of days between 0 and 30")
        window = 2
    as_of = None
    raw = (request.form.get("as_of") or "").strip()
    if raw:
        try:
            as_of = parse_date(raw, "as_of")
        except RowError as exc:
            errors.append(str(exc))
    return window, as_of, errors


@bp.get("/")
def index():
    return render_template("index.html", runs=service.list_runs(get_db()))


@bp.post("/runs")
def create_run():
    window, as_of, errors = _parse_options()
    texts = {name: _read_upload(name) for name in ("orders", "payments", "settlements")}
    for name, text in texts.items():
        if text is None:
            errors.append(f"Please upload {name}.csv")
        elif text == "":
            errors.append(f"{name}.csv must be a UTF-8 text CSV file")
    if not errors:
        label = (request.form.get("label") or "").strip() or f"Upload {date.today().isoformat()}"
        run_id, errors = _run_reconciliation(texts["orders"], texts["payments"], texts["settlements"],
                                             window, as_of, label)
        if run_id:
            flash("Reconciliation complete.", "success")
            return redirect(url_for("tracker.run_detail", run_id=run_id))
    for e in errors[:20]:
        flash(e, "error")
    if len(errors) > 20:
        flash(f"…and {len(errors) - 20} more errors", "error")
    return redirect(url_for("tracker.index")), 303


@bp.post("/runs/sample")
def sample_run():
    folder = Path(current_app.config["SAMPLE_DATA_DIR"])
    texts = [(folder / f"{n}.csv").read_text(encoding="utf-8") for n in ("orders", "payments", "settlements")]
    run_id, errors = _run_reconciliation(*texts, window=2, as_of=None, label="Sample data")
    if errors:  # pragma: no cover - bundled data is valid
        for e in errors:
            flash(e, "error")
        return redirect(url_for("tracker.index"))
    flash("Sample reconciliation complete.", "success")
    return redirect(url_for("tracker.run_detail", run_id=run_id))


@bp.get("/runs/<int:run_id>")
def run_detail(run_id: int):
    db = get_db()
    run = service.get_run(db, run_id) or abort(404)
    filters = {k: request.args.get(k) or None for k in ("status", "type", "severity", "q")}
    exceptions = service.list_exceptions(db, run_id, status=filters["status"], type_=filters["type"],
                                         severity=filters["severity"], q=filters["q"])
    return render_template(
        "run.html", run=run, breakdown=service.run_breakdown(db, run_id), exceptions=exceptions,
        filters=filters, statuses=service.STATUSES, types=[t.value for t in ExceptionType],
        severities=[s.value for s in Severity],
    )


@bp.get("/runs/<int:run_id>/export.csv")
def export_run(run_id: int):
    db = get_db()
    service.get_run(db, run_id) or abort(404)
    rows = [dict(r) for r in service.list_exceptions(db, run_id, status=request.args.get("status") or None)]
    return Response(
        to_csv(rows, EXPORT_COLUMNS), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=run_{run_id}_exceptions.csv"},
    )


@bp.get("/exceptions/<int:exception_id>")
def exception_detail(exception_id: int):
    db = get_db()
    exc = service.get_exception(db, exception_id) or abort(404)
    return render_template(
        "exception.html", exc=exc, history=service.audit_history(db, exception_id),
        allowed=sorted(service.TRANSITIONS[exc["status"]]), note_required=service.NOTE_REQUIRED,
    )


def _workflow(exception_id: int, action):
    db = get_db()
    service.get_exception(db, exception_id) or abort(404)
    try:
        action(db)
        flash("Saved.", "success")
    except service.WorkflowError as exc:
        flash(str(exc), "error")
    return redirect(url_for("tracker.exception_detail", exception_id=exception_id))


@bp.post("/exceptions/<int:exception_id>/status")
def update_status(exception_id: int):
    f = request.form
    return _workflow(exception_id, lambda db: service.change_status(
        db, exception_id, f.get("status", ""), f.get("actor", ""), f.get("note", "")))


@bp.post("/exceptions/<int:exception_id>/assign")
def update_assignee(exception_id: int):
    f = request.form
    return _workflow(exception_id, lambda db: service.assign(
        db, exception_id, f.get("assignee", ""), f.get("actor", "")))


@bp.post("/exceptions/<int:exception_id>/comment")
def comment(exception_id: int):
    f = request.form
    return _workflow(exception_id, lambda db: service.add_comment(
        db, exception_id, f.get("actor", ""), f.get("note", "")))


# ---- JSON API -------------------------------------------------------------

@bp.get("/api/health")
def health():
    return jsonify(status="ok")


@bp.get("/api/runs/<int:run_id>")
def api_run(run_id: int):
    db = get_db()
    run = service.get_run(db, run_id)
    if run is None:
        return jsonify(error="run not found"), 404
    breakdown = service.run_breakdown(db, run_id)
    data = dict(run)
    data["match_rate"] = round(100 * run["fully_matched_orders"] / run["total_orders"], 2) if run["total_orders"] else 0.0
    data["exceptions_by_type"] = {r["type"]: r["n"] for r in breakdown["by_type"]}
    data["exceptions_by_status"] = breakdown["by_status"]
    return jsonify(data)
