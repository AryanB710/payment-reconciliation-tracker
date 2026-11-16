"""Three-way reconciliation: orders <-> payments <-> settlements.

The engine is pure Python with no database or web dependencies, so every rule
can be unit-tested in isolation.

Matching rules
--------------
1. Duplicate records    same ID appears twice in one file.
2. Duplicate charge     an order has more than one SUCCESS payment.
3. Missing payment      an order has no SUCCESS payment.
4. Orphan payment       a SUCCESS payment points to an order that doesn't exist.
5. Currency mismatch    payment currency differs from order currency.
6. Amount mismatch      payment amount differs from order amount (beyond tolerance).
7. Missing settlement   a SUCCESS payment has no settlement after the settlement
                        window (e.g. T+2 days). Younger payments are "pending".
8. Orphan settlement    a settlement points to an unknown or non-successful payment.
9. Settlement mismatch  settlement gross amount differs from the payment amount.
10. Fee mismatch        gross - fee != net on the settlement line.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, TypeVar

from .models import (
    ExceptionType as ET,
    Order,
    Payment,
    ReconException,
    ReconResult,
    Settlement,
)

T = TypeVar("T")


@dataclass(frozen=True)
class ReconConfig:
    settlement_window_days: int = 2
    amount_tolerance: Decimal = Decimal("0.00")
    as_of: date | None = None  # defaults to the latest date in the data


def _dedupe(records: Iterable[T], key: str, exc_type: ET, label: str) -> tuple[dict[str, T], list[ReconException]]:
    """Keep the first record per ID and report every repeat."""
    unique: dict[str, T] = {}
    exceptions: list[ReconException] = []
    for rec in records:
        rec_id = getattr(rec, key)
        if rec_id in unique:
            exceptions.append(
                ReconException(
                    type=exc_type,
                    message=f"{label} {rec_id} appears more than once in the file",
                    **{key: rec_id},
                )
            )
        else:
            unique[rec_id] = rec
    return unique, exceptions


def _latest_date(orders, payments, settlements) -> date:
    dates = [o.order_date for o in orders]
    dates += [p.payment_date for p in payments]
    dates += [s.settlement_date for s in settlements]
    return max(dates) if dates else date.today()


def reconcile(
    orders: list[Order],
    payments: list[Payment],
    settlements: list[Settlement],
    config: ReconConfig | None = None,
) -> ReconResult:
    config = config or ReconConfig()
    as_of = config.as_of or _latest_date(orders, payments, settlements)
    tol = config.amount_tolerance
    exceptions: list[ReconException] = []

    # 1. Duplicate records inside each file
    order_map, dup = _dedupe(orders, "order_id", ET.DUPLICATE_ORDER, "Order")
    exceptions += dup
    payment_map, dup = _dedupe(payments, "payment_id", ET.DUPLICATE_PAYMENT_RECORD, "Payment")
    exceptions += dup
    settlement_map, dup = _dedupe(settlements, "settlement_id", ET.DUPLICATE_SETTLEMENT_RECORD, "Settlement")
    exceptions += dup

    successful = [p for p in payment_map.values() if p.is_success]

    # Group successful payments by order, oldest first, so the first one is the
    # "real" charge and any later ones are duplicates.
    by_order: dict[str, list[Payment]] = defaultdict(list)
    for p in sorted(successful, key=lambda p: (p.payment_date, p.payment_id)):
        by_order[p.order_id].append(p)

    # 4. Orphan payments
    for order_id, pays in by_order.items():
        if order_id not in order_map:
            for p in pays:
                exceptions.append(ReconException(
                    type=ET.ORPHAN_PAYMENT,
                    message=f"Payment {p.payment_id} references unknown order {order_id}",
                    order_id=order_id, payment_id=p.payment_id, actual=p.amount,
                ))

    for order in order_map.values():
        pays = by_order.get(order.order_id, [])

        # 3. Missing payment
        if not pays:
            exceptions.append(ReconException(
                type=ET.MISSING_PAYMENT,
                message=f"Order {order.order_id} has no successful payment",
                order_id=order.order_id, expected=order.amount, actual=Decimal("0.00"),
            ))
            continue

        primary, extras = pays[0], pays[1:]

        # 2. Duplicate charges
        for extra in extras:
            exceptions.append(ReconException(
                type=ET.DUPLICATE_CHARGE,
                message=(f"Order {order.order_id} was charged again by payment {extra.payment_id} "
                         f"(first charge: {primary.payment_id})"),
                order_id=order.order_id, payment_id=extra.payment_id,
                expected=Decimal("0.00"), actual=extra.amount,
            ))

        # 5. Currency mismatch (amounts in different currencies can't be compared)
        if primary.currency != order.currency:
            exceptions.append(ReconException(
                type=ET.CURRENCY_MISMATCH,
                message=(f"Order {order.order_id} is in {order.currency} but payment "
                         f"{primary.payment_id} is in {primary.currency}"),
                order_id=order.order_id, payment_id=primary.payment_id,
            ))
        # 6. Amount mismatch
        elif abs(primary.amount - order.amount) > tol:
            exceptions.append(ReconException(
                type=ET.PAYMENT_AMOUNT_MISMATCH,
                message=(f"Payment {primary.payment_id} is {primary.amount} but order "
                         f"{order.order_id} is {order.amount}"),
                order_id=order.order_id, payment_id=primary.payment_id,
                expected=order.amount, actual=primary.amount,
            ))

    # Settlements, grouped by payment
    settlements_by_payment: dict[str, list[Settlement]] = defaultdict(list)
    for s in sorted(settlement_map.values(), key=lambda s: (s.settlement_date, s.settlement_id)):
        settlements_by_payment[s.payment_id].append(s)

    successful_ids = {p.payment_id for p in successful}

    # 8. Orphan settlements
    for payment_id, setts in settlements_by_payment.items():
        if payment_id not in successful_ids:
            reason = "unknown payment" if payment_id not in payment_map else (
                f"payment with status {payment_map[payment_id].status}")
            for s in setts:
                exceptions.append(ReconException(
                    type=ET.ORPHAN_SETTLEMENT,
                    message=f"Settlement {s.settlement_id} references {reason} {payment_id}",
                    payment_id=payment_id, settlement_id=s.settlement_id, actual=s.gross_amount,
                ))

    pending = 0
    pending_orders: set[str] = set()
    deadline = timedelta(days=config.settlement_window_days)
    for p in successful:
        setts = settlements_by_payment.get(p.payment_id, [])

        # 7. Missing settlement (or still inside the settlement window)
        if not setts:
            if p.payment_date + deadline >= as_of:
                pending += 1
                pending_orders.add(p.order_id)
            else:
                exceptions.append(ReconException(
                    type=ET.MISSING_SETTLEMENT,
                    message=(f"Payment {p.payment_id} from {p.payment_date} has not settled "
                             f"within T+{config.settlement_window_days} days"),
                    order_id=p.order_id, payment_id=p.payment_id,
                    expected=p.amount, actual=Decimal("0.00"),
                ))
            continue

        for s in setts:
            # 9. Settlement gross must equal what the customer paid
            if abs(s.gross_amount - p.amount) > tol:
                exceptions.append(ReconException(
                    type=ET.SETTLEMENT_AMOUNT_MISMATCH,
                    message=(f"Settlement {s.settlement_id} gross {s.gross_amount} does not match "
                             f"payment {p.payment_id} amount {p.amount}"),
                    order_id=p.order_id, payment_id=p.payment_id, settlement_id=s.settlement_id,
                    expected=p.amount, actual=s.gross_amount,
                ))
            # 10. Internal arithmetic on the settlement line
            if s.gross_amount - s.fee != s.net_amount:
                exceptions.append(ReconException(
                    type=ET.SETTLEMENT_FEE_MISMATCH,
                    message=(f"Settlement {s.settlement_id}: gross {s.gross_amount} - fee {s.fee} "
                             f"!= net {s.net_amount}"),
                    order_id=p.order_id, payment_id=p.payment_id, settlement_id=s.settlement_id,
                    expected=s.gross_amount - s.fee, actual=s.net_amount,
                ))

    # Fully matched = known order with no exceptions anywhere in its chain
    # and a completed settlement.
    payment_to_order = {p.payment_id: p.order_id for p in payment_map.values()}
    problem_orders: set[str] = set(pending_orders)
    for exc in exceptions:
        if exc.order_id:
            problem_orders.add(exc.order_id)
        elif exc.payment_id and exc.payment_id in payment_to_order:
            problem_orders.add(payment_to_order[exc.payment_id])
    fully_matched = sum(1 for oid in order_map if oid not in problem_orders)

    return ReconResult(
        exceptions=exceptions,
        total_orders=len(order_map),
        total_payments=len(payment_map),
        total_settlements=len(settlement_map),
        fully_matched_orders=fully_matched,
        pending_settlements=pending,
    )
