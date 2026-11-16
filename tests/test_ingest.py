from datetime import date
from decimal import Decimal

import pytest

from recon.ingest import RowError, parse_money, parse_orders, parse_payments, parse_settlements, to_csv

ORDERS_HEADER = "order_id,customer_id,order_date,amount,currency\n"


def test_parses_valid_orders():
    r = parse_orders(ORDERS_HEADER + "O1,C1,2025-08-01,1499.50,inr\n")
    assert r.ok
    o = r.records[0]
    assert o.amount == Decimal("1499.50") and o.currency == "INR" and o.order_date == date(2025, 8, 1)


def test_reports_missing_columns():
    r = parse_orders("order_id,amount\nO1,10\n")
    assert not r.ok
    assert "missing required column" in r.errors[0]
    assert "customer_id" in r.errors[0]


def test_collects_row_errors_with_line_numbers():
    text = ORDERS_HEADER + "O1,C1,2025-08-01,abc,INR\nO2,C1,not-a-date,10,INR\nO3,C1,2025-08-01,10,INR\n"
    r = parse_orders(text)
    assert len(r.records) == 1
    assert r.errors[0].startswith("orders.csv line 2") and "not a number" in r.errors[0]
    assert r.errors[1].startswith("orders.csv line 3") and "valid date" in r.errors[1]


def test_handles_excel_bom_blank_lines_and_header_case():
    text = "﻿Order_ID,Customer_ID,Order_Date,Amount,Currency\n\nO1,C1,01/08/2025,\"1,250.00\",INR\n\n"
    r = parse_orders(text)
    assert r.ok and r.records[0].amount == Decimal("1250.00")
    assert r.records[0].order_date == date(2025, 8, 1)


def test_empty_required_field():
    r = parse_orders(ORDERS_HEADER + ",C1,2025-08-01,10,INR\n")
    assert "'order_id' is empty" in r.errors[0]


@pytest.mark.parametrize("raw, expected", [("10", "10.00"), ("10.005", "10.00"), (" 7.1 ", "7.10")])
def test_money_is_quantized_to_two_places(raw, expected):
    assert parse_money(raw, "amount") == Decimal(expected)


@pytest.mark.parametrize("raw", ["-5", "", "NaN", "Infinity", "1e"])
def test_rejects_bad_money(raw):
    with pytest.raises(RowError):
        parse_money(raw, "amount")


def test_payment_status_is_validated():
    header = "payment_id,order_id,payment_date,amount,currency,method,status\n"
    r = parse_payments(header + "P1,O1,2025-08-01,10,INR,upi,PENDING\n")
    assert "'status' must be one of" in r.errors[0]
    ok = parse_payments(header + "P1,O1,2025-08-01,10,INR,upi,success\n")
    assert ok.ok and ok.records[0].status == "SUCCESS" and ok.records[0].method == "UPI"


def test_parses_settlements():
    header = "settlement_id,payment_id,settlement_date,gross_amount,fee,net_amount\n"
    r = parse_settlements(header + "S1,P1,2025-08-02,100,2,98\n")
    assert r.ok and r.records[0].net_amount == Decimal("98.00")


def test_to_csv_round_trip():
    text = to_csv([{"a": 1, "b": "x"}, {"a": 2}], ["a", "b"])
    assert text == "a,b\n1,x\n2,\n"
