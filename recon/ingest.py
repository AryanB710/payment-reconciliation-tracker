"""CSV parsing and validation.

Every row is validated before it reaches the reconciliation engine. Bad rows
are collected as readable errors (with line numbers) instead of crashing the
whole upload, so an operator can fix the source file.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Callable, Generic, Iterable, TypeVar

from .models import Order, Payment, Settlement

T = TypeVar("T")

ORDER_COLUMNS = ["order_id", "customer_id", "order_date", "amount", "currency"]
PAYMENT_COLUMNS = ["payment_id", "order_id", "payment_date", "amount", "currency", "method", "status"]
SETTLEMENT_COLUMNS = ["settlement_id", "payment_id", "settlement_date", "gross_amount", "fee", "net_amount"]

VALID_PAYMENT_STATUSES = {"SUCCESS", "FAILED", "REFUNDED"}
TWO_PLACES = Decimal("0.01")


class RowError(ValueError):
    pass


@dataclass
class ParseResult(Generic[T]):
    records: list[T] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_money(value: str, column: str, allow_negative: bool = False) -> Decimal:
    raw = (value or "").strip().replace(",", "")
    if not raw:
        raise RowError(f"'{column}' is empty")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise RowError(f"'{column}' is not a number: {value!r}") from None
    if not amount.is_finite():
        raise RowError(f"'{column}' is not a finite number: {value!r}")
    if amount < 0 and not allow_negative:
        raise RowError(f"'{column}' cannot be negative: {value!r}")
    return amount.quantize(TWO_PLACES)


def parse_date(value: str, column: str) -> date:
    raw = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise RowError(f"'{column}' is not a valid date (use YYYY-MM-DD): {value!r}")


def require(value: str, column: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise RowError(f"'{column}' is empty")
    return cleaned


def _read_rows(text: str, required: list[str], label: str) -> tuple[list[tuple[int, dict]], list[str]]:
    # Strip a UTF-8 BOM that Excel adds when saving CSVs.
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in required if c not in headers]
    if missing:
        return [], [f"{label}: missing required column(s): {', '.join(missing)}"]
    reader.fieldnames = headers
    rows = []
    for line_no, row in enumerate(reader, start=2):  # line 1 is the header
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # skip blank lines
        rows.append((line_no, row))
    return rows, []


def _parse(text: str, required: list[str], label: str, build: Callable[[dict], T]) -> ParseResult[T]:
    result: ParseResult[T] = ParseResult()
    rows, header_errors = _read_rows(text, required, label)
    if header_errors:
        result.errors.extend(header_errors)
        return result
    for line_no, row in rows:
        try:
            result.records.append(build(row))
        except RowError as exc:
            result.errors.append(f"{label} line {line_no}: {exc}")
    return result


def _build_order(row: dict) -> Order:
    return Order(
        order_id=require(row["order_id"], "order_id"),
        customer_id=require(row["customer_id"], "customer_id"),
        order_date=parse_date(row["order_date"], "order_date"),
        amount=parse_money(row["amount"], "amount"),
        currency=require(row["currency"], "currency").upper(),
    )


def _build_payment(row: dict) -> Payment:
    status = require(row["status"], "status").upper()
    if status not in VALID_PAYMENT_STATUSES:
        raise RowError(f"'status' must be one of {sorted(VALID_PAYMENT_STATUSES)}: {row['status']!r}")
    return Payment(
        payment_id=require(row["payment_id"], "payment_id"),
        order_id=require(row["order_id"], "order_id"),
        payment_date=parse_date(row["payment_date"], "payment_date"),
        amount=parse_money(row["amount"], "amount"),
        currency=require(row["currency"], "currency").upper(),
        method=require(row["method"], "method").upper(),
        status=status,
    )


def _build_settlement(row: dict) -> Settlement:
    return Settlement(
        settlement_id=require(row["settlement_id"], "settlement_id"),
        payment_id=require(row["payment_id"], "payment_id"),
        settlement_date=parse_date(row["settlement_date"], "settlement_date"),
        gross_amount=parse_money(row["gross_amount"], "gross_amount"),
        fee=parse_money(row["fee"], "fee"),
        net_amount=parse_money(row["net_amount"], "net_amount"),
    )


def parse_orders(text: str) -> ParseResult[Order]:
    return _parse(text, ORDER_COLUMNS, "orders.csv", _build_order)


def parse_payments(text: str) -> ParseResult[Payment]:
    return _parse(text, PAYMENT_COLUMNS, "payments.csv", _build_payment)


def parse_settlements(text: str) -> ParseResult[Settlement]:
    return _parse(text, SETTLEMENT_COLUMNS, "settlements.csv", _build_settlement)


def to_csv(rows: Iterable[dict], columns: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return buffer.getvalue()
