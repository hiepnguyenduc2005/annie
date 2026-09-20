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
                        make_handler, parse_dispatch, phrase_message, plan_mission)

RUN = "5488e7cb-8d54-4c59-8e02-9b739d694a81"
TEXT = "How are you feeling today?"
LINE = 'Jeanine, it\'s Annie. Zach asked me to pass this along: "How are you feeling today?"'
JEANINE = {"found": True, "track_id": 3, "identity": {"name": "Jeanine", "score": 0.71}, "approached": True, "searched_s": 12.5}
quiet = lambda _text: None  # noqa: E731


class FakeBody:
    """Scripted body_command: result dict or exception per command name; records calls and the event log seen."""

    def __init__(self, log=None, **results):
        self.results = {"find_person": JEANINE, "say": {"played": True}, "listen": {"transcript": "Okay.", "heard": True},
                        "stop": {"stop_code": 0}, "hello": {"codes": {"stand": 0, "balance": 0, "hello": 0}},
                        "dance": {"codes": {"stand": 0, "balance": 0, "dance": "no_ack"}}, **results}
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


def test_interpret_reply_names_whoever_was_addressed():
    assert interpret_reply("okay", "Ellis")["detail"] == "Ellis says okay. 🙂"


# ---- plain language -> mission ----

@pytest.mark.parametrize("text, intent, target", [
    ("wave at grandma", "wave", "Jeanine"), ("Say hi to mom for me", "wave", "Jeanine"), ("go say hello to Ellis", "wave", "Ellis"),
    ("greet granny", "wave", "Jeanine"), ("please wave!", "wave", "Jeanine"), ("WAVE TO NAN", "wave", "Jeanine"),
    ("dance for grandma", "dance", "Jeanine"), ("do a little dance", "dance", "Jeanine"), ("Dance!", "dance", "Jeanine"),
    ("can you dance for Henry", "dance", "Henry"), ("show gran a dance", "dance", "Jeanine"),
    ("check on mom", "checkin", "Jeanine"), ("check in on her", "checkin", "Jeanine"), ("is she okay?", "checkin", "Jeanine"),
    ("is he okay", "checkin", "Jeanine"), ("see if grandma needs anything", "checkin", "Jeanine"),
    ("make sure mum is alright", "checkin", "Jeanine"), ("how's Janine doing?", "checkin", "Jeanine"),
    ("ask grandma how she's doing", "checkin", "Jeanine"), ("ask mom if she is okay today?", "checkin", "Jeanine"),
    ("can you tell me if grandma is okay", "checkin", "Jeanine"), ("check on Ellis", "checkin", "Ellis"),
    ("come", "come", None), ("come here", "come", None), ("Come to me", "come", None), ("Annie, come here!", "come", None),
    ("go to grandma", "come", "Jeanine"), ("find Ellis", "come", "Ellis"), ("go find my mom", "come", "Jeanine"),
    ("say hi to grandma and check on her", "wave", "Jeanine"),  # first match wins: wave, dance, checkin, come
    ("Henry here, wave at grandma", "wave", "Jeanine"),
])
def test_plan_mission_commands(text, intent, target):
    mission = plan_mission("Henry" if text.startswith("Henry") else "Zach", text)
    assert (mission["intent"], mission["target"], mission["message"]) == (intent, target, None)
    assert mission["trick"] == {"wave": "hello", "dance": "dance"}.get(intent) and mission["listen"] is (intent == "checkin")


@pytest.mark.parametrize("text, target, message", [
    ("tell grandma dinner is at six", "Jeanine", "dinner is at six"),
    ("Tell Grandma that dinner is at 6", "Jeanine", "dinner is at 6"),
    ("remind mom to take her pills", "Jeanine", "take her pills"),
    ("ask gran, did the mail come?", "Jeanine", "did the mail come?"),
    ("let grandma know I'll be late", "Jeanine", "I'll be late"),
    ("please tell Ellis to call me", "Ellis", "call me"),
    ("tell her I love her", "Jeanine", "I love her"),
    # an explicit tell/ask/remind always relays: keywords inside the message must not swallow it
    ("remind grandma to make sure the stove is off", "Jeanine", "make sure the stove is off"),
    ("ask grandma if she wants to dance on Saturday", "Jeanine", "if she wants to dance on Saturday"),
    ("tell grandma to check on the cat", "Jeanine", "check on the cat"),
    # ordinary messages fall through word for word (today's behaviour), even with keyword-like words in them
    (TEXT, "Jeanine", TEXT),
    ("Heat wave coming, drink lots of water", "Jeanine", "Heat wave coming, drink lots of water"),
    ("Make sure you take your pills tonight", "Jeanine", "Make sure you take your pills tonight"),
    ("Come over for dinner on Sunday!", "Jeanine", "Come over for dinner on Sunday!"),
    ("Did you check on the oven?", "Jeanine", "Did you check on the oven?"),
    ("Dance class is cancelled tomorrow", "Jeanine", "Dance class is cancelled tomorrow"),
    ("go to grandma's house later", "Jeanine", "go to grandma's house later"),
    ("tell grandma", "Jeanine", "tell grandma"),
    # a name inside a message is not its recipient; a name that opens it is
    ("Ellis will pick you up at 5", "Jeanine", "Ellis will pick you up at 5"),
    ("Ellis, call me when you can", "Ellis", "Ellis, call me when you can"),
])
def test_plan_mission_relays(text, target, message):
    mission = plan_mission("Zach", text)
    assert (mission["intent"], mission["target"], mission["message"]) == ("relay", target, message)
    assert mission["line"] == phrase_message("Zach", message, target) and mission["listen"] and mission["trick"] is None


