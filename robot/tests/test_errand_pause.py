"""Pause the real FIFO service using fake errands and body acknowledgments."""
import asyncio
import json
import threading
import time
import uuid

import pytest
import httpx

from robot.dog.missions.errand import BodyClient, ErrandError, ErrandService


def dispatch(service, run_id=None):
    run_id = run_id or str(uuid.uuid4())
    payload = {"run_id": run_id, "author_id": "zach", "author_name": "Zach", "text": "Charge your phone", "dispatched_at": 1}
    return run_id, service.handle("POST", "/dispatch", json.dumps(payload).encode(), headers={"X-Internal-Secret": "test-secret"})


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_pause_cancels_active_and_queued_runs_and_only_a_new_request_executes():
    started = []
    cancelled = threading.Event()

    async def runner(run):
        started.append(run["run_id"])
        if len(started) == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return "completed"

    async def stop():
        return {"stop_code": 0}

    service = ErrandService(runner, stop_body=stop, dispatch_secret="test-secret", status=lambda _: None).start()
    try:
        first, _ = dispatch(service)
        wait_for(lambda: started == [first])
        second, receipt = dispatch(service)
        assert receipt[1]["state"] == "queued"
        begin = time.monotonic()
        code, paused = service.handle("POST", "/pause", b"{}", headers={"X-Internal-Secret": "test-secret"})
        assert code == 200 and paused["stop_confirmed"] is True
        assert time.monotonic() - begin < 1
        assert set(paused["cancelled_runs"]) == {first, second}
        assert cancelled.is_set() and started == [first]
        assert service.handle("GET", f"/runs/{first}")[1]["state"] == "cancelled"
        assert service.handle("GET", f"/runs/{second}")[1]["state"] == "cancelled"
        # An idempotent retry must never replay the old command.
        assert dispatch(service, first)[1][1]["state"] == "cancelled"
        third, receipt = dispatch(service)
        assert receipt[0] == 202
        wait_for(lambda: service.runs[third]["state"] == "completed")
        assert started == [first, third] and service.open == 0
        code, again = service.handle("POST", "/pause", b"{}", headers={"X-Internal-Secret": "test-secret"})
        assert code == 200 and again["cancelled_runs"] == []
    finally:
        service.close()


@pytest.mark.parametrize("receipt", [{"cancelled": True}, {"stop_code": None}, {"stop_code": False}, {"stop_code": 1},
                                     {"state": "failed", "stop_code": 0}])
def test_pause_requires_body_ack_not_queue_acceptance(receipt):
    async def stop():
        return receipt

    service = ErrandService(None, stop_body=stop, dispatch_secret="test-secret", status=lambda _: None).start()
    try:
        assert service.handle("POST", "/pause", b"{}")[0] == 401
        code, result = service.handle("POST", "/pause", b"{}", headers={"X-Internal-Secret": "test-secret"})
        assert code == 200 and result["paused"] and not result["stop_confirmed"]
    finally:
        service.close()


def test_new_dispatch_cannot_race_cancellation_cleanup():
    entered = threading.Event()
    release = threading.Event()

    async def runner(run):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            raise

    async def stop():
        return {"stop_code": 0}

    service = ErrandService(runner, stop_body=stop, status=lambda _: None).start()
    try:
        first, _ = dispatch(service)
        wait_for(lambda: service.active is not None)
        future = asyncio.run_coroutine_threadsafe(service._pause(), service.loop)
        assert entered.wait(1)
        assert dispatch(service)[1][0] == 409
        release.set()
        assert future.result(2)["cancelled_runs"] == [first]
        assert service.open == 0
    finally:
        release.set()
        service.close()


def test_body_stop_waits_for_its_correlated_firmware_receipt():
    calls, command_id = [], None

    def handle(request):
        nonlocal command_id
        calls.append(request)
        if request.method == "POST":
            command_id = json.loads(request.content)["command_id"]
            return httpx.Response(202, json={"command_id": command_id, "state": "accepted", "stop_code": None})
        assert request.url.path == f"/command/{command_id}"
        return httpx.Response(200, json={"command_id": command_id, "state": "completed", "stop_code": 0,
                                        "processed_at_ms": 123, "result": {"stop_code": 0}})

    async def check():
        client = BodyClient("http://body", poll_s=0, token="test-body-token", transport=httpx.MockTransport(handle))
        try:
            receipt = await client.stop()
            assert receipt["stop_code"] == 0 and receipt["processed_at_ms"] == 123
            assert len(calls) == 2 and all(r.headers["X-Body-Token"] == "test-body-token" for r in calls)
        finally:
            await client.aclose()
    asyncio.run(check())


def test_body_stop_rejects_an_unrelated_completed_receipt():
    async def check():
        client = BodyClient("http://body", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            "command_id": str(uuid.uuid4()), "state": "completed", "stop_code": 0})))
        try:
            with pytest.raises(ErrandError, match="unrelated"):
                await client.stop()
        finally:
            await client.aclose()
    asyncio.run(check())
