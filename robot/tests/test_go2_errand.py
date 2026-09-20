"""Tests for the family-message errand relay: fakes only, no hardware, no external network.

The errand core runs against a scripted fake body and a fake app_backend; the HTTP adapters run
against httpx.MockTransport; the HTTP surface is exercised through `ErrandService.handle` with a
fake errand runner (plus one loopback round trip through the stdlib handler shim).
"""
import asyncio
import http.client
import json
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import go2_errand  # noqa: E402
from go2_errand import (AppClient, BodyClient, Errand, ErrandError, ErrandService, interpret_reply,  # noqa: E402
                        make_handler, parse_dispatch, phrase_message)

RUN = "5488e7cb-8d54-4c59-8e02-9b739d694a81"
TEXT = "How are you feeling today?"
LINE = 'Jeanine, it\'s Annie. Zach asked me to pass this along: "How are you feeling today?"'
JEANINE = {"found": True, "track_id": 3, "identity": {"name": "Jeanine", "score": 0.71}, "approached": True, "searched_s": 12.5}
quiet = lambda _text: None  # noqa: E731


class FakeBody:
    """Scripted body_command: result dict or exception per command name; records calls and the event log seen."""

    def __init__(self, log=None, **results):
        self.results = {"find_person": JEANINE, "say": {"played": True}, "listen": {"transcript": "Okay.", "heard": True},
                        "stop": {"stop_code": 0}, **results}
        self.calls, self.seen, self.log = [], {}, log

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if self.log is not None:
            self.seen[name] = [e["kind"] for e in self.log]
        result = self.results[name]
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def names(self):
        return [name for name, _ in self.calls]


class FakeApp:
    """post_event fake: stores accepted events; `fail` maps kind -> how many attempts to fail first."""

    def __init__(self, fail=None, error=None):
        self.posts, self.attempts, self.fail, self.error = [], [], dict(fail or {}), error or httpx.ConnectError("refused")

    async def __call__(self, run_id, kind, payload, at):
        assert type(at) is int and at > 0
        self.attempts.append(kind)
        if self.fail.get(kind, 0) > 0:
            self.fail[kind] -= 1
            raise self.error
        self.posts.append((run_id, kind, payload))
        return True

    @property
    def kinds(self):
        return [kind for _, kind, _ in self.posts]


def run_errand(body, app, *, text=TEXT, log=None):
    errand = Errand(body, app, status=quiet, retry_pause_s=0)
    return asyncio.run(errand.run(RUN, "Zach", text, log=log))


# ---- pure core ----

def test_phrase_is_the_fixed_template_around_the_family_words():
    assert phrase_message("Zach", TEXT) == LINE
    assert phrase_message(" Zach ", "  call\n me   back ") == \
        'Jeanine, it\'s Annie. Zach asked me to pass this along: "call me back"'


def test_phrase_truncates_the_quote_so_the_line_fits_one_say():
    line = phrase_message("Z" * 80, "word " * 400)
    assert len(line) <= 300 and line.endswith('…"') and line.startswith("Jeanine, it's Annie. ZZZ")
    assert len(phrase_message("Zach", "x" * 2000)) == 300


def test_interpret_reply_is_an_exact_phrase_match():
    assert interpret_reply("Okay.") == {"reply": "okay", "mood": "happy", "detail": "Jeanine says okay. 🙂"}
    assert interpret_reply(" Help me! ") == {"reply": "concern", "mood": "worried", "detail": 'Jeanine may need help: "Help me!"'}
    assert interpret_reply("I'm okay, thank you") == {"reply": "unclear", "mood": "neutral",
                                                       "detail": 'Jeanine said: "I\'m okay, thank you"'}
    for nothing in (None, "", "   "):
        assert interpret_reply(nothing) == {"reply": "none", "mood": "neutral", "detail": "Message delivered; no reply heard."}


# ---- errand sequence ----

