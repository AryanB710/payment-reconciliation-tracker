"""Synthetic test data with known, injected discrepancies.

`generate()` builds a clean set of orders, payments and settlements, then
injects a fixed number of each discrepancy type into *different* orders. It
returns the exact set of exceptions the engine should find (the ground truth),
so tests can check the engine catches every injected problem and raises no
false alarms.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .ingest import ORDER_COLUMNS, PAYMENT_COLUMNS, SETTLEMENT_COLUMNS, to_csv
from .models import ExceptionType as ET

FEE_RATE = Decimal("0.02")
METHODS = ["UPI", "CARD", "NETBANKING", "WALLET"]


def _money(x) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class SyntheticData:
    orders: list[dict] = field(default_factory=list)
    payments: list[dict] = field(default_factory=list)
    settlements: list[dict] = field(default_factory=list)
    expected: set[tuple] = field(default_factory=set)
    as_of: date = date(2025, 8, 31)
    settlement_window_days: int = 2
    expected_pending: int = 0
    expected_fully_matched: int = 0

    def to_csv(self) -> dict[str, str]:
        return {
            "orders": to_csv(self.orders, ORDER_COLUMNS),
            "payments": to_csv(self.payments, PAYMENT_COLUMNS),
            "settlements": to_csv(self.settlements, SETTLEMENT_COLUMNS),
        }


def generate(n_orders: int = 200, per_type: int = 3, n_pending: int = 4, seed: int = 42,
             start: date = date(2025, 8, 1)) -> SyntheticData:
    rng = random.Random(seed)
    data = SyntheticData(as_of=start + timedelta(days=30))
    pid_n = sid_n = 0

    def next_pid():
        nonlocal pid_n
        pid_n += 1
        return f"PAY{pid_n:06d}"

    def next_sid():
        nonlocal sid_n
        sid_n += 1
        return f"SET{sid_n:06d}"

    def settle(payment: dict) -> dict:
        gross = _money(payment["amount"])
        fee = _money(gross * FEE_RATE)
        pay_date = date.fromisoformat(payment["payment_date"])
        return {
            "settlement_id": next_sid(), "payment_id": payment["payment_id"],
            "settlement_date": (pay_date + timedelta(days=rng.randint(1, 2))).isoformat(),
            "gross_amount": str(gross), "fee": str(fee), "net_amount": str(gross - fee),
        }

    # ---- 1. clean data -----------------------------------------------------
    chains = []  # (order, success payment, settlement) per order
    for i in range(1, n_orders + 1):
        order_date = start + timedelta(days=rng.randint(0, 20))
        amount = _money(rng.uniform(99, 25000))
        order = {"order_id": f"ORD{i:05d}", "customer_id": f"CUST{rng.randint(1, 400):04d}",
                 "order_date": order_date.isoformat(), "amount": str(amount), "currency": "INR"}
        data.orders.append(order)
        pay_date = order_date + timedelta(days=rng.randint(0, 1))
        # ~10% of orders have a failed attempt before the successful one (normal noise)
        if rng.random() < 0.10:
            data.payments.append({"payment_id": next_pid(), "order_id": order["order_id"],
                                  "payment_date": pay_date.isoformat(), "amount": str(amount), "currency": "INR",
                                  "method": rng.choice(METHODS), "status": "FAILED"})
        payment = {"payment_id": next_pid(), "order_id": order["order_id"], "payment_date": pay_date.isoformat(),
                   "amount": str(amount), "currency": "INR", "method": rng.choice(METHODS), "status": "SUCCESS"}
        data.payments.append(payment)
        settlement = settle(payment)
        data.settlements.append(settlement)
        chains.append((order, payment, settlement))

    # ---- 2. inject discrepancies, each into a different order --------------
    injectable = [
        ET.MISSING_PAYMENT, ET.DUPLICATE_CHARGE, ET.PAYMENT_AMOUNT_MISMATCH, ET.CURRENCY_MISMATCH,
        ET.MISSING_SETTLEMENT, ET.SETTLEMENT_AMOUNT_MISMATCH, ET.SETTLEMENT_FEE_MISMATCH,
        ET.DUPLICATE_ORDER, ET.DUPLICATE_PAYMENT_RECORD, ET.DUPLICATE_SETTLEMENT_RECORD,
    ]
    needed = len(injectable) * per_type
    if needed > n_orders:
        raise ValueError(f"need at least {needed} orders to inject {per_type} of each type")
    targets = rng.sample(range(n_orders), needed)
    touched_orders: set[str] = set()

    for t_idx, exc_type in enumerate(injectable):
        for k in range(per_type):
            order, payment, settlement = chains[targets[t_idx * per_type + k]]
            oid, pid, sid = order["order_id"], payment["payment_id"], settlement["settlement_id"]

            if exc_type is ET.MISSING_PAYMENT:
                data.payments.remove(payment)
                data.settlements.remove(settlement)
                data.expected.add((exc_type.value, oid, None, None))
            elif exc_type is ET.DUPLICATE_CHARGE:
                dup_date = date.fromisoformat(payment["payment_date"]) + timedelta(days=1)
                dup = dict(payment, payment_id=next_pid(), payment_date=dup_date.isoformat())
                data.payments.append(dup)
                data.settlements.append(settle(dup))
                data.expected.add((exc_type.value, oid, dup["payment_id"], None))
            elif exc_type is ET.PAYMENT_AMOUNT_MISMATCH:
                new_amount = _money(Decimal(payment["amount"]) - Decimal(rng.randint(1, 500)))
                payment["amount"] = str(new_amount)
                fee = _money(new_amount * FEE_RATE)
                settlement.update(gross_amount=str(new_amount), fee=str(fee), net_amount=str(new_amount - fee))
                data.expected.add((exc_type.value, oid, pid, None))
            elif exc_type is ET.CURRENCY_MISMATCH:
                payment["currency"] = "USD"
                data.expected.add((exc_type.value, oid, pid, None))
            elif exc_type is ET.MISSING_SETTLEMENT:
                data.settlements.remove(settlement)
                data.expected.add((exc_type.value, oid, pid, None))
            elif exc_type is ET.SETTLEMENT_AMOUNT_MISMATCH:
                gross = _money(Decimal(settlement["gross_amount"]) - Decimal("100.00"))
                fee = _money(gross * FEE_RATE)
                settlement.update(gross_amount=str(gross), fee=str(fee), net_amount=str(gross - fee))
                data.expected.add((exc_type.value, oid, pid, sid))
            elif exc_type is ET.SETTLEMENT_FEE_MISMATCH:
                settlement["net_amount"] = str(_money(Decimal(settlement["net_amount"]) - Decimal("10.00")))
                data.expected.add((exc_type.value, oid, pid, sid))
            elif exc_type is ET.DUPLICATE_ORDER:
                data.orders.append(dict(order))
                data.expected.add((exc_type.value, oid, None, None))
            elif exc_type is ET.DUPLICATE_PAYMENT_RECORD:
                data.payments.append(dict(payment))
                data.expected.add((exc_type.value, None, pid, None))
            elif exc_type is ET.DUPLICATE_SETTLEMENT_RECORD:
                data.settlements.append(dict(settlement))
                data.expected.add((exc_type.value, None, None, sid))
                continue  # a re-sent settlement row doesn't break the order's own chain
            touched_orders.add(oid)

    # Orphans: records that don't belong to any order / payment
    for k in range(per_type):
        ghost = {"payment_id": next_pid(), "order_id": f"ORD9{k:04d}",
                 "payment_date": (start + timedelta(days=5)).isoformat(),
                 "amount": str(_money(rng.uniform(100, 5000))), "currency": "INR",
                 "method": "CARD", "status": "SUCCESS"}
        data.payments.append(ghost)
        data.settlements.append(settle(ghost))
        data.expected.add((ET.ORPHAN_PAYMENT.value, ghost["order_id"], ghost["payment_id"], None))

        stray = settle({"payment_id": f"PAY9{k:05d}", "amount": "750.00",
                        "payment_date": (start + timedelta(days=6)).isoformat()})
        data.settlements.append(stray)
        data.expected.add((ET.ORPHAN_SETTLEMENT.value, None, stray["payment_id"], stray["settlement_id"]))

    # ---- 3. recent orders still inside the settlement window (not errors) --
    recent = data.as_of - timedelta(days=1)
    for k in range(n_pending):
        oid = f"ORD{n_orders + k + 1:05d}"
        amount = str(_money(rng.uniform(99, 5000)))
        data.orders.append({"order_id": oid, "customer_id": "CUST0001", "order_date": recent.isoformat(),
                            "amount": amount, "currency": "INR"})
        data.payments.append({"payment_id": next_pid(), "order_id": oid, "payment_date": recent.isoformat(),
                              "amount": amount, "currency": "INR", "method": "UPI", "status": "SUCCESS"})
    data.expected_pending = n_pending
    data.expected_fully_matched = n_orders - len(touched_orders)

    rng.shuffle(data.orders)
    rng.shuffle(data.payments)
    rng.shuffle(data.settlements)
    return data
