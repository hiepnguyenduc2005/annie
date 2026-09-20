import ipaddress
from urllib.parse import urlsplit
from starlette.datastructures import Headers
from starlette.responses import JSONResponse


def local_ip(host):
    if host in ('localhost', 'testclient', 'testserver'):
        return True
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback or any(address in ipaddress.ip_network(net) for net in
            ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', 'fc00::/7', 'fe80::/10'))
    except (ValueError, TypeError):
        return False


class LocalBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        host = headers.get('host', '')
        scheme = 'https' if scope.get('scheme') in ('https', 'wss') else 'http'
        try:
            valid_host = local_ip(urlsplit('//' + host).hostname)
        except ValueError:
            valid_host = False
        valid_client = scope.get('client') and local_ip(scope['client'][0])
        origin = headers.get('origin')
        code = 400 if not valid_host else 403 if not valid_client or (origin and origin != f'{scheme}://{host}') else None
        if code:
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
            else:
                await JSONResponse({'detail': 'Local network and same-origin access required'}, status_code=code)(scope, receive, send)
            return
        if scope['type'] == 'http':
            chunks, size = [], 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body', b'')
                size += len(chunk)
                if size > 65536:
                    await JSONResponse({'detail': 'Request body too large'}, status_code=413)(scope, receive, send)
                    return
                chunks.append(chunk)
                if not message.get('more_body', False):
                    break
            consumed = False
            original_receive = receive
            async def replay():
                nonlocal consumed
                if consumed:
                    return await original_receive()
                consumed = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            receive = replay
        original_send = send
        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] = list(message.get('headers', [])) + [(b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff')]
            await original_send(message)
        await self.app(scope, receive, secure_send)
