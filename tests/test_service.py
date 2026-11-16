"""Resolution workflow and audit trail."""

import pytest

from recon import ReconConfig, reconcile
from tracker import service
from tracker.db import connect

from conftest import D, order


@pytest.fixture
def db_with_run(app):
    db = connect(app.config["DATABASE"])
    result = reconcile([order("O1"), order("O2")], [], [], ReconConfig(as_of=D))
    run_id = service.save_run(db, result, ReconConfig(as_of=D), D, "test")
    ids = [r["id"] for r in service.list_exceptions(db, run_id)]
    yield db, run_id, ids
    db.close()


def test_save_run_stores_exceptions_and_creation_audit(db_with_run):
    db, run_id, ids = db_with_run
    run = service.get_run(db, run_id)
    assert run["exception_count"] == 2 and run["total_orders"] == 2
    history = service.audit_history(db, ids[0])
    assert [h["action"] for h in history] == ["CREATED"]
    assert history[0]["actor"] == "system"


def test_full_lifecycle_is_audited(db_with_run):
    db, _, ids = db_with_run
    eid = ids[0]
    service.assign(db, eid, "Priya", actor="Aryan")
    service.change_status(db, eid, service.IN_REVIEW, actor="Priya")
    service.add_comment(db, eid, "Priya", "Customer confirmed payment failed at bank")
    service.change_status(db, eid, service.RESOLVED, actor="Priya", note="Order cancelled")
    service.change_status(db, eid, service.OPEN, actor="Aryan", note="Customer paid later")

    actions = [h["action"] for h in service.audit_history(db, eid)]
    assert actions == ["CREATED", "ASSIGNED", "STATUS_CHANGED", "COMMENT", "STATUS_CHANGED", "REOPENED"]
    exc = service.get_exception(db, eid)
    assert exc["status"] == service.OPEN and exc["assignee"] == "Priya"


@pytest.mark.parametrize("to_status", [service.RESOLVED, service.IGNORED])
def test_closing_requires_a_note(db_with_run, to_status):
    db, _, ids = db_with_run
    with pytest.raises(service.WorkflowError, match="note is required"):
        service.change_status(db, ids[0], to_status, actor="Aryan", note="  ")


def test_actor_is_required(db_with_run):
    db, _, ids = db_with_run
    with pytest.raises(service.WorkflowError, match="name is required"):
        service.change_status(db, ids[0], service.IN_REVIEW, actor="")
    with pytest.raises(service.WorkflowError):
        service.assign(db, ids[0], "Priya", actor="")
    with pytest.raises(service.WorkflowError):
        service.add_comment(db, ids[0], "", "hi")


def test_invalid_transition_is_rejected(db_with_run):
    db, _, ids = db_with_run
    service.change_status(db, ids[0], service.RESOLVED, actor="A", note="done")
    with pytest.raises(service.WorkflowError, match="Cannot move from RESOLVED to IN_REVIEW"):
        service.change_status(db, ids[0], service.IN_REVIEW, actor="A")


def test_unknown_status_and_missing_exception(db_with_run):
    db, _, ids = db_with_run
    with pytest.raises(service.WorkflowError, match="Unknown status"):
        service.change_status(db, ids[0], "DONE", actor="A")
    for fn in (lambda: service.change_status(db, 9999, service.OPEN, "A"),
               lambda: service.assign(db, 9999, "x", "A"),
               lambda: service.add_comment(db, 9999, "A", "x")):
        with pytest.raises(service.WorkflowError, match="not found"):
            fn()


def test_filters_and_breakdown(db_with_run):
    db, run_id, ids = db_with_run
    service.change_status(db, ids[0], service.IN_REVIEW, actor="A")
    assert len(service.list_exceptions(db, run_id, status=service.IN_REVIEW)) == 1
    assert len(service.list_exceptions(db, run_id, type_="MISSING_PAYMENT", severity="MEDIUM")) == 2
    assert [r["order_id"] for r in service.list_exceptions(db, run_id, q="O2")] == ["O2"]
    b = service.run_breakdown(db, run_id)
    assert b["by_status"] == {"OPEN": 1, "IN_REVIEW": 1}
    assert b["by_severity"] == {"MEDIUM": 2}
    assert service.list_runs(db)[0]["open_count"] == 2


def test_unassign(db_with_run):
    db, _, ids = db_with_run
    service.assign(db, ids[0], "Priya", actor="A")
    service.assign(db, ids[0], "", actor="A")
    assert service.get_exception(db, ids[0])["assignee"] is None
    assert service.audit_history(db, ids[0])[-1]["note"] == "Unassigned"
