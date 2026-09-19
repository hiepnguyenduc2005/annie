"""Fail-closed reservation ledger for explicitly approved OpenRouter models."""
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile

APPROVED_VISION_MODELS = frozenset({
    'qwen/qwen3-vl-32b-instruct:floor',
    'deepseek/deepseek-v4.1-flash:floor',
    'xiaomi/mimo-v2.5:floor',
})
APPROVED_AUDIO_MODELS = frozenset({'xiaomi/mimo-v2.5:floor'})

# Conservative per-attempt reservations cover the full approved model context
# at the enforced price ceilings plus the 512-token output bound, including
# attempts that fail or time out after egress:
# - qwen3-vl: 131072 ctx * $0.11/M + 512 out * $0.42/M < $0.015, reserve $0.02.
# - deepseek-v4.1-flash: 1048576 ctx * $0.31/M + 512 out * $1.21/M < $0.33,
#   reserve $0.50.
# - mimo-v2.5: 1050000 ctx * $0.15/M + 512 out * $0.29/M < $0.16, reserve $0.20.
MODEL_RESERVATION_USD = {
    'qwen/qwen3-vl-32b-instruct:floor': 0.02,
    'deepseek/deepseek-v4.1-flash:floor': 0.50,
    'xiaomi/mimo-v2.5:floor': 0.20,
}


class BudgetError(RuntimeError):
    pass


def reserve_attempt(path: str, max_calls: int, budget_usd: float, *, model: str,
                    reservation_usd: float):
    """Persist one attempt before egress. Failures keep their reservation.

    The ledger is shared across approved models: the budget caps the combined
    total (not per model) and survives restarts. Callers pass the fixed
    reservation for their approved model from MODEL_RESERVATION_USD.
    """
    if (not isinstance(model, str) or model not in MODEL_RESERVATION_USD
            or reservation_usd != MODEL_RESERVATION_USD[model]
            or not math.isfinite(budget_usd) or type(max_calls) is not int
            or max_calls < 1):
        raise BudgetError('Cloud reservation configuration is invalid')
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.with_suffix(target.suffix + '.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = json.loads(target.read_text()) if target.exists() else {}
            calls, reserved, entries = _ledger_state(state, model)
            if calls >= max_calls or reserved + reservation_usd > budget_usd + 1e-9:
                raise BudgetError('Cloud reservation limit reached')
            calls += 1
            reserved += reservation_usd
            entries[model] = {'attempts': calls,
                              'reserved_usd': round(calls * reservation_usd, 2)}
            state = {'models': entries, 'total_reserved_usd': round(reserved, 2)}
            fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.vision-usage-')
            try:
                with os.fdopen(fd, 'w') as output:
                    json.dump(state, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    except BudgetError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise BudgetError('Cloud reservation ledger is unavailable or invalid') from None


def _ledger_state(state, model):
    """Validate the shared ledger and return (calls, total, entries).

    Accepts legacy single-model files. Every entry must name a known approved
    model and carry reserved_usd consistent with attempts * its fixed
    reservation; the reported total must equal the sum of the entries.
    """
    if state == {}:
        return 0, 0.0, {}
    if not isinstance(state, dict):
        raise ValueError('Invalid ledger')
    entries = state.get('models')
    if entries is None and isinstance(state.get('model'), str):
        entries = {state['model']: {'attempts': state.get('attempts'),
                                    'reserved_usd': state.get('reserved_usd')}}
    if not isinstance(entries, dict):
        raise ValueError('Invalid ledger')
    total = 0.0
    normalized = {}
    for name, entry in entries.items():
        if (not isinstance(name, str) or name not in MODEL_RESERVATION_USD
                or not isinstance(entry, dict)):
            raise ValueError('Invalid ledger')
        attempts = entry.get('attempts')
        reserved = entry.get('reserved_usd')
        if reserved is None:
            reserved = attempts * MODEL_RESERVATION_USD[name]
        if (type(attempts) is not int or attempts < 0
                or isinstance(reserved, bool) or not isinstance(reserved, (int, float))
                or not math.isfinite(reserved)
                or abs(reserved - attempts * MODEL_RESERVATION_USD[name]) > 1e-8):
            raise ValueError('Invalid ledger')
        normalized[name] = {'attempts': attempts, 'reserved_usd': float(reserved)}
        total += float(reserved)
    reported = state.get('total_reserved_usd')
    if reported is not None and (isinstance(reported, bool)
                                 or not isinstance(reported, (int, float))
                                 or not math.isfinite(reported)
                                 or abs(reported - total) > 1e-8):
        raise ValueError('Invalid ledger')
    calls = normalized[model]['attempts'] if model in normalized else 0
    return calls, total, normalized
