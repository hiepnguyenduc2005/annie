"""Safe, actionable explanations for first-party inference availability errors."""
import httpx


_REASONS = {
    "Planner provider is disabled":
        "Planning is disabled. Enable a configured planner before resuming.",
    "Vision provider is disabled":
        "Vision is disabled. Enable a configured vision provider before resuming.",
    "Cloud reservation limit reached":
        "The cloud inference allowance cannot cover another call. Review the remaining "
        "budget and attempt limit; explicitly authorize any increase before resuming.",
    "Cloud reservation configuration is invalid":
        "Cloud inference reservation settings are invalid. Correct the configuration before resuming.",
    "Cloud reservation ledger is unavailable or invalid":
        "The cloud inference usage ledger cannot be read or validated. Restore its "
        "availability and integrity without resetting recorded usage before resuming.",
}


def inference_block_reason(exc: Exception) -> str | None:
    """Return fixed advice only for recognized /plan or /infer HTTP 503 errors.

    Exception text, URLs, and arbitrary response content are never returned.
    This function performs no I/O and does not change configuration or budgets.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    if exc.response.status_code != 503 or exc.request.url.path not in {"/plan", "/infer"}:
        return None
    try:
        body = exc.response.json()
    except (ValueError, UnicodeError):
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    return _REASONS.get(detail) if isinstance(detail, str) else None
