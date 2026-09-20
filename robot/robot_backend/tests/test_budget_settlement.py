"""Durable, conservative settlement without network calls."""
from concurrent.futures import ProcessPoolExecutor
import json

import pytest

from robot.robot_backend.app.brain import budget

MODEL = 'deepseek/deepseek-v4.1-flash:floor'


def reserve(path, max_calls=100, limit=20):
    return budget.reserve_attempt(str(path), max_calls, limit, model=MODEL,
                                  reservation_usd=0.5)


def state(path):
    return json.loads(path.read_text())


def parallel_reserve(path):
    try:
        return reserve(path, limit=2)
    except budget.BudgetError:
        return None


def parallel_settle(args):
    return budget.settle_attempt(*args)


def test_restart_legacy_and_single_settlement(tmp_path):
    path = tmp_path / 'usage.json'
    path.write_text(json.dumps({'model': MODEL, 'attempts': 29, 'reserved_usd': 14.5}))
    token = reserve(path)
    assert state(path)['total_reserved_usd'] == 15
    assert budget.settle_attempt(str(path), token, 0.00123456789)
    assert state(path)['total_reserved_usd'] == pytest.approx(14.50123456789)
    assert not budget.settle_attempt(str(path), token, 0)
    assert not budget.settle_attempt(str(path), 'historical', 0)
    assert state(path)['models'][MODEL]['attempts'] == 30
    reserve(path)
    assert state(path)['total_reserved_usd'] == pytest.approx(15.00123456789)


@pytest.mark.parametrize('cost', [None, True, -1, '0.001', float('nan'), float('inf'), {}])
def test_missing_invalid_cost_keeps_charge_and_consumes_token(tmp_path, cost):
    path = tmp_path / 'usage.json'
    token = reserve(path)
    assert budget.settle_attempt(str(path), token, cost)
    assert state(path)['total_reserved_usd'] == 0.5
    assert not budget.settle_attempt(str(path), token, 0)


def test_overspend_debits_extra_and_blocks_future_calls(tmp_path):
    path = tmp_path / 'usage.json'
    token = reserve(path, limit=1)
    assert budget.settle_attempt(str(path), token, 1.1)
    assert state(path)['total_reserved_usd'] == 1.1
    with pytest.raises(budget.BudgetError):
        reserve(path, limit=1)


def test_settlement_never_resets_attempt_cap(tmp_path):
    path = tmp_path / 'usage.json'
    token = reserve(path, max_calls=1)
    budget.settle_attempt(str(path), token, 0)
    with pytest.raises(budget.BudgetError):
        reserve(path, max_calls=1)


def test_cross_process_reservations_and_duplicate_settlement(tmp_path):
    path = tmp_path / 'usage.json'
    with ProcessPoolExecutor(max_workers=4) as pool:
        tokens = [token for token in pool.map(parallel_reserve, [str(path)] * 12) if token]
        assert len(tokens) == len(set(tokens)) == 4
        assert state(path)['total_reserved_usd'] == 2
        outcomes = list(pool.map(parallel_settle, [(str(path), tokens[0], 0.01)] * 8))
    assert outcomes.count(True) == 1
    assert state(path)['total_reserved_usd'] == 1.51
    assert state(path)['models'][MODEL]['attempts'] == 4


def test_bounded_pending_expiration_does_not_refund(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, 'MAX_PENDING_RESERVATIONS', 2)
    path = tmp_path / 'usage.json'
    old = reserve(path)
    reserve(path)
    fresh = reserve(path)
    assert len(state(path)['pending_reservations']) == 2
    assert not budget.settle_attempt(str(path), old, 0)
    assert budget.settle_attempt(str(path), fresh, 0.01)
    assert state(path)['total_reserved_usd'] == 1.01


def test_multi_model_legacy_preserves_entire_balance(tmp_path):
    path = tmp_path / 'usage.json'
    other = 'xiaomi/mimo-v2.5:floor'
    path.write_text(json.dumps({'models': {
        MODEL: {'attempts': 29, 'reserved_usd': 14.5},
        other: {'attempts': 5, 'reserved_usd': 1}}, 'total_reserved_usd': 15.5}))
    token = reserve(path)
    budget.settle_attempt(str(path), token, 0.002)
    assert state(path)['total_reserved_usd'] == 15.502
    assert state(path)['models'][other] == {'attempts': 5, 'reserved_usd': 1}
