"""Bounded end-to-end run against the live simulator, brain, and family API.

Uses real camera pixels and real inference. Only the scene and resident WAV
are scripted inputs. No authored posture is submitted to the incident API.
Start with a fresh incident episode; this driver never clears an existing one.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx

from robot.simulation.bridge import Bridge


async def run(args):
    token = os.getenv('ANNIE_API_TOKEN')
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    brain_token = os.getenv('ANNIE_BRAIN_TOKEN') or token
    brain_headers = {'Authorization': 'Bearer ' + brain_token} if brain_token else {}
    report = {'started_at': int(time.time() * 1000), 'reply_fixture': args.reply,
              'scene': args.scene, 'status': 'running', 'source': 'live_simulation_pixels'}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        output.write_text(json.dumps(report, indent=2) + '\n')
    save()
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8766', timeout=20, trust_env=False) as viewer, \
            httpx.AsyncClient(base_url='http://127.0.0.1:8000', headers=headers, timeout=5, trust_env=False) as app, \
            httpx.AsyncClient(base_url=args.brain_url, headers=brain_headers, timeout=40, trust_env=False) as brain:
        initial = (await app.get('/events')).json()
        initial_ids = {event['event_id'] for event in initial}
        for payload in ({'action': 'scene', 'id': args.scene}, {'action': 'play'}):
            (await viewer.post('/control', json=payload)).raise_for_status()
        await asyncio.sleep(.5)
        turn_id = str(uuid4())
        (await viewer.post('/control', json={'action': 'mission', 'cmd': 'turn',
                                             'heading': 1.6, 'command_id': turn_id})).raise_for_status()
        end = time.monotonic() + 12
        while time.monotonic() < end:
            state = (await viewer.get('/state')).json()
            commands = (state.get('navigation') or {}).get('commands', [])
            outcome = next((item for item in commands if item['command_id'] == turn_id), None)
            if outcome and outcome['status'] in ('completed', 'failed'):
                report['turn'] = outcome
                report['person_safety'] = state.get('person_safety')
                break
            await asyncio.sleep(.1)
        bridge = Bridge(viewer, app, brain, perception='vision', max_inferences=args.max_inferences,
                        inference_interval=.5, status_file='.data/simulation/bridge-status.json')
        finish = time.monotonic() + 55
        answered = False
        incident = None
        try:
            while time.monotonic() < finish:
                await bridge.tick()
                status = (await app.get('/status')).json()
                pending = status.get('pending_checkin')
                if pending:
                    incident = pending['event_id']
                    report['checkin'] = pending
                if pending and pending.get('phase') == 'awaiting_reply' and not answered and args.reply != 'none':
                    answered = True
                    reply = await viewer.post('/resident-reply', json={'fixture': args.reply, 'event_id': incident})
                    reply.raise_for_status()
                    report['reply'] = reply.json()
                events = [event for event in (await app.get('/events')).json() if event['event_id'] not in initial_ids]
                report['events'] = events
                resolved = any(event['kind'] in ('checkin_ok', 'fall_confirmed') for event in events)
                if resolved and incident:
                    report['resolved_at'] = int(time.time() * 1000)
                    break
                await asyncio.sleep(.15)
            else:
                raise RuntimeError('No incident outcome within the bounded live run')
            # A real family message traverses the app command queue and body
            # bridge, then waits for the selected player's completion receipt.
            response = await app.post('/say', json={'text': 'I am here with you.'})
            response.raise_for_status()
            family = response.json()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                await bridge.tick()
                commands = (await app.get('/commands')).json()
                family = next(item for item in commands if item['command_id'] == family['command_id'])
                if family['status'] in ('completed', 'failed'):
                    break
                await asyncio.sleep(.15)
            report['family_message'] = family
            report['speech'] = (await viewer.get('/state')).json().get('speech', [])
            report['inferences'] = bridge.inferences
            report['last_perception'] = bridge.last_perception
            report['status'] = 'passed' if family['status'] == 'completed' else 'failed'
            if args.reply == 'okay':
                assert any(item['kind'] == 'checkin_ok' for item in report['events'])
                assert not any(item['kind'] == 'fall_confirmed' for item in report['events'])
            else:
                assert sum(item['kind'] == 'fall_confirmed' for item in report['events']) == 1
            if args.reply != 'none':
                assert report['reply']['decision']['applied']
            assert family['status'] == 'completed'
        except Exception as exc:
            report['status'] = 'failed'
            report['error'] = type(exc).__name__
            raise
        finally:
            if bridge.vision_task and not bridge.vision_task.done():
                await bridge.vision_task
            report['finished_at'] = int(time.time() * 1000)
            save()
    print(json.dumps({'status': report['status'], 'output': str(output), 'inferences': report['inferences']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reply', choices=('okay', 'help', 'not-okay', 'none'), default='not-okay')
    parser.add_argument('--scene', default='floor_lying-000')
    parser.add_argument('--brain-url', default='http://127.0.0.1:8002')
    parser.add_argument('--max-inferences', type=int, default=6)
    parser.add_argument('--output', default='output/live-e2e.json')
    args = parser.parse_args()
    if not 1 <= args.max_inferences <= 12:
        parser.error('--max-inferences must be 1–12')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
