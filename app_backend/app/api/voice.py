"""Family control for robot/Mac speech, under the existing local-network boundary."""
import os
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

router = APIRouter(prefix='/api/settings', tags=['voice'])


class VoiceUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    muted: StrictBool


async def dog_voice(muted=None):
    url = os.getenv('ANNIE_DOG_VIEW_URL', 'http://127.0.0.1:8011').rstrip('/') + '/voice'
    token = os.getenv('ANNIE_BODY_TOKEN', '')
    headers = {'X-Body-Token': token} if token else {}
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False, follow_redirects=False) as client:
            response = await client.get(url, headers=headers) if muted is None else await client.post(
                url, json={'muted': muted}, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or type(data.get('muted')) is not bool:
            raise ValueError('Missing confirmed mute state')
        if muted is not None and data['muted'] != muted:
            raise ValueError('Mute state not confirmed')
        return {'available': True, 'muted': data['muted']}
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, 'Robot audio state is unavailable; change was not confirmed') from None


@router.get('/voice')
async def voice_settings():
    return await dog_voice()


@router.post('/voice')
async def update_voice(body: VoiceUpdate):
    return await dog_voice(body.muted)
