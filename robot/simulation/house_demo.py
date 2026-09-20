"""Live full-house acceptance run; scripted environment, model-selected robot.

This owns the body bridge during the run. Stop a separate bridge first. It
stages a synthetic fall and recorded reply, never injects perception labels or
robot destinations. Actual camera inference and execution receipts decide pass.
"""
import argparse
import base64
import asyncio
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx

from robot.simulation.bridge import Bridge


async def wait_for_routine(viewer, bridge, save, *, duration=16, timeout=30):
    """A paused or faulted simulator cannot leave the demo running forever."""
    started=time.monotonic()
    while time.monotonic()-started < timeout:
        state=(await viewer.get('/state')).json()
        if state.get('physics_error'):
            raise RuntimeError('Simulator faulted during the routine')
        if state['simulation_time'] >= duration:
            return
        if not state.get('running') and time.monotonic()-started > 1:
            raise RuntimeError('Simulation was paused during the routine')
        await bridge.tick()
        save()
        await asyncio.sleep(.1)
    raise TimeoutError('House routine did not advance within its deadline')


async def run(args):
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'run_id':str(uuid4()), 'status':'running', 'started_at':int(time.time()*1000),
              'scene':'grandmas-house', 'source':'live_simulation_pixels',
              'stages':[], 'reply_fixture':args.reply, 'goal':args.goal}
    def record(kind, detail, **extra):
        if not any(item['kind']==kind for item in report['stages']):
            report['stages'].append({'kind':kind,'ts':int(time.time()*1000),'detail':detail,**extra})
        save()
    def save():
        report['updated_at']=int(time.time()*1000)
        temporary=output.with_suffix('.tmp')
        temporary.write_text(json.dumps(report,indent=2)+'\n')
        temporary.replace(output)
    token=os.getenv('ANNIE_API_TOKEN')
    headers={'Authorization':'Bearer '+token} if token else {}
    async with httpx.AsyncClient(base_url=args.viewer_url,timeout=20,trust_env=False) as viewer, \
            httpx.AsyncClient(base_url=args.app_url,headers=headers,timeout=5,trust_env=False) as app, \
            httpx.AsyncClient(base_url=args.brain_url,timeout=40,trust_env=False) as brain:
        health=(await brain.get('/health')).json()
        if health.get('mode') not in ('local','cloud'):
            raise ValueError('Demo brain is not configured')
        if health.get('mode')=='cloud' and not args.allow_cloud:
            raise ValueError('Cloud demo inference requires explicit --allow-cloud')
        report['provider_mode']=health['mode']
        report['model']=health['model']; save()
        bridge=Bridge(viewer,app,brain,perception='agent',continuous_local=health['mode']=='local',
                      max_inferences=args.max_inferences,
                      inference_interval=.35,status_file='.data/simulation/bridge-status.json')
        def capture_evidence(perception):
            observation=bridge.observation_evidence
            if not observation or observation['frame_id']!=perception['frame_id']:
                return
            images=report.setdefault('evidence_images',[])
            if len(images)>=6 or any(item['frame_id']==observation['frame_id'] for item in images):
                return
            target=Path('output')/('house-frame-'+observation['frame_id']+'.jpg')
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(base64.b64decode(observation['jpeg_b64'],validate=True))
            images.append({'frame_id':observation['frame_id'],'ts':observation['ts'],
                           'path':str(target),'perception':perception})
        old=(await viewer.get('/state')).json()['map_id']
        initial_ids={e['event_id'] for e in (await app.get('/events')).json()}
        initial_status=(await app.get('/status')).json()
        if initial_status.get('pending_checkin'):
            raise ValueError('Resolve the current check-in before starting another run')
        (await app.post('/demo/reset-episode')).raise_for_status()
        (await viewer.post('/control',json={'action':'scene','id':'grandmas-house'})).raise_for_status()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            state=(await viewer.get('/state')).json()
            if state.get('ready') and state['map_id']!=old: break
            await asyncio.sleep(.1)
        else: raise TimeoutError('House did not load')
        (await viewer.post('/control',json={'action':'play'})).raise_for_status()
        record('routine','Grandma begins her daily routine; the robot remains at the entrance.')
        # A timed environmental fixture stages the incident in open walking
        # space, not underneath the reading chair. Never move the robot here.
        await wait_for_routine(viewer, bridge, save)
        for command in ({'action':'resident','cmd':'fall'},
                        {'action':'intelligence','enabled':True,'goal':args.goal}):
            (await viewer.post('/control',json=command)).raise_for_status()
        record('started','Staged fall in the full house. Robot route is chosen by the configured vision model.')
        report['map_id']=state['map_id']
        start_pose=state['qpos_base'][:2]
        finish=time.monotonic()+args.timeout
        reply_task=None; answered=False; family=None; checkin=None
        try:
            while time.monotonic()<finish:
                await bridge.tick()
                state=(await viewer.get('/state')).json()
                if state['map_id']!=report['map_id']:
                    raise ValueError('Scene changed during the run')
                status=(await app.get('/status')).json()
                commands=(await app.get('/commands')).json()
                events=[e for e in (await app.get('/events')).json() if e['event_id'] not in initial_ids]
                report.update(events=events,inferences=bridge.inferences,last_error=bridge.last_error,
                              last_plan=bridge.agent_state, memory=bridge.memory_state,
                              person_safety=state.get('person_safety'))
                moved=sum((a-b)**2 for a,b in zip(start_pose,state['qpos_base'][:2]))**.5
                if moved>.5:
                    record('movement',f'Measured robot displacement {moved:.2f} m',pose=state['qpos_base'][:3])
                perception=status.get('perception')
                if perception and perception.get('person') and perception['pose']['map_id']==report['map_id']:
                    capture_evidence(perception)
                    record('person','Image model reports a visible person',frame_id=perception['frame_id'],caption=perception['caption'])
                pending=status.get('pending_checkin')
                if pending:
                    checkin=pending; report['checkin']=pending
                    record('checkin','Two qualifying camera captures started a check-in',event_id=pending['event_id'])
                    if pending['phase']=='awaiting_reply':
                        record('question_played','Native audio playback completed',command_id=pending['say_command_id'])
                        if not answered and args.reply!='none':
                            answered=True
                            reply_task=asyncio.create_task(viewer.post('/resident-reply',json={
                                'fixture':args.reply,'event_id':pending['event_id']}))
                if reply_task and reply_task.done():
                    response=reply_task.result(); response.raise_for_status()
                    report['reply']=response.json(); reply_task=None
                    record('reply', 'Local speech recognition completed',
                           text=report['reply']['recognition']['text'],
                           latency_ms=report['reply']['recognition']['latency_ms'],
                           applied=report['reply']['decision']['applied'])
                alerts=[e for e in events if e['kind']=='fall_confirmed']
                if alerts and family is None:
                    record('alert','Alert persisted in the family app',event_id=alerts[0]['event_id'])
                    response=await app.post('/say',json={'text':'I am here with you.'});response.raise_for_status()
                    family=response.json()
                if family:
                    receipt=next((c for c in commands if c['command_id']==family['command_id']),family)
                    report['family_message']=receipt
                    if receipt['status']=='failed': raise RuntimeError('Family audio playback failed')
                    if receipt['status']=='completed' and reply_task is None:
                        record('family_played','Family message completed native playback',command_id=family['command_id'])
                        if not any(s['kind']=='movement' for s in report['stages']):
                            raise RuntimeError('Incident loop worked but model-directed approach was not demonstrated')
                        if args.reply!='none' and not report.get('reply',{}).get('decision',{}).get('applied'):
                            raise RuntimeError('Recorded reply did not apply to this incident')
                        report['status']='passed';break
                save();await asyncio.sleep(.1)
            else: raise TimeoutError('Full-house workflow did not finish within its deadline')
        except (Exception, asyncio.CancelledError) as exc:
            report.update(status='failed',error=str(exc) or 'Run interrupted')
        finally:
            # Stop issuing model actions; do not label incomplete stages passed.
            await viewer.post('/control',json={'action':'intelligence','enabled':False,'goal':args.goal})
            if bridge.vision_task and not bridge.vision_task.done():
                await bridge.vision_task
            if reply_task: await reply_task
            await bridge.tick()
            report['finished_at']=int(time.time()*1000);save()
            archive=Path('output')/('house-demo-'+report['run_id']+'.json')
            archive.parent.mkdir(parents=True,exist_ok=True)
            archive.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({'status':report['status'],'output':str(output),'inferences':bridge.inferences}))
        return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--brain-url',default='http://127.0.0.1:8004')
    p.add_argument('--allow-cloud',action='store_true',help='Use configured cloud brain for synthetic frames only')
    p.add_argument('--max-inferences',type=int,default=20)
    p.add_argument('--viewer-url',default='http://127.0.0.1:8766')
    p.add_argument('--app-url',default='http://127.0.0.1:8000')
    p.add_argument('--goal',default='Go to the living room, look around, and check whether the resident needs help. Stay clear of people.')
    p.add_argument('--reply',choices=('not-okay','help','none'),default='not-okay')
    p.add_argument('--timeout',type=float,default=180)
    p.add_argument('--output',default='.data/simulation/house-demo.json')
    args=p.parse_args()
    if not 1 <= args.max_inferences <= 20:
        p.error('--max-inferences must be between 1 and 20')
    if not 0 < args.timeout <= 300:
        p.error('--timeout must be positive and at most 300 seconds')
    from dotenv import load_dotenv
    from robot.simulation.bridge_lease import body_bridge_lease
    load_dotenv(Path(__file__).resolve().parents[2]/'.env',override=False)
    try:
        with body_bridge_lease():
            report=asyncio.run(run(args))
            if report['status']!='passed': raise SystemExit(1)
    except Exception as exc:
        output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps({'status':'failed','stages':[],'error':str(exc)}))
        raise


if __name__=='__main__':main()
