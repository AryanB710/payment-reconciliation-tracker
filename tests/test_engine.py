"""One focused test per matching rule, on tiny hand-built datasets."""

from datetime import timedelta
from decimal import Decimal

import pytest

from recon import ExceptionType as ET, ReconConfig, reconcile
from recon.models import Severity

from conftest import D, order, payment, settlement

AS_OF = D + timedelta(days=10)
CFG = ReconConfig(as_of=AS_OF)


def types(result):
    return sorted(e.type for e in result.exceptions)


def test_clean_chain_has_no_exceptions_and_full_match():
    r = reconcile([order()], [payment()], [settlement()], CFG)
    assert r.exceptions == []
    assert r.fully_matched_orders == 1
    assert r.match_rate == 100.0


def test_empty_input_is_safe():
    r = reconcile([], [], [])
    assert r.exceptions == [] and r.match_rate == 0.0


def test_missing_payment():
    r = reconcile([order()], [], [], CFG)
    assert types(r) == [ET.MISSING_PAYMENT]
    exc = r.exceptions[0]
    assert exc.expected == Decimal("100.00") and exc.actual == Decimal("0.00")
    assert r.fully_matched_orders == 0


def test_failed_payment_does_not_count_as_paid():
    r = reconcile([order()], [payment(status="FAILED")], [], CFG)
    assert types(r) == [ET.MISSING_PAYMENT]


def test_failed_then_successful_retry_is_clean():
    pays = [payment("P0", status="FAILED"), payment("P1")]
    r = reconcile([order()], pays, [settlement()], CFG)
    assert r.exceptions == []


def test_duplicate_charge_flags_the_later_payment():
    pays = [payment("P1"), payment("P2", day=D + timedelta(days=1))]
    setts = [settlement("S1", "P1"), settlement("S2", "P2")]
    r = reconcile([order()], pays, setts, CFG)
    assert types(r) == [ET.DUPLICATE_CHARGE]
    exc = r.exceptions[0]
    assert exc.payment_id == "P2"
    assert exc.severity is Severity.HIGH
    assert exc.difference == Decimal("100.00")


def test_orphan_payment():
    r = reconcile([], [payment(oid="GHOST")], [settlement()], CFG)
    assert types(r) == [ET.ORPHAN_PAYMENT]
    assert r.exceptions[0].order_id == "GHOST"


def test_amount_mismatch_reports_difference():
    r = reconcile([order()], [payment(amount="90.00")],
                  [settlement(gross="90.00", fee="1.80", net="88.20")], CFG)
    assert types(r) == [ET.PAYMENT_AMOUNT_MISMATCH]
    assert r.exceptions[0].difference == Decimal("-10.00")


def test_amount_within_tolerance_is_accepted():
    cfg = ReconConfig(as_of=AS_OF, amount_tolerance=Decimal("0.50"))
    r = reconcile([order()], [payment(amount="99.60")],
                  [settlement(gross="99.60", fee="2.00", net="97.60")], cfg)
    assert r.exceptions == []


def test_currency_mismatch_skips_amount_comparison():
    r = reconcile([order()], [payment(currency="USD", amount="1.20")],
                  [settlement(gross="1.20", fee="0.02", net="1.18")], CFG)
    assert types(r) == [ET.CURRENCY_MISMATCH]


def test_missing_settlement_after_window():
    r = reconcile([order()], [payment()], [], CFG)
    assert types(r) == [ET.MISSING_SETTLEMENT]
    assert r.pending_settlements == 0


@pytest.mark.parametrize("days_ago, expected_pending", [(0, 1), (2, 1), (3, 0)])
def test_settlement_window_boundary(days_ago, expected_pending):
    """A T+2 window: payments 0-2 days old are pending, 3+ days old are missing."""
    pay_day = AS_OF - timedelta(days=days_ago)
    r = reconcile([order(day=pay_day)], [payment(day=pay_day)], [],
                  ReconConfig(as_of=AS_OF, settlement_window_days=2))
    assert r.pending_settlements == expected_pending
    assert (ET.MISSING_SETTLEMENT in types(r)) == (expected_pending == 0)
    assert r.fully_matched_orders == 0  # pending is not "fully matched" yet


def test_orphan_settlement_unknown_payment():
    r = reconcile([order()], [payment()], [settlement(), settlement("S9", "NOPE")], CFG)
    assert types(r) == [ET.ORPHAN_SETTLEMENT]
    assert "unknown payment" in r.exceptions[0].message


def test_orphan_settlement_for_failed_payment():
    r = reconcile([order()], [payment("P0", status="FAILED"), payment("P1")],
                  [settlement("S0", "P0"), settlement("S1", "P1")], CFG)
    assert types(r) == [ET.ORPHAN_SETTLEMENT]
    assert "FAILED" in r.exceptions[0].message


def test_settlement_amount_mismatch():
    r = reconcile([order()], [payment()], [settlement(gross="80.00", fee="1.60", net="78.40")], CFG)
    assert types(r) == [ET.SETTLEMENT_AMOUNT_MISMATCH]


def test_settlement_fee_arithmetic():
    r = reconcile([order()], [payment()], [settlement(net="97.00")], CFG)
    assert types(r) == [ET.SETTLEMENT_FEE_MISMATCH]
    assert r.exceptions[0].expected == Decimal("98.00")


def test_duplicate_records_in_each_file():
    r = reconcile([order(), order()], [payment(), payment()], [settlement(), settlement()], CFG)
    assert types(r) == sorted([ET.DUPLICATE_ORDER, ET.DUPLICATE_PAYMENT_RECORD, ET.DUPLICATE_SETTLEMENT_RECORD])
    assert r.total_orders == 1 and r.total_payments == 1 and r.total_settlements == 1


def test_decimal_money_has_no_float_drift():
    # 0.1 + 0.2 != 0.3 in floats; with Decimal the chain matches exactly.
    r = reconcile([order(amount="0.30")], [payment(amount="0.30")],
                  [settlement(gross="0.30", fee="0.10", net="0.20")], CFG)
    assert r.exceptions == []


def test_as_of_defaults_to_latest_date_in_data():
    later = D + timedelta(days=30)
    r = reconcile([order(), order("O2", day=later)], [payment(), payment("P2", "O2", day=later)],
                  [settlement(day=D + timedelta(days=1))])
    # P1 is 30 days old relative to the latest record -> missing? No: it has a settlement.
    # P2 is on the latest day -> pending, not missing.
    assert r.exceptions == []
    assert r.pending_settlements == 1


def test_counts_by_type_sorted_by_frequency():
    r = reconcile([order("O1"), order("O2"), order("O3")], [payment("P3", "O3")], [settlement("S3", "P3")], CFG)
    assert r.counts_by_type() == {"MISSING_PAYMENT": 2}
    assert r.match_rate == pytest.approx(33.33)