def test_happy_path_posts_the_exact_event_sequence_and_ends_happy():
    log, app = [], FakeApp()
    body = FakeBody(log=log)
    assert run_errand(body, app, log=log) == "completed"
    assert body.calls == [("find_person", {"name": "Jeanine", "timeout_s": 90, "approach": True}),
                          ("say", {"text": LINE}), ("listen", {"max_s": 10})]  # and no stop on success
    assert app.posts == [
        (RUN, "navigating", {"detail": "Looking for Jeanine."}),
        (RUN, "arrived", {"detail": "Found Jeanine."}),
        (RUN, "speaking", {"text": LINE}),
        (RUN, "listening", {}),
        (RUN, "heard", {"transcript": "Okay."}),
        (RUN, "completed", {"detail": "Jeanine says okay. 🙂", "reply": "okay", "mood": "happy"}),
    ]
    # `speaking` marks real playback: it is raised only after the say command returned.
    assert body.seen == {"find_person": ["navigating"], "say": ["navigating", "arrived"],
                         "listen": ["navigating", "arrived", "speaking", "listening"]}
    assert [e["delivered"] for e in log] == [True] * 6 and all(set(e) == {"kind", "payload", "at", "delivered"} for e in log)


def test_unnamed_person_is_accepted_and_said_so():
    app = FakeApp()
    assert run_errand(FakeBody(find_person={**JEANINE, "identity": None}), app) == "completed"
    assert app.posts[1] == (RUN, "arrived", {"detail": "Found someone; assuming it is Jeanine."})


def test_concern_and_unclear_replies_carry_the_transcript():
    app = FakeApp()
    run_errand(FakeBody(listen={"transcript": "I need help", "heard": True}), app)
    assert app.posts[-1] == (RUN, "completed", {"detail": 'Jeanine may need help: "I need help"', "reply": "concern",
                                                "mood": "worried"})
    app = FakeApp()
    run_errand(FakeBody(listen={"transcript": "Tell him I called", "heard": True}), app)
    assert app.posts[-1] == (RUN, "completed", {"detail": 'Jeanine said: "Tell him I called"', "reply": "unclear",
                                                "mood": "neutral"})


def test_nothing_heard_completes_with_reply_none_and_no_heard_event():
    for listen in ({"transcript": None, "heard": False}, {"transcript": "  ", "heard": True},
                   {"transcript": "okay", "heard": False}):
        app = FakeApp()
        assert run_errand(FakeBody(listen=listen), app) == "completed"
        assert app.kinds == ["navigating", "arrived", "speaking", "listening", "completed"]
        assert app.posts[-1][2] == {"detail": "Message delivered; no reply heard.", "reply": "none", "mood": "neutral"}


def test_not_found_fails_and_sends_the_software_stop_without_speaking():
    body = FakeBody(find_person={"found": False, "track_id": None, "identity": None, "approached": False, "searched_s": 90})
    app = FakeApp()
    assert run_errand(body, app) == "failed"
    assert app.posts == [(RUN, "navigating", {"detail": "Looking for Jeanine."}),
                         (RUN, "failed", {"error": "Could not find Jeanine."})]
    assert body.names == ["find_person", "stop"]


def test_a_person_named_as_someone_else_never_hears_the_message():
    body, app = FakeBody(find_person={**JEANINE, "identity": {"name": "Ellis", "score": 0.8}}), FakeApp()
    assert run_errand(body, app) == "failed"
    assert app.posts[-1] == (RUN, "failed", {"error": "Could not find Jeanine."}) and "say" not in body.names


def test_body_exception_fails_exactly_once_with_safe_text_and_sends_stop():
    body, app = FakeBody(say=RuntimeError("Traceback: token=hunter2 at /Users/x/secret.py")), FakeApp()
    assert run_errand(body, app) == "failed"
    assert app.kinds == ["navigating", "arrived", "failed"] and app.kinds.count("failed") == 1
    assert app.posts[-1][2] == {"error": "Annie hit an unexpected problem (RuntimeError)."}
    assert body.names == ["find_person", "say", "stop"]


def test_errand_error_text_is_passed_on_and_unplayed_audio_is_not_reported_as_spoken():
    app = FakeApp()
    run_errand(FakeBody(find_person=ErrandError("The robot body service is unreachable.")), app)
    assert app.posts[-1] == (RUN, "failed", {"error": "The robot body service is unreachable."})
    body, app = FakeBody(say={"played": False}), FakeApp()
    assert run_errand(body, app) == "failed"
    assert app.kinds == ["navigating", "arrived", "failed"] and body.names == ["find_person", "say", "stop"]
    assert app.posts[-1][2] == {"error": "Could not play the message on the speaker."}


