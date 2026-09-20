"""The MCP server: each tool's request shape against a fake dog process (httpx.MockTransport), readable errors
(offline, token, busy), polling until a receipt is terminal, and a stdio smoke test with the real MCP client.
No test here can reach a real dog: every client uses the mock transport or a dead port."""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

pytest.importorskip("mcp")
from robot.dog import mcp_server as srv  # noqa: E402

TELEMETRY = {"state": {"mode": "patrol", "battery": 71}, "connected": True, "greetings": [{"t_s": 3.0, "text": "Hi Jeanine!", "name": "Jeanine"}],
             "missions": [{"name": "say", "state": "completed"}], "graph_sentences": [f"sentence {i}" for i in range(6)],
             "voice": {"cloud": False, "speak_via": "local"}, "concerns": [], "brain": {"decisions": ["x"] * 8}, "map": {"big": True},
             "source": "simulation", "conversations": [{"t_s": 5.0, "name": "Jeanine", "kind": "fine"}]}
GRAPH = {"entities": [{"name": "Jeanine", "identity": "Jeanine", "label": "person 7", "kind": "person", "x": 2.0, "y": 0.5, "age_s": 200.0,
                       "last_seen": 1000.0, "posture": "sitting", "place": "p1", "place_name": "living room", "n_seen": 9},
                      {"name": "person 3", "identity": "Jeanine", "label": "person 3", "kind": "person", "x": 9.0, "y": 9.0, "age_s": 4000.0},
                      {"name": "cup", "identity": None, "label": "cup", "kind": "object", "x": 1.0, "y": 1.0, "age_s": 12.0}],
         "events": [{"t": 1.0, "kind": "greeting", "text": "greeted Jeanine", "place_name": "living room", "x": 0, "y": 0}]}


class FakeDog:
    """Records every request; answers like robot/dog/runtime/patrol.py. `states` is the receipt state sequence."""

    def __init__(self, states=("completed",), result=None, overrides=None):
        self.calls, self.states, self.result, self.overrides, self.polls = [], list(states), result, overrides or {}, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, dict(request.headers)))
        key = (request.method, request.url.path)
        if key in self.overrides:
            return self.overrides[key]
        if key == ("GET", "/telemetry.json"):
            return httpx.Response(200, json=TELEMETRY)
        if key == ("GET", "/graph.json"):
            return httpx.Response(200, json=GRAPH)
        if key == ("GET", "/people"):
            return httpx.Response(200, json={"people": [{"name": "Jeanine", "relation": "grandmother", "faces": 3}]})
        if key == ("GET", "/voice"):
            return httpx.Response(200, json={"cloud": False, "speak_via": "local", "eleven_key": "SECRET"})
        if key == ("POST", "/voice"):
            return httpx.Response(200, json={"cloud": body.get("cloud", False), "devices": {"input": body.get("input_device")}})
        if key == ("POST", "/people"):
            return httpx.Response(200, json={"name": body["name"], "relation": body.get("relation"), "shirt": body.get("shirt"), "faces_added": 0})
        if key == ("POST", "/stop"):
            return httpx.Response(200, json={"cancelled": True, "note": "software stop via the patrol loop"})
        if key == ("POST", "/command") and "action" in body:
            return httpx.Response(202, json={"accepted": body["action"]})
        if key == ("POST", "/command"):
            return httpx.Response(202, json=self._receipt(body["command_id"], body["name"]))
        if request.method == "GET" and request.url.path.startswith("/command/"):
            self.polls += 1
            return httpx.Response(200, json=self._receipt(request.url.path.rsplit("/", 1)[1], "polled"))
        return httpx.Response(404, json={"error": "not found"})

    def _receipt(self, cid, name):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"command_id": cid, "name": name, "state": state, "result": self.result if state == "completed" else None,
                "error": "nobody found" if state == "failed" else None}


def use(dog, **kw):
    async def no_sleep(_s):
        return None
    srv.CLIENT = srv.DogClient(base_url="http://dog.test", token=kw.pop("token", None), transport=httpx.MockTransport(dog),
                               sleep=no_sleep, **kw)
    return dog


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_real_dog(monkeypatch):
    monkeypatch.delenv("ANNIE_BODY_TOKEN", raising=False)
    monkeypatch.setenv("ANNIE_BODY_URL", "http://127.0.0.1:9")  # a client built by accident still cannot reach a dog
    yield
    srv.CLIENT = None


def test_client_reads_url_and_token_from_the_environment(monkeypatch):
    assert srv.DogClient(base_url=None).base_url == "http://127.0.0.1:9" and srv.DogClient().headers == {}
    monkeypatch.delenv("ANNIE_BODY_URL")
    monkeypatch.setenv("ANNIE_BODY_TOKEN", "s3cret")
    c = srv.DogClient()
    assert c.base_url == "http://127.0.0.1:8111" and c.headers == {"X-Body-Token": "s3cret"} and c.timeout_s == 5.0  # 8111 = simulator; 8011 is physical


