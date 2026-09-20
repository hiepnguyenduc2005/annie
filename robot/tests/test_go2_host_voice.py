"""Fakes-only tests for go2_host_voice: no speaker, no mic, no network."""
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import go2_host_voice  # noqa: E402


class FakeApp:
    """Scripted app backend: a queue of commands and an optional check-in status."""

    def __init__(self, commands=(), pending=None):
        self.commands = [dict(c) for c in commands]
        self.pending = pending
        self.receipts = []
        self.log = []

    def handler(self, request):
        body = json.loads(request.content) if request.content else None
        self.log.append((request.method, request.url.path, body))
        if request.url.path == '/commands' and request.method == 'GET':
            return httpx.Response(200, json=self.commands)
        if request.url.path == '/status':
            return httpx.Response(200, json={'pending_checkin': self.pending})
        if request.url.path.startswith('/commands/') and request.url.path.endswith('/receipt'):
            cid = request.url.path.split('/')[2]
            self.receipts.append((cid, body['status'], body['source'], body.get('detail')))
            for item in self.commands:
                if item['command_id'] == cid:
                    item['status'] = body['status']
            return httpx.Response(200, json={'ok': True})
        return httpx.Response(404)

    def transport(self):
        return httpx.MockTransport(self.handler)


def say_command(text='Are you okay?'):
    return {'command_id': str(uuid4()), 'cmd': 'say', 'text': text, 'status': 'queued'}


def run(app, *, speaker=None, player=None, listener=None, duration_s=0.15, **kw):
    spoken, played, listened = [], [], []

    async def default_speaker(text, command_id):
        spoken.append((text, command_id))
        return {'file_path': f'/tmp/{command_id}.wav', 'duration_s': 1.5}

    def default_player(path, duration_s):
        played.append((path, duration_s))

    def default_listener():
        listened.append(1)
        return {'outcome': 'speech', 'decision': {'intent': 'reassurance'}}

    report = asyncio.run(go2_host_voice.run_host_voice(
        app_url='http://app', speaker=speaker or default_speaker, player=player or default_player,
        listener=listener or default_listener, http_transport=app.transport(),
        poll_s=0.01, duration_s=duration_s, **kw))
    return report, spoken, played, listened


def test_say_command_is_spoken_played_and_receipted_in_order():
    cmd = say_command()
    app = FakeApp([cmd])
    report, spoken, played, listened = run(app)
    assert spoken == [('Are you okay?', cmd['command_id'])]
    assert played == [(f"/tmp/{cmd['command_id']}.wav", 1.5)]
    assert [(s, src) for _, s, src, _ in app.receipts] == [('accepted', 'host'), ('executing', 'host'), ('completed', 'host')]
    assert report['spoken'] == 1 and report['completed'] is True and listened == []


def test_each_command_is_handled_once_even_if_it_stays_listed():
    cmd = say_command()
    app = FakeApp([cmd])
    # The fake never removes completed commands from the list; the daemon must not replay them.
    _, spoken, _, _ = run(app, duration_s=0.2)
    assert len(spoken) == 1


def test_speaker_failure_yields_failed_receipt_with_sanitized_detail():
    cmd = say_command()
    app = FakeApp([cmd])

    async def broken(text, command_id):
        raise RuntimeError('private vendor path /Users/secret/say')

    report, _, played, _ = run(app, speaker=broken)
    statuses = [s for _, s, _, _ in app.receipts]
    assert statuses == ['accepted', 'failed'] and played == []
    detail = app.receipts[-1][3]
    assert 'secret' not in detail and 'RuntimeError' in detail
    assert report['failed'] == 1


def test_player_failure_after_synthesis_is_failed_not_completed():
    app = FakeApp([say_command()])

    def bad_player(path, duration_s):
        raise RuntimeError('afplay exploded')

    run(app, player=bad_player)
    assert [s for _, s, _, _ in app.receipts] == ['accepted', 'executing', 'failed']


def test_motion_commands_are_left_alone():
    app = FakeApp([{'command_id': str(uuid4()), 'cmd': 'goto', 'waypoint': 'kitchen', 'status': 'queued'}])
    report, spoken, _, _ = run(app)
    assert spoken == [] and app.receipts == [] and report['ignored_motion'] == 1
    assert not [e for e in app.log if e[0] == 'POST' and e[1] == '/commands']


def test_awaiting_reply_runs_listener_once_per_event():
    app = FakeApp([], pending={'event_id': str(uuid4()), 'phase': 'awaiting_reply', 'deadline_at': 10 ** 13})
    report, _, _, listened = run(app, duration_s=0.2)
    assert listened == [1] and report['listened'] == 1
    assert report['last_reply']['outcome'] == 'speech'


def test_listener_error_is_reported_and_loop_continues():
    app = FakeApp([], pending={'event_id': str(uuid4()), 'phase': 'awaiting_reply', 'deadline_at': 10 ** 13})

    def broken():
        raise RuntimeError('mic gone')

    report, _, _, _ = run(app, listener=broken)
    assert report['listen_errors'] == 1 and report['completed'] is True


def test_app_unreachable_is_reported_not_fatal():
    def down(request):
        raise httpx.ConnectError('refused')

    report = asyncio.run(go2_host_voice.run_host_voice(
        app_url='http://app', speaker=None, player=None, listener=None,
        http_transport=httpx.MockTransport(down), poll_s=0.01, duration_s=0.05))
    assert report['app_errors'] >= 1 and report['spoken'] == 0


def test_free_speech_after_the_wake_word_becomes_an_instruction():
    from robot.dog.voice.commands import parse_command
    cmd = parse_command("Annie, go check whether grandma is in the kitchen")
    assert cmd["intent"] == "instruct" and cmd["wake"] == "annie"
    assert cmd["phrase"].startswith("go check whether grandma")
    assert parse_command("Annie sit")["intent"] == "sit"          # fixed phrases still win
    assert parse_command("go check on grandma") is None            # no wake word: not for the dog
    assert parse_command("Annie") is None and parse_command("Annie hi")["intent"] == "hello"


def test_conversation_window_needs_no_wake_word_and_mutes_annies_own_voice():
    import time
    from robot.dog.voice.commands import CommandListener, parse_command
    lis = CommandListener(lambda c, t: None, status=lambda *a: None)
    assert parse_command("I'm feeling fine today") is None                                   # normally not for the dog
    lis.open_conversation(45.0)
    cmd = parse_command("I'm feeling fine today", require_wake=not (time.time() < lis.open_until))
    assert cmd["intent"] == "instruct" and cmd["wake"] is None                                # the loop turns this into converse
    lis.mute(1.0)
    assert time.time() < lis.muted_until
    lis.muted_until = 0.0
    assert not (time.time() < lis.muted_until)