def test_unconfirmed_stop_does_not_raise_or_post_twice():
    body, app = FakeBody(listen=OSError("mic"), stop=ErrandError("The robot body service is unreachable.")), FakeApp()
    assert run_errand(body, app) == "failed"
    assert app.kinds == ["navigating", "arrived", "speaking", "listening", "failed"] and body.names[-1] == "stop"


def test_failed_event_posts_do_not_abort_and_the_terminal_event_gets_three_retries():
    log, app = [], FakeApp(fail={"arrived": 5, "completed": 3})
    assert run_errand(FakeBody(), app, log=log) == "completed"
    assert app.attempts.count("arrived") == 2 and app.attempts.count("completed") == 4  # 1 retry vs 3 retries
    assert app.kinds == ["navigating", "speaking", "listening", "heard", "completed"]
    assert [e["delivered"] for e in log] == [True, False, True, True, True, True]


def test_a_permanent_rejection_is_not_retried():
    app = FakeApp(fail={"navigating": 5}, error=ErrandError("app_backend answered HTTP 409"))
    assert run_errand(FakeBody(), app) == "completed"
    assert app.attempts.count("navigating") == 1 and app.kinds[-1] == "completed"


def test_cancellation_posts_failed_and_sends_stop():
    async def scenario():
        gate, app = asyncio.Event(), FakeApp()
        body = FakeBody()

        async def slow_body(name, args):
            if name == "find_person":
                gate.set()
                await asyncio.sleep(30)
            return await body(name, args)

        task = asyncio.create_task(Errand(slow_body, app, status=quiet, retry_pause_s=0).run(RUN, "Zach", TEXT))
        await gate.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return body, app

    body, app = asyncio.run(scenario())
    assert app.kinds == ["navigating", "failed"] and body.names == ["stop"]


# ---- dispatch validation and the HTTP surface ----

def dispatch_body(run_id=RUN, **over):
    return json.dumps({"run_id": run_id, "author_id": "zach", "author_name": "Zach", "text": TEXT,
                       "dispatched_at": 1789800000123, **over}).encode()


def test_parse_dispatch_accepts_the_contract_example_and_ignores_unknown_keys():
    parsed = parse_dispatch(dispatch_body(run_id=RUN.upper(), future_field=1))
    assert parsed == {"run_id": RUN, "author_id": "zach", "author_name": "Zach", "text": TEXT, "dispatched_at": 1789800000123}


@pytest.mark.parametrize("raw", [
    b"{not json", b"[]", b'"text"', b"\xff\xfe", dispatch_body(run_id="not-a-uuid"), dispatch_body(run_id=7),
    dispatch_body(text=""), dispatch_body(text="x" * 2001), dispatch_body(text=None), dispatch_body(text=["hi"]),
    dispatch_body(author_name=""), dispatch_body(author_name="   "), dispatch_body(author_name="n" * 81),
    dispatch_body(author_id=5), dispatch_body(dispatched_at="now"), dispatch_body(dispatched_at=True),
])
def test_malformed_dispatch_is_a_400(raw):
    with pytest.raises(ValueError):
        parse_dispatch(raw)
    service = ErrandService(None, status=quiet)  # never started: a bad body must not reach the queue
    code, payload = service.handle("POST", "/dispatch", raw)
    service.close()
    assert code == 400 and set(payload) == {"error"} and service.runs == {}


def wait_until(predicate, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.002)