def test_dog_status_is_trimmed_telemetry():
    dog = use(FakeDog())
    out = run(srv.dog_status())
    assert dog.calls[0][:2] == ("GET", "/telemetry.json")
    assert out["ok"] and out["state"] == TELEMETRY["state"] and out["greetings"][0]["name"] == "Jeanine"
    assert out["source"] == "simulation" and out["conversations"][0]["kind"] == "fine"  # sim/replay vs hardware is visible
    assert set(out) == {"ok", "connected", "state", "greetings", "missions", "graph_sentences", "voice", "concerns",
                        "source", "conversations"}  # no map, no brain log


def test_instruct_posts_the_instruct_command_and_returns_the_receipt():
    dog = use(FakeDog(states=["accepted"]), token="tok")
    out = run(srv.instruct("  check on   Jeanine "))
    method, path, body, headers = dog.calls[0]
    assert (method, path) == ("POST", "/command") and headers["x-body-token"] == "tok"
    assert body["name"] == "instruct" and body["args"] == {"text": "check on Jeanine", "author": "mcp"} and len(body["command_id"]) == 36
    assert out["ok"] and out["state"] == "accepted" and out["command_id"] == body["command_id"] and "command_status" in out["note"]
    assert len(dog.calls) == 1  # instruct does not wait
    assert run(srv.instruct("   "))["ok"] is False and len(dog.calls) == 1


def test_command_posts_an_action_and_refuses_unknown_ones():
    dog = use(FakeDog())
    for action in srv.ACTIONS:
        assert run(srv.command(action)) ["accepted"] == action
    assert [c[2] for c in dog.calls] == [{"action": a} for a in srv.ACTIONS] and all(c[1] == "/command" for c in dog.calls)
    bad = run(srv.command("backflip"))
    assert bad["ok"] is False and "explore" in bad["error"] and len(dog.calls) == len(srv.ACTIONS)


def test_stop_cancels_the_mission_and_holds_still_and_says_what_it_is():
    dog = use(FakeDog())
    out = run(srv.stop())
    assert [(c[0], c[1], c[2]) for c in dog.calls] == [("POST", "/stop", {}), ("POST", "/command", {"action": "stop"})]
    assert out["ok"] and out["mission_cancelled"]["cancelled"] is True and "NOT a hardware emergency stop" in out["note"]
    assert "until you send another command" in out["note"]  # the hold is latched now, not "resumes in 12 s"


def test_say_and_listen_wait_for_the_receipt():
    dog = use(FakeDog(states=["accepted", "executing", "completed"], result={"heard": True, "transcript": "I am fine"}))
    out = run(srv.listen(99))
    assert dog.calls[0][2]["name"] == "listen" and dog.calls[0][2]["args"] == {"max_s": 15.0}
    assert dog.polls == 2 and dog.calls[1][1] == "/command/" + dog.calls[0][2]["command_id"]
    assert out == {"ok": True, "command_id": dog.calls[0][2]["command_id"], "name": "polled", "state": "completed",
                   "result": {"heard": True, "transcript": "I am fine"}, "error": None}
    dog = use(FakeDog())
    out = run(srv.say("Hello Jeanine"))
    assert dog.calls[0][2]["name"] == "say" and dog.calls[0][2]["args"] == {"text": "Hello Jeanine"} and out["state"] == "completed"
    assert run(srv.say(""))["ok"] is False and run(srv.say("x" * 400))["ok"] and len(dog.calls[-1][2]["args"]["text"]) == 300


def test_find_person_submits_and_polls_until_terminal():
    dog = use(FakeDog(states=["accepted", "executing", "executing", "failed"]))
    out = run(srv.find_person("Jeanine", approach=False))
    assert dog.calls[0][2]["args"] == {"name": "Jeanine", "approach": False, "timeout_s": 90.0} and dog.polls == 3
    assert out["ok"] is False and out["state"] == "failed" and out["error"] == "nobody found"
    dog = use(FakeDog())
    run(srv.find_person())
    assert dog.calls[0][2]["args"] == {"name": None, "approach": True, "timeout_s": 90.0}


def test_find_person_gives_up_after_its_wait_and_says_how_to_follow_up():
    ticks = iter(range(0, 10_000, 50))  # a fake clock: 50 s per reading, so the 120 s wait ends after a few polls
    dog = use(FakeDog(states=["executing"]), clock=lambda: float(next(ticks)))
    out = run(srv.find_person("Jeanine"))
    assert out["ok"] and out["state"] == "executing" and "command_status" in out["note"] and 1 <= dog.polls <= 3
    assert run(srv.command_status(out["command_id"]))["state"] == "executing"
    assert run(srv.command_status("../telemetry.json"))["ok"] is False


