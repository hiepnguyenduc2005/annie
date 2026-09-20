"""Pure error classification tests: no services or provider calls."""
import httpx
import pytest

from robot.simulation.inference_failures import inference_block_reason


MESSAGES = [
    "Planner provider is disabled",
    "Vision provider is disabled",
    "Cloud reservation limit reached",
    "Cloud reservation configuration is invalid",
    "Cloud reservation ledger is unavailable or invalid",
]


def failure(body=None, *, path="/plan", status=503, content=None):
    request = httpx.Request("POST", "http://mock" + path)
    kwargs = {"content": content} if content is not None else {"json": body}
    response = httpx.Response(status, request=request, **kwargs)
    return httpx.HTTPStatusError("private exception text", request=request, response=response)


@pytest.mark.parametrize("message", MESSAGES)
@pytest.mark.parametrize("path", ["/plan", "/infer"])
def test_known_first_party_error_returns_fixed_actionable_advice(message, path):
    reason = inference_block_reason(failure({"detail": message}, path=path))
    assert isinstance(reason, str) and "before resuming" in reason
    assert "private" not in reason and "http" not in reason
    assert reason == inference_block_reason(failure({"detail": message, "secret": "private"}, path=path))


@pytest.mark.parametrize("body", [None, [], "private", 42, {}, {"detail": None},
    {"detail": []}, {"detail": {}}, {"detail": 503}, {"detail": "private provider response"},
    {"detail": "Cloud reservation limit reached: private"}])
def test_malformed_or_unrecognized_body_is_not_exposed(body):
    assert inference_block_reason(failure(body)) is None


@pytest.mark.parametrize("content", [b"not json: private", b"", b"\xff", b"<html>private</html>"])
def test_invalid_json_is_not_exposed(content):
    assert inference_block_reason(failure(content=content)) is None


@pytest.mark.parametrize("status", [400, 401, 429, 500, 502, 504])
def test_other_status_is_not_classified(status):
    assert inference_block_reason(failure({"detail": MESSAGES[2]}, status=status)) is None


@pytest.mark.parametrize("path", ["/say", "/commands", "/other/plan", "/plan/", "/inference"])
def test_unrelated_path_is_not_classified(path):
    assert inference_block_reason(failure({"detail": MESSAGES[2]}, path=path)) is None


@pytest.mark.parametrize("exc", [ValueError("private"), httpx.ConnectError("private"), None])
def test_other_exception_is_not_classified(exc):
    assert inference_block_reason(exc) is None
