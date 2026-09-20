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
        assert s.status_code == 200 and s.json() == {'available': False, 'connected': False}
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
