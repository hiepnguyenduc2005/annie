"""Capture an explicit setup-camera memory for the Zach/Janine story.

This is a rendered historical prelude, not a claimed robot journey. Camera
pose comes from MuJoCo. Only the image model's response is indexed; authored
object coordinates and an expected caption never enter inference.
"""
import argparse
import asyncio
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx


def capture(model_path, map_id):
    import mujoco
    from PIL import Image
    model = mujoco.MjModel.from_xml_path(str(model_path.resolve()))
    data = mujoco.MjData(model)
    if model.nkey:
        mujoco.mj_resetDataKeyframe(model, data, 0)
    mujoco.mj_forward(model, data)
    camera = model.camera('phone_memory').id
    options = mujoco.MjvOption()
    options.geomgroup[3] = options.geomgroup[4] = 0
    with mujoco.Renderer(model, width=640, height=480) as renderer:
        renderer.update_scene(data, camera='phone_memory', scene_option=options)
        image = Image.fromarray(renderer.render())
    output = io.BytesIO()
    image.save(output, format='JPEG', quality=90)
    # MuJoCo cameras look down their local negative Z axis.
    rotation = data.cam_xmat[camera].reshape(3, 3)
    forward = -rotation[:, 2]
    return {'frame_id': str(uuid4()), 'ts': int(time.time()*1000),
            'pose': {'x': float(data.cam_xpos[camera][0]), 'y': float(data.cam_xpos[camera][1]),
                     'yaw': math.atan2(float(forward[1]), float(forward[0])), 'map_id': map_id},
            'source': 'simulation_render', 'jpeg_b64': base64.b64encode(output.getvalue()).decode()}


def validate_result(frame, result):
    from robot.app_backend.app.models import Perception
    perception = result['perception']
    if any(perception.get(key) != frame[key] for key in ('frame_id', 'ts', 'pose')):
        raise ValueError('Inference did not preserve setup capture identity')
    if perception.get('source') != 'simulation_vlm':
        raise ValueError('Historical memory needs real image inference')
    return Perception.model_validate(perception)


async def run(args):
    from dotenv import load_dotenv
    load_dotenv(override=False)
    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        state_response = await client.get(args.viewer_url+'/state')
        state_response.raise_for_status()
        state = state_response.json()
        if (not state.get('ready') or state.get('scene_loading')
                or state.get('current_scene', {}).get('id') != 'grandmas-house'):
            raise ValueError('Load Grandma\'s house before capturing its memory prelude')
        if state.get('intelligence_enabled'):
            raise ValueError('Finish or pause the current task before the setup prelude')
        frame = capture(args.model, state['map_id'])
        args.output.mkdir(parents=True, exist_ok=True)
        image_path = args.output / ('phone-memory-'+frame['frame_id']+'.jpg')
        image_path.write_bytes(base64.b64decode(frame['jpeg_b64']))
        report = {'stage': 'setup_camera_prelude', 'camera_id': 'phone_memory',
                  'source_model_sha256': hashlib.sha256(args.model.read_bytes()).hexdigest(),
                  'capture': {k:v for k,v in frame.items() if k != 'jpeg_b64'},
                  'image_path': str(image_path), 'indexed': False}
        if args.index:
            token = os.getenv('ANNIE_BRAIN_TOKEN') or os.getenv('ANNIE_API_TOKEN')
            headers = {'Authorization': 'Bearer '+token} if token else {}
            response = await client.post(args.brain_url+'/infer', json=frame, headers=headers)
            response.raise_for_status()
            result = response.json()
            perception = validate_result(frame, result)
            # A changed world cannot receive this scene's historical evidence.
            fresh = await client.get(args.viewer_url+'/state')
            fresh.raise_for_status()
            if fresh.json()['map_id'] != frame['pose']['map_id']:
                raise ValueError('Scene changed during memory inference')
            written = await client.post(args.memory_url+'/observations', json=perception.model_dump(mode='json'))
            written.raise_for_status()
            receipt = written.json()
            if receipt.get('frame_id') != frame['frame_id'] or receipt.get('status') not in ('indexed', 'already_indexed'):
                raise ValueError('Graph did not acknowledge this capture')
            report.update(indexed=True, perception=perception.model_dump(mode='json'),
                          provider=result.get('provider'), latency_ms=result.get('latency_ms'), receipt=receipt)
        (args.output/'phone-memory-prelude.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, default=Path('.data/simulation/scenes/grandmas-house.xml'))
    parser.add_argument('--output', type=Path, default=Path('output'))
    parser.add_argument('--viewer-url', default='http://127.0.0.1:8766')
    parser.add_argument('--brain-url', default='http://127.0.0.1:8003')
    parser.add_argument('--memory-url', default='http://127.0.0.1:8005')
    parser.add_argument('--index', action='store_true', help='Run real inference and index its cited result; may use the approved cloud budget')
    asyncio.run(run(parser.parse_args()))
