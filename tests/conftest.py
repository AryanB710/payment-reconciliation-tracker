import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from recon.models import Order, Payment, Settlement  # noqa: E402
from tracker import create_app  # noqa: E402


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.sqlite3"), "SECRET_KEY": "test"})
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


# ---- small builders for hand-written engine tests ---------------------------

D = date(2025, 8, 1)


def order(oid="O1", amount="100.00", currency="INR", day=D):
    return Order(oid, "C1", day, Decimal(amount), currency)


def payment(pid="P1", oid="O1", amount="100.00", currency="INR", status="SUCCESS", day=D):
    return Payment(pid, oid, day, Decimal(amount), currency, "UPI", status)


def settlement(sid="S1", pid="P1", gross="100.00", fee="2.00", net="98.00", day=D):
    return Settlement(sid, pid, day, Decimal(gross), Decimal(fee), Decimal(net))
