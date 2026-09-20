"""An explicitly raised cap preserves the existing shared spending ledger."""
import json

import pytest

from robot.robot_backend.app.brain.budget import BudgetError, reserve_attempt
from robot.robot_backend.app.brain.provider import VisionConfig


def test_authorized_cap_preserves_prior_debits_and_blocks_at_new_limit(tmp_path):
    path = tmp_path / 'usage.json'
    model = 'google/gemini-3.8-flash:floor'
    previous = {'version': 2, 'models': {model: {'attempts': 51, 'reserved_usd': 18.200133}},
                'total_reserved_usd': 18.200133, 'pending_reservations': {}}
    path.write_text(json.dumps(previous))
    with pytest.raises(BudgetError, match='limit reached'):
        reserve_attempt(str(path), 1000, 19, model=model, reservation_usd=.8)
    assert json.loads(path.read_text()) == previous
    config = VisionConfig(budget_usd=49)
    reserve_attempt(str(path), 1000, config.budget_usd, model=model, reservation_usd=.8)
    updated = json.loads(path.read_text())
    assert updated['models'][model]['attempts'] == 52
    assert updated['total_reserved_usd'] == pytest.approx(19.000133)
    updated['models'][model]['reserved_usd'] = updated['total_reserved_usd'] = 48.3
    path.write_text(json.dumps(updated))
    with pytest.raises(BudgetError, match='limit reached'):
        reserve_attempt(str(path), 1000, config.budget_usd, model=model, reservation_usd=.8)


@pytest.mark.parametrize('amount', [50.01, float('inf'), float('nan'), 0, -1])
def test_configuration_rejects_caps_outside_current_authorization(amount):
    with pytest.raises(ValueError, match='at most 50'):
        VisionConfig(budget_usd=amount)


def test_default_budget_does_not_increase_implicitly():
    assert VisionConfig().budget_usd == 20
