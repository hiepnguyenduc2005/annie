"""Showcase orchestration over mocked HTTP; no robot, sockets or model calls."""
import json

import httpx
import pytest

from robot.showcase import Showcase, TRICKS, positive


class Clock:
    now = 0.
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def runner(handler, **kwargs):
    client = httpx.Client(base_url='http://test', transport=httpx.MockTransport(handler))
    clock = Clock()
    return Showcase(client, client, client, clock=clock, sleep=clock.sleep,
                    timeout=2, poll=.1, **kwargs)


def test_patrol_sequences_only_after_identified_completed_receipts():
    posted, polls, output = [], [], []
    def handle(request):
        if request.method == 'POST':
            assert len(polls) == 2*len(posted)
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={'command_id': str(len(posted))})
        cid = str(len(posted))
        polls.append(cid)
        status = 'accepted' if polls.count(cid) == 1 else 'completed'
        return httpx.Response(200, json=[{'command_id': 'other', 'status': 'completed'},
                                       {'command_id': cid, 'status': status}])
    demo = runner(handle, output=output.append)
    demo.prepare = lambda: None
    demo.patrol()
    assert [c['waypoint'] for c in posted] == ['living-room', 'bedroom', 'hallway', 'home']
    assert sum('0.100 s' in line for line in output) == 4


def test_failed_receipt_stops_patrol():
    posts = []
    def handle(request):
        if request.method == 'POST':
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={'command_id': '1'})
        return httpx.Response(200, json=[{'command_id': '1', 'status': 'failed'}])
    demo = runner(handle)
    demo.prepare = lambda: None
    with pytest.raises(RuntimeError, match='failed'):
        demo.patrol()
    assert len(posts) == 1


def test_terminal_timeout_is_bounded_and_never_resends():
    posts = []
    def handle(request):
        if request.method == 'POST':
            posts.append(request)
            return httpx.Response(200, json={'command_id': '1'})
        return httpx.Response(200, json=[{'command_id': '1', 'status': 'accepted'}])
    demo = runner(handle)
    with pytest.raises(TimeoutError, match='receipt 1'):
        demo.command({'cmd': 'goto', 'waypoint': 'home'}, 'home')
    assert len(posts) == 1
    assert 2 <= demo.clock() < 2.2


def test_tricks_422_are_reported_individually():
    posts, output = [], []
    def handle(request):
        posts.append(json.loads(request.content))
        return httpx.Response(422, json={'detail': 'unsupported'})
    demo = runner(handle, output=output.append)
    demo.prepare = lambda: None
    demo.tricks()
    assert [p['trick'] for p in posts] == list(TRICKS)
    assert sum('trick command not available yet' in line for line in output) == 5


def test_tricks_do_not_hide_other_http_failures():
    demo = runner(lambda request: httpx.Response(503))
    demo.prepare = lambda: None
    with pytest.raises(httpx.HTTPStatusError):
        demo.tricks()


def fall_runner(*, audio_failure=False, change_map=False):
    evidence = {'frame_id': 'fresh', 'pose': {'map_id': 'sim-one'}}
    suspected = {'event_id': 'new', 'kind': 'fall_suspected', 'ts': 100, 'evidence': evidence}
    phase, controls, output = [0], [], []
    def handle(request):
        path = request.url.path
        if path == '/health':
            return httpx.Response(200, json={'mode': 'local'})
        if path == '/control':
            controls.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if path == '/demo/reset-episode':
            return httpx.Response(200, json={})
        if path == '/state':
            return httpx.Response(200, json={'map_id': 'changed' if change_map and phase[0] else 'sim-one',
                                            'resident': {'state': 'fallen'}})
        if path == '/status':
            phase[0] += 1
            pending = {'event_id': 'new', 'say_command_id': 'question',
                       'phase': 'awaiting_playback' if phase[0] == 1 else 'awaiting_reply', 'deadline_at': 9000}
            return httpx.Response(200, json={'pending_checkin': pending if phase[0] < 3 else None})
        if path == '/commands':
            return httpx.Response(200, json=[{'command_id': 'unrelated', 'status': 'completed'},
                                            {'command_id': 'question', 'status': 'completed' if phase[0] > 1 else 'accepted'}])
        if path == '/events':
            old = {'event_id': 'old', 'kind': 'fall_confirmed', 'evidence': evidence, 'ts': 1}
            if not phase[0]: return httpx.Response(200, json=[old])
            events = [old, suspected]
            if phase[0] >= 3 or audio_failure:
                events += [{'event_id': 'failure', 'kind': 'checkin_audio_failed', 'ts': 200,
                            'reason': 'playback_failed' if audio_failure else 'input_unavailable', 'evidence': evidence},
                           {'event_id': 'escalated', 'kind': 'fall_confirmed', 'ts': 201, 'evidence': evidence}]
            return httpx.Response(200, json=events)
        raise AssertionError(path)
    demo = runner(handle, output=output.append)
    demo.prepare = lambda: None
    demo.local_bridge = lambda: None
    return demo, controls, output


def test_fall_follows_own_question_and_reply_window_not_old_escalation():
    demo, controls, output = fall_runner()
    demo.fall()
    timeline = [line for line in output if line.startswith('+')]
    assert ['fall_suspected', 'say', 'reply window', 'checkin_audio_failed', 'escalation'] == [
        line.split(' s ', 1)[1].split(':')[0] for line in timeline]
    assert any('input_unavailable' in line for line in output)
    assert {'action': 'resident', 'cmd': 'fall'} in controls
    assert controls[-1]['enabled'] is False


def test_fall_playback_failure_is_not_a_successful_timeline():
    demo, controls, output = fall_runner(audio_failure=True)
    with pytest.raises(RuntimeError, match='full playback/reply timeline'):
        demo.fall()
    assert not any('reply window:' in line for line in output)
    assert controls[-1]['enabled'] is False


def test_fall_scene_change_aborts_and_pauses_planning():
    demo, controls, _ = fall_runner(change_map=True)
    with pytest.raises(RuntimeError, match='Scene changed'):
        demo.fall()
    assert controls[-1]['enabled'] is False


def test_fall_rejects_cloud_before_any_control():
    paths = []
    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={'mode': 'cloud'})
    demo = runner(handle)
    with pytest.raises(RuntimeError, match='local brain'):
        demo.fall()
    assert paths == ['/health']


def test_status_prints_all_components_on_http_failure(tmp_path):
    paths, output = [], []
    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(503)
    demo = runner(handle, status_file=tmp_path/'missing', output=output.append)
    with pytest.raises(RuntimeError, match='unavailable'):
        demo.status()
    assert paths == ['/status', '/state', '/health']
    assert any('bridge: status unavailable' in line for line in output)


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf'])
def test_deadlines_must_be_finite_positive(value):
    from argparse import ArgumentTypeError
    with pytest.raises(ArgumentTypeError):
        positive(value)


@pytest.mark.parametrize('override', [
    {'continuous_local': False}, {'last_provider': {'mode': 'cloud'}},
    {'updated_at': 0}, {'perception_mode': 'ground-truth'},
])
def test_fall_rejects_stale_or_nonlocal_bridge(tmp_path, override):
    import time
    path = tmp_path / 'status.json'
    path.write_text(json.dumps({'updated_at': int(time.time()*1000), 'continuous_local': True,
                              'perception_mode': 'agent', **override}))
    demo = runner(lambda request: httpx.Response(200), status_file=path)
    with pytest.raises(RuntimeError, match='fresh agent bridge'):
        demo.local_bridge()