def test_look_for_goes_through_an_instruction():
    dog = use(FakeDog())
    out = run(srv.look_for(" red cup "))
    assert dog.calls[0][2]["name"] == "instruct" and dog.calls[0][2]["args"] == {"text": "go to the red cup", "author": "mcp"}
    assert out["state"] == "completed" and run(srv.look_for(""))["ok"] is False


def test_the_look_for_instruction_plans_a_look_for_step_without_a_model():
    from robot.dog.planning import agent
    assert [s["name"] for s in agent.rule_plan("go to the red cup")["steps"]] == ["look_for"]


def test_where_is_reports_the_freshest_sighting_with_its_age():
    dog = use(FakeDog())
    out = run(srv.where_is("jeanine"))
    assert dog.calls[0][:2] == ("GET", "/graph.json")
    assert out["found"] and (out["x"], out["y"], out["age_s"], out["place_name"]) == (2.0, 0.5, 200.0, "living room")
    assert "3 min ago" in out["summary"] and "estimate" in out["summary"]
    assert run(srv.where_is("cup"))["kind"] == "object"
    missing = run(srv.where_is("Bob"))
    assert missing["ok"] and missing["found"] is False and "Jeanine" in missing["known"]
    assert run(srv.where_is(" "))["ok"] is False


def test_people_and_remember_person():
    dog = use(FakeDog())
    assert run(srv.people()) == {"ok": True, "people": [{"name": "Jeanine", "relation": "grandmother", "faces": 3}]}
    out = run(srv.remember_person("Anna", relation="daughter", shirt="Red"))
    assert dog.calls[1][:3] == ("POST", "/people", {"name": "Anna", "relation": "daughter", "shirt": "Red"})  # never photos
    assert out["ok"] and out["person"]["name"] == "Anna"
    run(srv.remember_person("Tom"))
    assert dog.calls[2][2] == {"name": "Tom"} and run(srv.remember_person(""))["ok"] is False
    dog = use(FakeDog(overrides={("POST", "/people"): httpx.Response(400, json={"error": "name must be 1-40 letters"})}))
    assert run(srv.remember_person("R2-D2!"))["error"] == "the dog process refused it: name must be 1-40 letters"


def test_voice_settings_reads_without_arguments_and_never_carries_keys():
    dog = use(FakeDog())
    out = run(srv.voice_settings())
    assert dog.calls[0][:2] == ("GET", "/voice") and out["changed"] == [] and "eleven_key" not in out["voice"]
    out = run(srv.voice_settings(cloud=True, input_device="Annie Audio"))
    assert dog.calls[1][:3] == ("POST", "/voice", {"cloud": True, "input_device": "Annie Audio"})
    assert out["changed"] == ["cloud", "input_device"] and out["voice"]["cloud"] is True
    run(srv.voice_settings(cloud=False))
    assert dog.calls[2][2] == {"cloud": False}  # False is a setting, not "unset"


def test_memory_returns_sentences_and_events():
    dog = use(FakeDog())
    out = run(srv.memory(limit=2))
    assert out["ok"] and out["sentences"] == ["sentence 0", "sentence 1"] and out["events"][0]["text"] == "greeted Jeanine"
    assert [c[1] for c in dog.calls] == ["/telemetry.json", "/graph.json"]
    dog = use(FakeDog(overrides={("GET", "/graph.json"): httpx.Response(503, json={"error": "graph off"})}))
    out = run(srv.memory())
    assert out["ok"] and len(out["sentences"]) == 6 and out["events"] == []  # the graph being off does not lose the sentences


def test_bad_numeric_args_fail_closed_with_readable_errors():
    dog = use(FakeDog())
    assert run(srv.memory(limit="many"))["error"] == "limit must be a finite whole number (1-30)"
    assert run(srv.listen(max_s="forever"))["error"] == "max_s must be a number (1 to 15)"
    assert run(srv.memory(limit=True))["ok"] is False  # a bool is not a count
    assert run(srv.listen(max_s=float("nan")))["error"] == "max_s must be a finite number (1 to 15)"
    assert run(srv.listen(max_s=float("inf")))["error"] == "max_s must be a finite number (1 to 15)"
    assert not dog.calls  # nothing reached the dog process


def test_malformed_dog_replies_fail_instead_of_raising():
    for path, tool, what in (("/telemetry.json", srv.dog_status, "status"), ("/graph.json", srv.memory, "memory"),
                             ("/graph.json", srv.where_is, "memory"), ("/people", srv.people, "a people list")):
        use(FakeDog(overrides={("GET", path): httpx.Response(200, json=[1, 2])}))
        out = run(tool("Jeanine") if tool is srv.where_is else tool())
        assert out["ok"] is False and what in out["error"] and "not a JSON object" in out["error"], out
        json.dumps(out)


