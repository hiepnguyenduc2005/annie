"""Tests for the bounded Subconscious team provider."""

import asyncio
import json

import httpx
import pytest

from robot.app_backend.app.subconscious_provider import (
    DEFAULT_MODEL,
    ENDPOINT,
    SubconsciousAPIError,
    SubconsciousInputError,
    run_team,
)


def _completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_parallel_success_with_citation_allowlist():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        assert request.url == httpx.URL(ENDPOINT)
        assert request.headers["Authorization"] == "Bearer test-key"
        assert "tools" not in payload
        assert payload["max_tokens"] <= 512
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        user_text = payload["messages"][-1]["content"]
        if "Team reviews" in user_text:
            return httpx.Response(
                200,
                json=_completion(
                    '{\"answer\": "Motion starts near the door.", '
                    '\"used_frame_ids\": ["frame-1", "frame-zzz", 7]}'
                ),
            )
        return httpx.Response(200, json=_completion(f"Review of: {user_text[:30]}"))

    evidence = [
        {"frame_id": "frame-1", "timestamp": "00:12", "caption": "person near door"},
        {"frame_id": "frame-2", "timestamp": "00:13", "observer": "cam-2", "caption": "empty hall"},
    ]
    result = asyncio.run(
        run_team(
            "Summarize movement",
            evidence,
            api_key="test-key",
            client=_client(handler),
        )
    )

    assert result["answer"] == "Motion starts near the door."
    assert result["used_frame_ids"] == ["frame-1"]
    assert [agent["role"] for agent in result["agents"]] == [
        "evidence analyst",
        "uncertainty reviewer",
    ]
    assert all(agent["summary"] for agent in result["agents"])
    assert result["model"] == DEFAULT_MODEL
    assert result["provider"] == "subconscious"
    assert result["calls_used"] == 3
    assert len(seen) == 3


def test_provider_failure_no_fallback_or_retry():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(500, text="internal provider secret leak")

    with pytest.raises(SubconsciousAPIError) as exc_info:
        asyncio.run(
            run_team(
                "task",
                [{"frame_id": "f1", "caption": "ok"}],
                api_key="test-key",
                client=_client(handler),
            )
        )

    assert calls["count"] == 2
    assert str(exc_info.value) == "Subconscious provider returned status 500"
    assert "secret" not in str(exc_info.value)
    assert "test-key" not in str(exc_info.value)


def test_rejects_raw_media_urls_and_tokens_before_network():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=_completion("x"))

    client = _client(handler)
    bad_inputs = [
        ("task", [{"frame_id": "f1", "jpeg": "AAAA"}]),
        ("task", [{"caption": "see https://example.com/frame.jpg"}]),
        ("task", [{"frame_id": "f1", "caption": "a" * 80}]),
        ("", []),
        ("task", [{"caption": "Bearer sk-abcdefgh12345678"}]),
    ]
    for task, evidence in bad_inputs:
        with pytest.raises(SubconsciousInputError):
            asyncio.run(run_team(task, evidence, api_key="test-key", client=client))
    assert calls["count"] == 0


def test_malformed_and_timeout_errors_are_sanitized():
    def malformed_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(SubconsciousAPIError) as exc_info:
        asyncio.run(
            run_team(
                "task",
                [],
                api_key="test-key",
                client=_client(malformed_handler),
            )
        )
    assert str(exc_info.value) == "Subconscious provider returned malformed data"

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("host details boom")

    with pytest.raises(SubconsciousAPIError) as exc_info:
        asyncio.run(
            run_team(
                "task",
                [],
                api_key="test-key",
                client=_client(timeout_handler),
            )
        )
    assert str(exc_info.value) == "Subconscious request timed out"
    assert "boom" not in str(exc_info.value)


def test_synthesis_without_json_uses_plain_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "Team reviews" in payload["messages"][-1]["content"]:
            return httpx.Response(200, json=_completion("Plain advisory answer."))
        return httpx.Response(200, json=_completion("review"))

    result = asyncio.run(
        run_team(
            "task",
            [{"frame_id": "f1", "caption": "ok"}],
            api_key="test-key",
            client=_client(handler),
        )
    )
    assert result["answer"] == "Plain advisory answer."
    assert result["used_frame_ids"] == []


def test_task_guards_reject_urls_tokens_and_media_schemes():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=_completion("x"))

    client = _client(handler)
    bad_tasks = [
        "Check https://example.com/frame.jpg for motion",
        "Use this key Bearer sk-abcdefgh12345678",
        "Decode data:image/png;base64,AAAA and describe it",
    ]
    for bad_task in bad_tasks:
        with pytest.raises(SubconsciousInputError):
            asyncio.run(run_team(bad_task, [], api_key="test-key", client=client))
    assert calls["count"] == 0


def test_role_failure_drains_sibling_before_returning():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        payload = json.loads(request.content)
        if "evidence analyst" in payload["messages"][0]["content"]:
            return httpx.Response(500, text="down")
        return httpx.Response(200, json=_completion("review ok"))

    client = _client(handler)
    with pytest.raises(SubconsciousAPIError) as exc_info:
        asyncio.run(
            run_team(
                "task",
                [{"frame_id": "f1", "caption": "ok"}],
                api_key="test-key",
                client=client,
            )
        )
    assert calls["count"] == 2
    assert str(exc_info.value) == "Subconscious provider returned status 500"
    assert client.is_closed is False


def test_oversized_provider_output_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("x" * 9000))

    with pytest.raises(SubconsciousAPIError) as exc_info:
        asyncio.run(
            run_team("task", [], api_key="test-key", client=_client(handler))
        )
    assert str(exc_info.value) == "Subconscious provider returned oversized output"


def test_json_synthesis_answer_validation():
    def make_handler(answer_value):
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if "Team reviews" not in payload["messages"][-1]["content"]:
                return httpx.Response(200, json=_completion("review"))
            body = json.dumps({"answer": answer_value, "used_frame_ids": []})
            return httpx.Response(200, json=_completion(body))

        return handler

    for bad_answer in ["", "   "]:
        with pytest.raises(SubconsciousAPIError) as exc_info:
            asyncio.run(
                run_team(
                    "task",
                    [{"frame_id": "f1", "caption": "ok"}],
                    api_key="test-key",
                    client=_client(make_handler(bad_answer)),
                )
            )
        assert str(exc_info.value) == "Subconscious provider returned malformed data"

    def missing_answer_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "Team reviews" not in payload["messages"][-1]["content"]:
            return httpx.Response(200, json=_completion("review"))
        return httpx.Response(200, json=_completion('{"used_frame_ids": ["f1"]}'))

    with pytest.raises(SubconsciousAPIError) as exc_info:
        asyncio.run(
            run_team(
                "task",
                [{"frame_id": "f1", "caption": "ok"}],
                api_key="test-key",
                client=_client(missing_answer_handler),
            )
        )
    assert str(exc_info.value) == "Subconscious provider returned malformed data"