def test_dispatch_is_202_then_idempotent_200_and_queues_fifo_while_busy():
    release, started = threading.Event(), []

    async def fake_runner(run):
        started.append(run["run_id"])
        run["events"].append({"kind": "navigating", "payload": {}, "at": 1, "delivered": True})
        while not release.is_set():
            await asyncio.sleep(0.002)
        return "failed" if run["text"] == "fail" else "completed"

    service = ErrandService(fake_runner, body_url="http://body:8001", status=quiet).start()
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    try:
        assert service.handle("GET", "/health") == (200, {"ok": True, "body_url": "http://body:8001", "busy": False})
        assert service.handle("POST", "/dispatch", dispatch_body(a)) == (202, {"run_id": a, "state": "accepted"})
        wait_until(lambda: started == [a])
        assert service.handle("POST", "/dispatch", dispatch_body(a)) == (200, {"run_id": a, "state": "running"})
        assert service.handle("POST", "/dispatch", dispatch_body(b, text="fail")) == (202, {"run_id": b, "state": "queued"})
        assert service.handle("POST", "/dispatch/", dispatch_body(c)) == (202, {"run_id": c, "state": "queued"})
        assert service.handle("POST", "/dispatch", dispatch_body(b, text="changed")) == (200, {"run_id": b, "state": "queued"})
        assert service.handle("GET", "/health")[1]["busy"] is True
        assert service.handle("GET", f"/runs/{a}") == (200, {"run_id": a, "state": "running", "events": [
            {"kind": "navigating", "payload": {}, "at": 1, "delivered": True}]})
        time.sleep(0.02)
        assert started == [a]  # one errand at a time
        release.set()
        wait_until(lambda: service.handle("GET", "/health")[1]["busy"] is False)
        assert started == [a, b, c]  # FIFO, and never a second errand for a repeated run_id
        assert service.handle("POST", "/dispatch", dispatch_body(a)) == (200, {"run_id": a, "state": "completed"})
        assert service.handle("GET", f"/runs/{b}")[1]["state"] == "failed"
        assert service.handle("GET", f"/runs/{uuid.uuid4()}") == (404, {"error": "unknown run"})
        assert service.handle("GET", "/runs/nope")[0] == 404 and service.handle("GET", "/nope")[0] == 404
    finally:
        release.set()
        service.close()


def test_a_full_queue_is_refused_and_a_crashing_runner_frees_the_slot():
    async def crashing_runner(run):
        raise RuntimeError("boom")

    service = ErrandService(crashing_runner, status=quiet)  # not started: runs pile up
    for _ in range(go2_errand.MAX_OPEN_RUNS):
        assert service.handle("POST", "/dispatch", dispatch_body(str(uuid.uuid4())))[0] == 202
    assert service.handle("POST", "/dispatch", dispatch_body(str(uuid.uuid4()))) == (503, {"error": "errand queue is full"})
    service.start()
    try:
        wait_until(lambda: service.handle("GET", "/health")[1]["busy"] is False)
        assert {run["state"] for run in service.runs.values()} == {"failed"}
    finally:
        service.close()


def test_closing_the_service_cancels_the_active_errand_with_failed_and_stop():
    body, app, started = FakeBody(), FakeApp(), threading.Event()

    async def slow_body(name, args):
        if name == "find_person":
            started.set()
            await asyncio.sleep(30)
        return await body(name, args)

    async def runner(run):
        return await Errand(slow_body, app, status=quiet, retry_pause_s=0).run(run["run_id"], run["author_name"], run["text"])

    service = ErrandService(runner, status=quiet).start()
    service.handle("POST", "/dispatch", dispatch_body())
    assert started.wait(2.0)
    service.close()
    assert app.kinds == ["navigating", "failed"] and body.names == ["stop"]
    assert service.runs[RUN]["state"] == "failed" and not service.thread.is_alive()