def test_plan_mission_lines_are_fixed_templates():
    assert plan_mission("Zach", "wave at grandma")["line"] == "Hi Jeanine! Zach says hello."
    assert plan_mission("Zach", "say hi to Ellis")["line"] == "Hi Ellis! Zach says hello."
    assert plan_mission("Zach", "dance for nan")["line"] == "Jeanine, that dance was from Zach!"
    assert plan_mission("Zach", "check on mom")["line"] == "Hi Jeanine, Zach asked me to check on you. Are you alright?"
    assert plan_mission("Zach", "come here")["line"] == "Here I am."
    assert plan_mission(" Zach ", "tell  grandma\n dinner is at six")["line"] == \
        'Jeanine, it\'s Annie. Zach asked me to pass this along: "dinner is at six"'
    assert len(plan_mission("Z" * 80, "tell grandma " + "x" * 1900)["line"]) == 300


# ---- errand sequence ----

def test_happy_path_posts_the_exact_event_sequence_and_ends_happy():
    log, app = [], FakeApp()
    body = FakeBody(log=log)
    assert run_errand(body, app, log=log) == "completed"
    assert body.calls == [("find_person", {"name": "Jeanine", "timeout_s": 90, "approach": True}),
                          ("say", {"text": LINE}), ("listen", {"max_s": 10})]  # and no stop on success
    assert app.posts == [
        (RUN, "navigating", {"detail": "Looking for Jeanine.", "intent": "relay", "target": "Jeanine"}),
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


def test_wave_errand_finds_her_waves_speaks_and_completes_without_listening():
    log, app = [], FakeApp()
    body = FakeBody(log=log)
    assert run_errand(body, app, text="go say hi to grandma", log=log) == "completed"
    assert body.calls == [("find_person", {"name": "Jeanine", "timeout_s": 90, "approach": True}), ("hello", {}),
                          ("say", {"text": "Hi Jeanine! Zach says hello."})]
    assert app.posts == [
        (RUN, "navigating", {"detail": "Looking for Jeanine.", "intent": "wave", "target": "Jeanine"}),
        (RUN, "arrived", {"detail": "Found Jeanine."}),
        (RUN, "speaking", {"text": "Hi Jeanine! Zach says hello."}),
        (RUN, "completed", {"detail": "Said hello to Jeanine. Wave command acknowledged by the robot.", "reply": "none",
                            "mood": "happy"}),
    ]
    assert body.seen["hello"] == ["navigating", "arrived"] and body.seen["say"] == ["navigating", "arrived"]


def test_dance_errand_reports_an_unacknowledged_trick_honestly():
    body, app = FakeBody(), FakeApp()
    assert run_errand(body, app, text="dance for grandma") == "completed"
    assert body.names == ["find_person", "dance", "say"]
    assert body.calls[-1] == ("say", {"text": "Jeanine, that dance was from Zach!"})
    assert app.kinds == ["navigating", "arrived", "speaking", "completed"]
    assert app.posts[-1][2] == {"detail": "Dance for Jeanine: command sent, not acknowledged.", "reply": "none", "mood": "happy"}


def test_a_failed_trick_fails_the_errand_and_nothing_is_said():
    body, app = FakeBody(hello=ErrandError("The robot body could not finish hello (failed).")), FakeApp()
    assert run_errand(body, app, text="wave at grandma") == "failed"
    assert body.names == ["find_person", "hello", "stop"] and app.kinds == ["navigating", "arrived", "failed"]
    assert app.posts[-1][2] == {"error": "The robot body could not finish hello (failed)."}


def test_checkin_errand_asks_listens_and_reports_the_reply():
    body, app = FakeBody(), FakeApp()
    assert run_errand(body, app, text="can you check on mom?") == "completed"
    question = "Hi Jeanine, Zach asked me to check on you. Are you alright?"
    assert body.calls[1:] == [("say", {"text": question}), ("listen", {"max_s": 10})]
    assert app.posts == [
        (RUN, "navigating", {"detail": "Looking for Jeanine.", "intent": "checkin", "target": "Jeanine"}),
        (RUN, "arrived", {"detail": "Found Jeanine."}),
        (RUN, "speaking", {"text": question}),
        (RUN, "listening", {}),
        (RUN, "heard", {"transcript": "Okay."}),
        (RUN, "completed", {"detail": "Jeanine says okay. 🙂", "reply": "okay", "mood": "happy"}),
    ]
    app = FakeApp()
    run_errand(FakeBody(listen={"transcript": None, "heard": False}), app, text="is she okay?")
    assert app.posts[-1][2] == {"detail": "Asked Jeanine if they are alright; no reply heard.", "reply": "none", "mood": "neutral"}
    app = FakeApp()
    run_errand(FakeBody(listen={"transcript": "help me", "heard": True}), app, text="check on grandma")
    assert app.posts[-1][2] == {"detail": 'Jeanine may need help: "help me"', "reply": "concern", "mood": "worried"}


def test_relay_with_tell_says_only_the_message_part():
    body, app = FakeBody(), FakeApp()
    assert run_errand(body, app, text="Tell grandma that dinner is at six") == "completed"
    line = 'Jeanine, it\'s Annie. Zach asked me to pass this along: "dinner is at six"'
    assert body.calls[1] == ("say", {"text": line}) and app.posts[2] == (RUN, "speaking", {"text": line})
    assert app.posts[0][2] == {"detail": "Looking for Jeanine.", "intent": "relay", "target": "Jeanine"}
    assert app.kinds == ["navigating", "arrived", "speaking", "listening", "heard", "completed"]


def test_come_goes_to_whoever_is_there_and_says_here_i_am():
    body, app = FakeBody(find_person={**JEANINE, "identity": {"name": "Ellis", "score": 0.8}}), FakeApp()
    assert run_errand(body, app, text="come here") == "completed"
    assert body.calls == [("find_person", {"name": None, "timeout_s": 90, "approach": True}), ("say", {"text": "Here I am."})]
    assert app.posts == [
        (RUN, "navigating", {"detail": "Looking for someone.", "intent": "come", "target": None}),
        (RUN, "arrived", {"detail": "Found Ellis."}),
        (RUN, "speaking", {"text": "Here I am."}),
        (RUN, "completed", {"detail": "Annie walked up to Ellis.", "reply": "none", "mood": "neutral"}),
    ]
    app = FakeApp()
    run_errand(FakeBody(find_person={**JEANINE, "identity": None, "approached": False}), app, text="come")
    assert app.posts[1][2] == {"detail": "Found someone."}
    assert app.posts[-1][2]["detail"] == "Annie found someone but did not walk up."
    app = FakeApp()
    assert run_errand(FakeBody(find_person={"found": False}), app, text="come here") == "failed"
    assert app.posts[-1][2] == {"error": "Could not find anyone."}


def test_a_named_target_other_than_jeanine_is_searched_for_and_guarded_the_same_way():
    ellis = {**JEANINE, "identity": {"name": "Ellis", "score": 0.8}}
    body, app = FakeBody(find_person=ellis), FakeApp()
    assert run_errand(body, app, text="tell Ellis to call me") == "completed"
    assert body.calls[0] == ("find_person", {"name": "Ellis", "timeout_s": 90, "approach": True})
    assert body.calls[1] == ("say", {"text": 'Ellis, it\'s Annie. Zach asked me to pass this along: "call me"'})
    assert app.posts[-1][2]["detail"] == "Ellis says okay. 🙂"
    body, app = FakeBody(), FakeApp()  # the body found Jeanine instead: Ellis's message is not for her
    assert run_errand(body, app, text="tell Ellis to call me") == "failed"
    assert app.posts[-1][2] == {"error": "Could not find Ellis."} and "say" not in body.names


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
    assert app.posts == [(RUN, "navigating", {"detail": "Looking for Jeanine.", "intent": "relay", "target": "Jeanine"}),
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


def wait_until(predicate, timeout_s=5.0):  # generous: only reached on failure
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.002)


def test_dispatch_is_202_then_idempotent_200_and_queues_fifo_while_busy():
    release, started = threading.Event(), []

    async def fake_runner(run):
        run["events"].append({"kind": "navigating", "payload": {}, "at": 1, "delivered": True})
        started.append(run["run_id"])  # last: the test thread reads `events` as soon as it sees this
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
    assert started.wait(5.0)
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

    with pytest.raises(ErrandError, match="unreachable"):  # keeps retrying through a link relaunch, then says so
        asyncio.run(body_client(refuse, reconnect_s=0.2).command("say", {"text": "hi"}))
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


def test_dispatch_requires_the_shared_secret_when_configured():
    from go2_errand import ErrandService

    async def never(run):
        return "completed"
    svc = ErrandService(never, body_url="http://127.0.0.1:1", dispatch_secret="s3cret")
    body = b'{"run_id":"5488e7cb-8d54-4c59-8e02-9b739d694a81","author_id":"zach","author_name":"Zach","text":"hi","dispatched_at":1}'
    assert svc.handle("POST", "/dispatch", body)[0] == 401
    assert svc.handle("POST", "/dispatch", body, headers={"X-Internal-Secret": "wrong"})[0] == 401
    assert svc.handle("GET", "/health")[0] == 200  # health stays open
    open_svc = ErrandService(never, body_url="http://127.0.0.1:1")  # loopback dev mode: no secret configured
    assert open_svc.handle("POST", "/dispatch", body)[0] in (202, 503)
