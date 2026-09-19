import asyncio
import json
from uuid import uuid4

import httpx

from simulation.bridge import Bridge


def test_body_map_reset_and_command_execution():
    async def exercise():
        cid, voice = str(uuid4()), str(uuid4())
        state = dict(ready=True, map_id="map-1", qpos_base=[1, 2, .3, 1, 0, 0, 0],
                     running=True, physics_error=None, navigation=dict(state="moving",
                     waypoint="home", waypoints=[dict(id="home", x=1, y=2)], commands=[]))
        commands = [dict(command_id=cid, status="queued", cmd="goto", waypoint="home"),
                    dict(command_id=voice, status="queued", cmd="unsupported", text="hello")]
        ingests, controls, receipts = [], [], []

        def handler(request):
            data = json.loads(request.content) if request.content else None
            if request.url.path == "/state":
                return httpx.Response(200, json=state)
            if request.url.path == "/commands":
                return httpx.Response(200, json=commands)
            if request.url.path == "/ingest":
                ingests.append(data)
            elif request.url.path == "/control":
                controls.append(data)
            elif request.url.path.endswith("/receipt"):
                receipts.append(data)
                target = next(c for c in commands if c['command_id'] in request.url.path)
                target['status'] = data['status']
            else:
                raise AssertionError(request.url)
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler)) as client:
            bridge = Bridge(client, client, client, clock=lambda: 100)
            await bridge.tick()
            await bridge.tick()
            assert len(controls) == 1
            assert [x['status'] for x in receipts] == ['accepted', 'failed']
            state['navigation']['commands'] = [dict(command_id=cid, status='completed')]
            await bridge.tick()
            assert receipts[-1]['status'] == 'completed'
            assert len([x for x in ingests if x['channel'] == 'dog.map']) == 1
            state['map_id'] = 'map-reset'
            await bridge.tick()
            maps = [x['data'] for x in ingests if x['channel'] == 'dog.map']
            assert len(maps) == 2 and maps[-1]['map_id'] == 'map-reset'
            status = [x['data'] for x in ingests if x['channel'] == 'dog.status'][-1]
            assert status['pose'] == dict(x=1, y=2, yaw=0, map_id='map-reset')
            assert status['ts'] == 100000 and status['state'] == 'patrolling'
    asyncio.run(exercise())


def test_vision_preserves_evidence_deduplicates_and_caps_attempts():
    async def exercise():
        observation = dict(frame_id=str(uuid4()), ts=123, pose=dict(x=1, y=2, yaw=0, map_id='m'),
                           source='simulation_render', jpeg_b64='abcd', simulation_time=9, camera_id='robot_front')
        sent, ingests = [], []
        def handler(request):
            if request.url.path == '/observation':
                return httpx.Response(200, json=observation)
            data = json.loads(request.content)
            if request.url.path == '/infer':
                sent.append(data)
                return httpx.Response(200, json={'perception': {**{k: data[k] for k in ('frame_id','ts','pose')},
                    'person': True, 'posture':'lying', 'location':'floor', 'confidence':.9, 'caption':'Person lying.'}})
            ingests.append(data)
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
            bridge = Bridge(client, client, client, perception='vision', max_inferences=2, clock=lambda: .123)
            await bridge.infer()
            await bridge.infer()
            assert len(sent) == 1
            assert 'simulation_time' not in sent[0] and 'camera_id' not in sent[0]
            assert ingests[0]['data']['ts'] == 123
            assert 'jpeg_b64' not in ingests[0]['data']
            assert bridge.inferences == 1
            observation['frame_id'] = str(uuid4())
            await bridge.infer()
            observation['frame_id'] = str(uuid4())
            await bridge.infer()
            assert bridge.inferences == 2 and len(sent) == 2
    asyncio.run(exercise())


def test_provider_failure_does_not_fabricate_perception_or_leak(caplog):
    async def exercise():
        def handler(request):
            if request.url.path == '/observation':
                return httpx.Response(200, json=dict(frame_id=str(uuid4()), ts=1, pose={},
                                                    source='simulation_render', jpeg_b64='abcd'))
            if request.url.path == '/infer':
                return httpx.Response(503, text='secret provider data')
            raise AssertionError('No app ingestion allowed on provider failure')
        async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
            bridge = Bridge(client, client, client, perception='vision', clock=lambda: .001)
            await bridge.infer()
            assert bridge.inferences == 1
    asyncio.run(exercise())
    assert 'vision unavailable' in caplog.text
    assert 'secret provider data' not in caplog.text


