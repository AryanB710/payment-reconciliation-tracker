"""Core data models for the reconciliation engine.

Money is always stored as ``Decimal`` (never float) so that amounts such as
0.1 + 0.2 compare exactly and rounding is explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum


class ExceptionType(str, Enum):
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    DUPLICATE_PAYMENT_RECORD = "DUPLICATE_PAYMENT_RECORD"
    DUPLICATE_SETTLEMENT_RECORD = "DUPLICATE_SETTLEMENT_RECORD"
    DUPLICATE_CHARGE = "DUPLICATE_CHARGE"
    MISSING_PAYMENT = "MISSING_PAYMENT"
    ORPHAN_PAYMENT = "ORPHAN_PAYMENT"
    PAYMENT_AMOUNT_MISMATCH = "PAYMENT_AMOUNT_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    MISSING_SETTLEMENT = "MISSING_SETTLEMENT"
    ORPHAN_SETTLEMENT = "ORPHAN_SETTLEMENT"
    SETTLEMENT_AMOUNT_MISMATCH = "SETTLEMENT_AMOUNT_MISMATCH"
    SETTLEMENT_FEE_MISMATCH = "SETTLEMENT_FEE_MISMATCH"


class Severity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# How urgent each exception type is. Money leaving the business without a
# matching order (duplicate charges, orphan payments) is the most serious.
SEVERITY: dict[ExceptionType, Severity] = {
    ExceptionType.DUPLICATE_CHARGE: Severity.HIGH,
    ExceptionType.ORPHAN_PAYMENT: Severity.HIGH,
    ExceptionType.PAYMENT_AMOUNT_MISMATCH: Severity.HIGH,
    ExceptionType.MISSING_SETTLEMENT: Severity.HIGH,
    ExceptionType.SETTLEMENT_AMOUNT_MISMATCH: Severity.HIGH,
    ExceptionType.CURRENCY_MISMATCH: Severity.MEDIUM,
    ExceptionType.ORPHAN_SETTLEMENT: Severity.MEDIUM,
    ExceptionType.SETTLEMENT_FEE_MISMATCH: Severity.MEDIUM,
    ExceptionType.MISSING_PAYMENT: Severity.MEDIUM,
    ExceptionType.DUPLICATE_ORDER: Severity.LOW,
    ExceptionType.DUPLICATE_PAYMENT_RECORD: Severity.LOW,
    ExceptionType.DUPLICATE_SETTLEMENT_RECORD: Severity.LOW,
}


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    order_date: date
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class Payment:
    payment_id: str
    order_id: str
    payment_date: date
    amount: Decimal
    currency: str
    method: str
    status: str  # SUCCESS | FAILED | REFUNDED

    @property
    def is_success(self) -> bool:
        return self.status == "SUCCESS"


@dataclass(frozen=True)
class Settlement:
    settlement_id: str
    payment_id: str
    settlement_date: date
    gross_amount: Decimal
    fee: Decimal
    net_amount: Decimal


@dataclass
class ReconException:
    type: ExceptionType
    message: str
    order_id: str | None = None
    payment_id: str | None = None
    settlement_id: str | None = None
    expected: Decimal | None = None
    actual: Decimal | None = None

    @property
    def severity(self) -> Severity:
        return SEVERITY[self.type]

    @property
    def difference(self) -> Decimal | None:
        if self.expected is None or self.actual is None:
            return None
        return self.actual - self.expected

    def key(self) -> tuple:
        """Identity used to compare engine output with known ground truth."""
        return (self.type.value, self.order_id, self.payment_id, self.settlement_id)


@dataclass
class ReconResult:
    exceptions: list[ReconException] = field(default_factory=list)
    total_orders: int = 0
    total_payments: int = 0
    total_settlements: int = 0
    fully_matched_orders: int = 0
    pending_settlements: int = 0

    @property
    def match_rate(self) -> float:
        if self.total_orders == 0:
            return 0.0
        return round(100 * self.fully_matched_orders / self.total_orders, 2)

    def counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for exc in self.exceptions:
            counts[exc.type.value] = counts.get(exc.type.value, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
