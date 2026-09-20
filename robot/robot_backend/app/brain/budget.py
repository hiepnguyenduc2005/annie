"""Fail-closed reservation ledger for explicitly approved OpenRouter models."""
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import uuid

MAX_PENDING_RESERVATIONS = 1024

APPROVED_VISION_MODELS = frozenset({
    'google/gemini-3.8-flash:floor',
    'google/gemini-2.5-flash-lite:floor',
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
    # 1,048,576 context * $0.76/M + 512 output * $3.76/M < $0.80.
    'google/gemini-3.8-flash:floor': 0.80,
    # 1,048,576 context * $0.11/M + 512 output * $0.41/M < $0.12.
    'google/gemini-2.5-flash-lite:floor': 0.12,
    'qwen/qwen3-vl-32b-instruct:floor': 0.02,
    'deepseek/deepseek-v4.1-flash:floor': 0.50,
    'xiaomi/mimo-v2.5:floor': 0.20,
}


class BudgetError(RuntimeError):
    pass


def reserve_attempt(path: str, max_calls: int, budget_usd: float, *, model: str,
                    reservation_usd: float) -> str:
    """Persist one attempt before egress and return its opaque settlement ID.

    Existing callers may ignore the return value. Failures keep their reservation.
    Lifetime attempt caps are never reset or refunded by settlement.

    The ledger is shared across approved models: the budget caps the combined
    total (not per model) and survives restarts. Callers pass the fixed
    reservation for their approved model from MODEL_RESERVATION_USD.
    """
    if (not isinstance(model, str) or model not in MODEL_RESERVATION_USD
            or reservation_usd != MODEL_RESERVATION_USD[model]
            or type(budget_usd) not in (int, float)
            or not math.isfinite(budget_usd) or budget_usd <= 0
            or type(max_calls) is not int
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
            pending = dict(state.get('pending_reservations', {}))
            # Forgotten/uncertain requests retain their debit forever. Expiring
            # their settlement capability bounds storage without freeing money.
            if len(pending) >= MAX_PENDING_RESERVATIONS:
                del pending[next(iter(pending))]
            reservation_id = uuid.uuid4().hex
            pending[reservation_id] = model
            entries[model] = {'attempts': calls + 1,
                              'reserved_usd': round(entries.get(model, {}).get(
                                  'reserved_usd', 0) + reservation_usd, 12)}
            _write_state(target, entries, pending)
            return reservation_id
    except BudgetError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise BudgetError('Cloud reservation ledger is unavailable or invalid') from None


def _write_state(target, entries, pending):
    state = {'version': 2, 'models': entries,
             'total_reserved_usd': round(sum(e['reserved_usd'] for e in entries.values()), 12),
             'pending_reservations': pending}
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.vision-usage-')
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(state, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def settle_attempt(path: str, reservation_id: str, actual_cost_usd=None) -> bool:
    """Settle one new request once using trusted successful provider usage.

    Return False for unknown, expired, or already consumed IDs. Missing/invalid
    cost consumes the ID but retains the full allowance. Valid costs above the
    allowance debit the excess, even beyond the budget, blocking future calls.
    Callers must never supply estimated costs or costs from uncertain responses.
    """
    if not isinstance(reservation_id, str) or not reservation_id:
        return False
    target = Path(path)
    try:
        with target.with_suffix(target.suffix + '.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if not target.exists():
                return False
            state = json.loads(target.read_text())
            _, _, entries = _ledger_state(state, next(iter(MODEL_RESERVATION_USD)))
            pending = dict(state.get('pending_reservations', {}))
            model = pending.pop(reservation_id, None)
            if model is None:
                return False
            if (type(actual_cost_usd) in (int, float)
                    and math.isfinite(actual_cost_usd) and actual_cost_usd >= 0):
                entries[model]['reserved_usd'] = round(
                    entries[model]['reserved_usd'] - MODEL_RESERVATION_USD[model]
                    + actual_cost_usd, 12)
            _write_state(target, entries, pending)
            return True
    except BudgetError:
        raise
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        raise BudgetError('Cloud reservation ledger is unavailable or invalid') from None


def _ledger_state(state, model):
    """Validate the shared ledger and return (calls, total, entries).

    Legacy entries must equal attempts times their fixed allowance. Version 2
    stores settled charges plus outstanding allowances under the compatible
    reserved_usd field. Totals and outstanding capabilities remain consistent;
    migration gives no settlement capability to historical charges.
    """
    if state == {}:
        return 0, 0.0, {}
    if not isinstance(state, dict):
        raise ValueError('Invalid ledger')
    version = state.get('version', 1)
    if type(version) is not int or version not in (1, 2):
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
        if reserved is None and version == 1 and type(attempts) is int:
            reserved = attempts * MODEL_RESERVATION_USD[name]
        if (type(attempts) is not int or attempts < 0
                or isinstance(reserved, bool) or not isinstance(reserved, (int, float))
                or not math.isfinite(reserved)
                or reserved < 0
                or (version == 1 and abs(reserved - attempts * MODEL_RESERVATION_USD[name]) > 1e-8)):
            raise ValueError('Invalid ledger')
        normalized[name] = {'attempts': attempts, 'reserved_usd': float(reserved)}
        total += float(reserved)
    reported = state.get('total_reserved_usd')
    if version == 2 and reported is None:
        raise ValueError('Invalid ledger')
    if reported is not None and (isinstance(reported, bool)
                                 or not isinstance(reported, (int, float))
                                 or not math.isfinite(reported)
                                 or abs(reported - total) > 1e-8):
        raise ValueError('Invalid ledger')
    pending = state.get('pending_reservations', {})
    if (not isinstance(pending, dict) or len(pending) > MAX_PENDING_RESERVATIONS
            or (version == 1 and pending)):
        raise ValueError('Invalid ledger')
    for token, name in pending.items():
        if (not isinstance(token, str) or len(token) != 32
                or any(c not in '0123456789abcdef' for c in token)
                or not isinstance(name, str) or name not in normalized):
            raise ValueError('Invalid ledger')
    for name, entry in normalized.items():
        count = sum(value == name for value in pending.values())
        if (count > entry['attempts']
                or count * MODEL_RESERVATION_USD[name] > entry['reserved_usd'] + 1e-8):
            raise ValueError('Invalid ledger')
    calls = normalized[model]['attempts'] if model in normalized else 0
    return calls, total, normalized