def test_http_shim_round_trip_over_loopback():
    async def runner(run):
        return "completed"

    service = ErrandService(runner, body_url="http://body:8001", status=quiet).start()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    except OSError:
        service.close()
        pytest.skip("cannot bind a loopback port here")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    def call(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    try:
        assert call("POST", "/dispatch", b"{not json") == (400, {"error": "body must be valid JSON"})
        assert call("POST", "/dispatch", dispatch_body()) == (202, {"run_id": RUN, "state": "accepted"})
        wait_until(lambda: call("GET", f"/runs/{RUN}")[1]["state"] == "completed")
        assert call("POST", "/dispatch", dispatch_body()) == (200, {"run_id": RUN, "state": "completed"})
        assert call("GET", "/health?x=1") == (200, {"ok": True, "body_url": "http://body:8001", "busy": False})
        assert call("POST", "/dispatch", b"x", {"Content-Length": str(go2_errand.MAX_BODY_BYTES + 1)})[0] == 413
    finally:
        server.shutdown()
        server.server_close()
        service.close()


# ---- thin HTTP adapters against httpx.MockTransport ----

class ScriptedBody:
    """Body service stand-in: receipts advance one state per request; records what it was sent."""

    def __init__(self, states, result=None, busy=0):
        self.states, self.result, self.busy, self.requests = list(states), result, busy, []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/stop":
            return httpx.Response(200, json={"stop_code": 0})
        if request.method == "POST" and self.busy > 0:
            self.busy -= 1
            return httpx.Response(409, json={"error": "busy"})
        sent = json.loads(self.requests[[r.method for r in self.requests].index("POST")].content)
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        receipt = {"command_id": sent["command_id"], "name": sent["name"], "state": state,
                   "result": self.result if state == "completed" else None, "error": "motor fault" if state == "failed" else None}
        return httpx.Response(202 if request.method == "POST" else 200, json=receipt)


def body_client(script, **kwargs):
    return BodyClient("http://body:8001/", poll_s=0.001, transport=httpx.MockTransport(script), status=quiet, **kwargs)


def test_body_client_submits_then_polls_the_receipt_to_completion():
    script = ScriptedBody(["accepted", "executing", "completed"], result={"played": True}, busy=1)
    client = body_client(script, token="body-token")
    assert asyncio.run(client.command("say", {"text": "hi"})) == {"played": True}
    sent = [json.loads(r.content) for r in script.requests if r.method == "POST"]
    assert sent[0] == sent[1] and sent[0]["name"] == "say" and sent[0]["args"] == {"text": "hi"}  # 409 retried, same id
    assert str(uuid.UUID(sent[0]["command_id"])) == sent[0]["command_id"]
    assert [(r.method, r.url.path) for r in script.requests[2:]] == [("GET", f"/command/{sent[0]['command_id']}")] * 2
    assert all(r.headers["X-Body-Token"] == "body-token" for r in script.requests)


def test_body_client_stop_uses_the_dedicated_endpoint_and_no_token_means_no_header():
    script = ScriptedBody(["completed"])
    assert asyncio.run(body_client(script).command("stop", {})) == {"stop_code": 0}
    assert [(r.method, r.url.path) for r in script.requests] == [("POST", "/stop")]
    assert "X-Body-Token" not in script.requests[0].headers


def test_body_client_failures_are_safe_errand_errors():
    with pytest.raises(ErrandError, match=r"could not finish find_person \(failed\)") as failed:
        asyncio.run(body_client(ScriptedBody(["executing", "failed"])).command("find_person", {}))
    assert "motor fault" not in str(failed.value)  # body error text stays in the operator log
    with pytest.raises(ErrandError, match="did not finish listen within 0.05 s"):
        asyncio.run(body_client(ScriptedBody(["executing"]), deadlines={"listen": 0.05}).command("listen", {"max_s": 10}))

    def refuse(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ErrandError, match="unreachable"):
        asyncio.run(body_client(refuse).command("say", {"text": "hi"}))
    with pytest.raises(ErrandError, match="rejected say"):
        asyncio.run(body_client(lambda request: httpx.Response(422, json={"error": "bad args"})).command("say", {}))


def test_app_client_posts_the_contract_shape_with_the_secret_header():
    seen = []

    def app_backend(request):
        seen.append(request)
        kind = json.loads(request.content)["kind"]
        return httpx.Response({"speaking": 202, "heard": 500, "completed": 409}[kind], json={})

    client = AppClient("http://app:8000/", "s3cret", transport=httpx.MockTransport(app_backend))
    assert asyncio.run(client.post_event(RUN, "speaking", {"text": LINE}, 1789800002500)) is True
    assert (seen[0].method, seen[0].url.path, seen[0].headers["X-Internal-Secret"]) == ("POST", "/internal/events", "s3cret")
    assert json.loads(seen[0].content) == {"run_id": RUN, "kind": "speaking", "payload": {"text": LINE}, "at": 1789800002500}
    assert asyncio.run(client.post_event(RUN, "heard", {"transcript": "ok"}, 1)) is False  # retryable
    with pytest.raises(ErrandError, match="HTTP 409") as rejected:  # run already closed: a retry cannot help
        asyncio.run(client.post_event(RUN, "completed", {}, 1))
    assert "s3cret" not in str(rejected.value)


def test_main_refuses_to_start_without_the_internal_secret(monkeypatch, capsys):
    monkeypatch.delenv("ANNIE_INTERNAL_SECRET", raising=False)
    assert go2_errand.main([]) == 2
    err = capsys.readouterr().err
    assert err.startswith("go2-errand:") and "ANNIE_INTERNAL_SECRET is not set" in err