def test_transport_uncertainty_does_not_resend_motion():
    async def exercise():
        cid = str(uuid4())
        controls, receipts = [], []
        def handler(request):
            if request.url.path == '/commands':
                return httpx.Response(200, json=[dict(command_id=cid, cmd='stop', status='queued')])
            if request.url.path == '/control':
                controls.append(request)
                raise httpx.ReadTimeout('private error', request=request)
            receipts.append(json.loads(request.content))
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
            bridge = Bridge(client, client, client)
            await bridge.process_commands({'commands': []})
            await bridge.process_commands({'commands': []})
            assert len(controls) == 1
            assert not any(r['status'] == 'completed' for r in receipts)
            await bridge.process_commands({'commands': [{'command_id': cid, 'status':'completed'}]})
            assert receipts[-1]['status'] == 'completed'
    asyncio.run(exercise())


def test_mismatched_brain_evidence_is_not_ingested():
    async def exercise():
        observation = dict(frame_id=str(uuid4()), ts=123, pose=dict(x=1, y=2, yaw=0, map_id='m'),
                           source='simulation_render', jpeg_b64='abcd')
        def handler(request):
            if request.url.path == '/observation':
                return httpx.Response(200, json=observation)
            if request.url.path == '/infer':
                return httpx.Response(200, json={'perception': {'frame_id':observation['frame_id'],
                                                               'ts':124, 'pose':observation['pose']}})
            raise AssertionError('Evidence mismatch must not reach app')
        async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
            await Bridge(client, client, client).infer()
    asyncio.run(exercise())


def test_speech_synthesis_waits_for_browser_playback_receipt():
    async def exercise():
        cid = str(uuid4())
        sent, receipts = [], []
        def handler(request):
            if request.url.path == '/commands':
                return httpx.Response(200, json=[dict(command_id=cid, cmd='say', text='Are you okay?', status='queued')])
            if request.url.path == '/say':
                sent.append(json.loads(request.content))
                return httpx.Response(202, json=dict(command_id=cid, status='synthesized',
                                                    url=f'/speech/{cid}.wav', duration_s=1))
            receipts.append(json.loads(request.content))
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
            bridge = Bridge(client, client, client)
            await bridge.process_commands({'commands': []})
            await bridge.process_commands({'commands': []})
            assert sent == [dict(text='Are you okay?', command_id=cid)]
            assert [r['status'] for r in receipts] == ['accepted']
            await bridge.process_commands({'commands':[dict(command_id=cid,status='executing')]})
            assert receipts[-1]['status'] == 'executing'
            await bridge.process_commands({'commands':[dict(command_id=cid,status='completed')]})
            assert receipts[-1]['status'] == 'completed'
    asyncio.run(exercise())


def test_status_file_preserves_stale_result_and_omits_images(tmp_path):
    async def exercise():
        observation = dict(frame_id=str(uuid4()), ts=123, pose=dict(x=1,y=2,yaw=0,map_id='m'),
                           source='simulation_render', jpeg_b64='SECRET_IMAGE_BYTES')
        perception = {**{k:observation[k] for k in ('frame_id','ts','pose')},
                      'person':True, 'posture':'sitting','location':'chair','confidence':.9,'caption':'Person sits.'}
        def handler(request):
            if request.url.path == '/observation':
                return httpx.Response(200,json=observation)
            if request.url.path == '/infer':
                return httpx.Response(200,json=dict(perception=perception, provider=dict(mode='local',model='qwen',
                                                    jpeg_b64='SECRET_IMAGE_BYTES'),latency_ms=3000))
            return httpx.Response(200,json={'accepted':False})
        async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
            path = tmp_path / 'nested' / 'status.json'
            bridge = Bridge(client, client, client, perception='vision', status_file=path, clock=lambda: .123)
            await bridge.infer()
            status = json.loads(path.read_text())
            assert status['last_perception']['ts'] == 123
            assert status['ingest_accepted'] is False
            assert status['last_provider'] == dict(mode='local',model='qwen')
            assert status['last_latency_ms'] == 3000
            assert 'SECRET_IMAGE_BYTES' not in path.read_text()
            assert not list(path.parent.glob('.bridge-status-*'))
    asyncio.run(exercise())
