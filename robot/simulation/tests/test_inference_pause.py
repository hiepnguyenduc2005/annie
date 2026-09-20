"""A terminal inference failure pauses the brain while keeping body receipts alive."""
import asyncio
import json

import httpx

from robot.simulation.bridge import Bridge


def test_budget_failure_stops_automatic_retries_but_keeps_body_sync(tmp_path):
    async def check():
        bridge = Bridge(None, None, None, perception='agent', status_file=tmp_path/'status.json')
        state = {'ready': True, 'map_id': 'home', 'running': True,
                 'intelligence_enabled': True, 'navigation': {'state': 'idle'}}
        calls = []
        async def request(*args, **kwargs): return state
        async def body(*args): calls.append('body')
        async def think(*args):
            calls.append('plan')
            req = httpx.Request('POST', 'http://brain/plan')
            response = httpx.Response(503, request=req, json={'detail': 'Cloud reservation limit reached'})
            bridge.warn('agent', httpx.HTTPStatusError('private text', request=req, response=response))
        bridge.request, bridge.body, bridge.think = request, body, think
        await bridge.tick(wait_vision=True)
        bridge.next_inference = 0
        await bridge.tick(wait_vision=True)
        assert calls == ['body', 'plan', 'body']
        assert 'allowance' in bridge.inference_blocked
        status = json.loads(bridge.status_file.read_text())
        assert status['inference_blocked'] == status['last_error']
        assert 'private text' not in bridge.status_file.read_text()
    asyncio.run(check())


def test_unrecognized_provider_error_does_not_latch_configuration_block():
    bridge = Bridge(None, None, None)
    req = httpx.Request('POST', 'http://brain/plan')
    response = httpx.Response(503, request=req, json={'detail': 'private provider response'})
    bridge.warn('agent', httpx.HTTPStatusError('secret', request=req, response=response))
    assert bridge.inference_blocked is None
    assert 'private' not in bridge.last_error and 'secret' not in bridge.last_error
