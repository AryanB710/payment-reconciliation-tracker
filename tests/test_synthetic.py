"""End-to-end accuracy: the engine must find exactly the discrepancies the
generator injected, no more and no less, across many random datasets."""

import pytest

from recon import ReconConfig, reconcile
from recon.ingest import parse_orders, parse_payments, parse_settlements
from recon.models import ExceptionType
from recon.synthetic import generate


def run(data):
    csvs = data.to_csv()
    o, p, s = parse_orders(csvs["orders"]), parse_payments(csvs["payments"]), parse_settlements(csvs["settlements"])
    assert o.ok and p.ok and s.ok
    cfg = ReconConfig(as_of=data.as_of, settlement_window_days=data.settlement_window_days)
    return reconcile(o.records, p.records, s.records, cfg)


@pytest.mark.parametrize("seed", range(10))
def test_engine_finds_exactly_the_injected_discrepancies(seed):
    data = generate(n_orders=150, per_type=3, seed=seed)
    result = run(data)
    found = {e.key() for e in result.exceptions}
    missed = data.expected - found
    false_alarms = found - data.expected
    assert not missed, f"missed: {sorted(missed)}"
    assert not false_alarms, f"false alarms: {sorted(false_alarms)}"


def test_every_exception_type_is_exercised():
    data = generate()
    injected_types = {key[0] for key in data.expected}
    assert injected_types == {t.value for t in ExceptionType}


def test_match_rate_and_pending_counts():
    data = generate(n_orders=200, per_type=2, n_pending=5, seed=7)
    result = run(data)
    assert result.pending_settlements == data.expected_pending
    assert result.fully_matched_orders == data.expected_fully_matched


def test_generator_is_deterministic():
    assert generate(seed=3).to_csv() == generate(seed=3).to_csv()


def test_generator_rejects_too_few_orders():
    with pytest.raises(ValueError):
        generate(n_orders=5, per_type=3)


def test_scales_to_larger_files():
    data = generate(n_orders=5000, per_type=10, seed=1)
    result = run(data)
    assert {e.key() for e in result.exceptions} == data.expected
