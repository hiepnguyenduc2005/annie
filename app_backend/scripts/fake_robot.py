#!/usr/bin/env python3
"""A stand-in for robot_backend, so the app's outbound calls can be watched
without the GX10.

It listens where ROBOT_BACKEND_URL points, prints every payload app_backend
sends it, and (with --auto-reply) sends the callbacks back, so the whole loop
runs end to end with no robot. It is also a working reference for the robot
side: whatever this prints is exactly what robot_backend will receive, and
whatever it posts back is exactly what robot_backend must post.

Standard library only, so it runs on the GX10 as-is.

    .venv/bin/python app_backend/scripts/fake_robot.py --auto-reply

Then point app_backend at it and turn the mock off:

    ROBOT_BACKEND_URL=http://127.0.0.1:8001 ANNIE_FAMILY_MOCK_ROBOT=false \\
    ANNIE_INTERNAL_SECRET=demo-secret .venv/bin/uvicorn app_backend.app.main:app \\
        --host 0.0.0.0 --port 8000 --no-proxy-headers
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The point of this tool is watching payloads arrive, so never sit in a buffer
# when output is piped to a file or a log viewer.
sys.stdout.reconfigure(line_buffering=True)

ARGS = None

# What the dog "does" after a message is dispatched. Each entry is
# (seconds to wait, event kind, payload) and maps onto the run beats the
# family app renders live.
ERRAND = [
    (1.5, 'navigating', {'waypoint': 'living-room', 'detail': 'Looking for Jeanine.'}),
    (2.0, 'arrived', {'waypoint': 'living-room', 'detail': 'Found Jeanine in the living room.'}),
    (2.0, 'speaking', {'text': None}),          # filled with the dispatched text
    (2.0, 'listening', {}),
    (2.0, 'heard', {'transcript': 'Oh, thank you dear. I will do it now.'}),
    (1.0, 'completed', {'detail': 'Message delivered.'}),
]


def post(path, payload, secret=True):
    """Call back into app_backend the way robot_backend must."""
    request = urllib.request.Request(
        ARGS.app_url.rstrip('/') + path,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST')
    if secret:
        request.add_header('X-Internal-Secret', ARGS.secret)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        print(f'   !! callback {path} -> {exc.code} {exc.read()[:200].decode(errors="replace")}')
    except OSError as exc:
        print(f'   !! callback {path} failed: {exc}')
    return None


def run_errand(run_id, text):
    """Report progress beat by beat, exactly as the real robot should."""
    for delay, kind, payload in ERRAND:
        time.sleep(delay)
        body = dict(payload)
        if kind == 'speaking' and body.get('text') is None:
            body['text'] = f'Jeanine, Zach asked me to pass this along: "{text}"'
        status = post('/internal/events', {
            'run_id': run_id, 'kind': kind, 'payload': body,
            'at': int(time.time() * 1000)})
        print(f'   -> /internal/events {kind:11} {status}')


def reply_to_message(message_id):
    """The yellow path: the dog's action summary, then the resident's words."""
    time.sleep(3)
    print('   ->', '/internal/message-reply robot  ',
          post('/internal/message-reply', {
              'message_id': message_id,
              'text': 'Walked to the living room and passed the message along.'}))
    time.sleep(2)
    print('   ->', '/internal/message-reply resident',
          post('/internal/message-reply', {
              'message_id': message_id, 'source': 'resident',
              'text': 'Oh, thank you dear. I will do it now.'}))


def confirm_reminder(reminder_id, item):
    """The red path: compliance with a reminder."""
    time.sleep(3)
    print('   ->', '/internal/reminder-history      ',
          post('/internal/reminder-history', {
              'reminder_id': reminder_id,
              'description': f'Jeanine completed: {item}'}))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # We print our own, tidier line.

    def _read(self):
        length = int(self.headers.get('content-length') or 0)
        try:
            return json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError:
            return {}

    def _respond(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == '/health':
            self._respond(200, {'status': 'ok', 'service': 'fake-robot'})
        else:
            self._respond(404, {'detail': 'not found'})

    def do_POST(self):
        body = self._read()
        stamp = time.strftime('%H:%M:%S')
        print(f'\n[{stamp}] POST {self.path}')
        print(json.dumps(body, indent=2))

        # Ack immediately; the errand runs afterwards. app_backend allows
        # about 3s before it gives up and marks the run unreachable.
        self._respond(202, {'accepted': True})

        if not ARGS.auto_reply:
            return
        if self.path == '/dispatch' and body.get('run_id'):
            threading.Thread(target=run_errand,
                             args=(body['run_id'], body.get('text', '')),
                             daemon=True).start()
        elif self.path == '/messages' and body.get('message_id'):
            threading.Thread(target=reply_to_message,
                             args=(body['message_id'],), daemon=True).start()
        elif self.path == '/reminders' and body.get('reminder_id'):
            threading.Thread(target=confirm_reminder,
                             args=(body['reminder_id'], body.get('item', 'the reminder')),
                             daemon=True).start()


def main():
    global ARGS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8001)
    parser.add_argument('--app-url', default='http://127.0.0.1:8000',
                        help="app_backend's address, for the callbacks")
    parser.add_argument('--secret', default='demo-secret',
                        help='must match ANNIE_INTERNAL_SECRET on app_backend')
    parser.add_argument('--auto-reply', action='store_true',
                        help='send the callbacks back, completing the loop')
    ARGS = parser.parse_args()

    print(f'fake robot_backend listening on http://0.0.0.0:{ARGS.port}')
    print(f'  callbacks -> {ARGS.app_url}  (auto-reply {"on" if ARGS.auto_reply else "off"})')
    print('  serving: GET /health, POST /dispatch, POST /messages, POST /reminders')
    print('  waiting for app_backend...\n')
    ThreadingHTTPServer(('0.0.0.0', ARGS.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
