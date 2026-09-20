"""Live facts from the dog process merge into the family app's history and answers."""
from app.companion import CompanionService


def test_merge_live_turns_telemetry_into_facts_and_answers():
    c = CompanionService()
    n = c.merge_live({"state": {"t_s": 100.0},
                      "graph_sentences": ["Jeanine last seen 65 s ago near the chair near place-1"],
                      "greetings": [{"t_s": 40.0, "text": "Hi Jeanine, lovely to see you.", "name": "Jeanine"}],
                      "checkins": [{"t_s": 50.0}],
                      "instructions": [{"t_s": 90.0, "text": "go towards the door", "reply": "Looking for the door."}]})
    assert n == 4
    texts = [f["text"] for f in c.all_memory()]
    assert any("Jeanine last seen" in t for t in texts) and any("Said hello to Jeanine" in t for t in texts)
    assert all(f["id"] < 0 for f in c.live_facts) and all(f["id"] > 0 for f in c.memory)
    assert "near the chair" in c.ask("where is Jeanine?")
    assert c.merge_live({}) == 0 and c.all_memory() == c.memory  # dog down: seeded facts only


def test_dog_routes_degrade_when_the_dog_process_is_down(monkeypatch):
    """No dog process: status says so, commands and voice settings answer 503, memory keeps the seeded facts."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    monkeypatch.setenv('ANNIE_DOG_VIEW_URL', 'http://127.0.0.1:9')  # nothing listens on port 9
    with TestClient(create_app(':memory:', mode='live', token='')) as c:
        s = c.get('/api/dog/status')
        assert s.status_code == 200 and s.json() == {'available': False, 'connected': False,
                                                     'motion_enabled': None, 'paused': None}
        assert c.post('/api/dog/command', json={'action': 'explore'}).status_code == 503
        assert c.post('/api/dog/command', json={'action': 'fly'}).status_code == 422
        v = c.get('/api/settings/voice')
        assert v.status_code == 200 and v.json()['available'] is False
        m = c.get('/api/memory')
        assert m.status_code == 200 and all(f['id'] > 0 for f in m.json())


def test_ask_understands_grandma_and_she_as_the_resident():
    c = CompanionService()
    c.merge_live({"state": {"t_s": 10.0}, "graph_sentences": ["Jeanine last seen 65 s ago near the chair near place-1"]})
    assert "near the chair" in c.ask("Where is Grandma?")
    assert "near the chair" in c.ask("where is she right now")
    assert "near the chair" in c.ask("Where is Jeanine?")


def test_family_wording_and_honest_unknown_resident():
    c = CompanionService()
    c.merge_live({"state": {"t_s": 10.0}, "graph_sentences": [
        "an unidentified person last seen just now near place-1, upright (4 tracker id(s) in the last 2 min; ids churn, not a head count)",
        "1 place(s) explored over 31 s, 875 occupied voxels remembered; the dog is in place-1"]})
    texts = [f["text"] for f in c.live_facts]
    assert texts[0] == "Someone last seen just now near spot 1, upright."
    assert "voxels" not in texts[1] and "Annie is in spot 1" in texts[1]
    assert c.ask("Where is Grandma?").startswith("I haven't recognised Jeanine by name yet. Someone last seen")
    c.live_facts = []
    assert c.ask("where is she") == "I haven't seen Jeanine yet today, but I'm keeping watch."


def test_reminder_update_and_emergency_outcomes():
    from fastapi.testclient import TestClient
    from app.main import create_app
    import httpx
    H = {'X-Internal-Secret': 's3cret'}
    monkey_env = {'ROBOT_BACKEND_URL': 'http://robot.test'}
    import os
    old_env = {k: os.environ.get(k) for k in monkey_env}
    os.environ.update(monkey_env)
    from app import family as family_mod

    async def accepted(url, payload, timeout, headers=None):  # the errand accepted the dispatch; events come later
        return httpx.Response(202, json={'accepted': True})
    real_default = family_mod.default_dispatch
    family_mod.default_dispatch = accepted
    try:
        _run_outcome_checks(TestClient, create_app, H)
    finally:
        family_mod.default_dispatch = real_default
        for k, v in old_env.items():
            (os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v))


def _run_outcome_checks(TestClient, create_app, H):
    with TestClient(create_app(':memory:', mode='live', token='', internal_secret='s3cret')) as c:
        # the mock robot sequence is off in live mode; drive the run through the internal event route
        rid = c.post('/api/messages', json={'author_id': 'zach', 'text': 'tell Grandma to charge her phone', 'reminder_id': 4}).json()['run_id']
        c.post('/internal/events', json={'run_id': rid, 'kind': 'navigating', 'payload': {'detail': 'Looking for Jeanine.'}, 'at': 1}, headers=H)
        r = c.post('/internal/events', json={'run_id': rid, 'kind': 'completed', 'payload': {'detail': 'Jeanine says okay.', 'reply': 'okay', 'mood': 'happy'}, 'at': 2}, headers=H)
        assert r.status_code in (200, 201, 202)
        run = c.get(f'/api/runs/{rid}').json()
        assert run['outcome']['type'] == 'reminder_update' and run['outcome']['done'] is True
        assert next(x for x in c.get('/api/reminders').json() if x['id'] == 4)['done'] is True
        rid2 = c.post('/api/messages', json={'author_id': 'zach', 'text': 'check on Grandma'}).json()['run_id']
        c.post('/internal/events', json={'run_id': rid2, 'kind': 'completed', 'payload': {'detail': 'Jeanine may need help: "I fell"', 'reply': 'concern', 'mood': 'worried'}, 'at': 3}, headers=H)
        assert c.get(f'/api/runs/{rid2}').json()['outcome']['type'] == 'emergency'
        alerts = c.get('/api/alerts').json()
        assert len(alerts) == 1 and alerts[0]['run_id'] == rid2