def test_offline_dog_gives_the_same_readable_error_from_every_tool():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    for handler in (refuse, slow):
        use(handler)
        for call in (srv.dog_status(), srv.instruct("go home"), srv.command("sit"), srv.say("hi"), srv.listen(), srv.find_person(),
                     srv.look_for("door"), srv.where_is("Jeanine"), srv.people(), srv.remember_person("Anna"), srv.voice_settings(),
                     srv.memory(), srv.command_status("abc"), srv.stop()):
            out = run(call)
            assert out["ok"] is False and out["error"] == "the dog process is not reachable", out
            json.dumps(out)  # plain JSON-able
    assert "STOP WAS NOT DELIVERED" in run(srv.stop())["note"]


def test_http_errors_are_readable():
    dog = use(FakeDog(overrides={("POST", "/command"): httpx.Response(401, json={"error": "unauthorized"})}))
    assert "ANNIE_BODY_TOKEN" in run(srv.say("hi"))["error"] and len(dog.calls) == 1  # no polling after a refusal
    use(FakeDog(overrides={("POST", "/command"): httpx.Response(409, json={"error": "busy", "waiting": 8})}))
    assert "busy" in run(srv.find_person())["error"]
    use(FakeDog(overrides={("GET", "/people"): httpx.Response(503, json={"error": "people directory off"})}))
    assert run(srv.people())["error"] == "that part of the dog process is off (people directory off)"
    use(FakeDog(overrides={("GET", "/telemetry.json"): httpx.Response(200, content=b"<html>")}))
    assert "not JSON" in run(srv.dog_status())["error"]
    dog = use(FakeDog(overrides={("POST", "/stop"): httpx.Response(404, content=b"not found")}))
    out = run(srv.stop())  # no mission board: the hold still goes through, and the tool says which half failed
    assert out["ok"] and out["hold"] == {"accepted": "stop"} and "does not know" in out["mission_cancelled"]


def test_a_lost_receipt_is_reported_with_the_command_id():
    dog = FakeDog(states=["accepted"])

    def flaky(request):  # the command is accepted, then the dog process goes away before the first poll
        if request.method == "GET":
            raise httpx.ConnectError("gone", request=request)
        return dog(request)
    use(flaky)
    out = run(srv.say("hi"))
    assert out["ok"] is False and out["error"] == srv.OFFLINE and len(out["command_id"]) == 36 and "accepted" in out["note"]


def test_a_read_timeout_on_submit_still_reports_the_command_id_and_never_resends():
    posts = []

    def timeout_after_sending(request):  # the dog got the POST but the answer never came back
        if request.method == "POST":
            posts.append(json.loads(request.content))
            raise httpx.ReadTimeout("answer lost", request=request)
        raise httpx.ConnectError("dog is now unreachable", request=request)

    use(timeout_after_sending)
    out = run(srv.say("hi"))
    assert out["ok"] is False and out["error"] == srv.OFFLINE and out["state"] == "unknown"
    assert out["command_id"] == posts[0]["command_id"] and len(posts) == 1  # exactly one POST: poll, never resend
    assert "command_status" in out["note"]


def test_tools_and_resources_are_registered():
    names = {t.name for t in run(srv.mcp.list_tools())}
    assert names >= {"dog_status", "instruct", "command", "say", "listen", "find_person", "look_for", "where_is", "people",
                     "remember_person", "voice_settings", "memory", "stop", "command_status"}
    assert {str(r.uri) for r in run(srv.mcp.list_resources())} == {"annie://status", "annie://people"}
    use(FakeDog())
    assert json.loads(run(srv.status_resource()))["state"] == TELEMETRY["state"]
    assert json.loads(run(srv.people_resource()))["people"][0]["name"] == "Jeanine"


def test_mcp_json_registers_the_server():
    cfg = json.loads((ROOT / ".mcp.json").read_text())
    assert cfg["mcpServers"]["annie"] == {"command": ".venv/bin/python", "args": ["robot/dog/mcp_server.py"]}


def test_stdio_smoke_lists_the_tools():
    """Start the real server as a script (the way an MCP client does) and list its tools. No tool is called."""
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError:
        pytest.skip("mcp stdio client API unavailable")

    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "robot" / "dog" / "mcp_server.py")], cwd=str(ROOT),
                                       env={**os.environ, "ANNIE_BODY_URL": "http://127.0.0.1:9"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                return {t.name for t in listed.tools}

    names = asyncio.run(asyncio.wait_for(go(), timeout=60))
    assert {"dog_status", "instruct", "stop", "find_person", "where_is"} <= names
