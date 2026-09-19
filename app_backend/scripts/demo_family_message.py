#!/usr/bin/env python3
"""Exercise the full family-message path against a live app_backend and print
each run event as it arrives.

Run against the local demo server (see app_backend/README.md):
    .venv/bin/python app_backend/scripts/demo_family_message.py \
        --text "How are you feeling today?"

Reads ANNIE_API_URL / ANNIE_API_TOKEN from the environment; both default to
the loopback demo server with no token.
"""
import argparse
import os
import sys
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--author', default='zach', choices=['jeanine', 'zach', 'ellis'])
    parser.add_argument('--text', default="Hi Mom, just checking in, how are you feeling today?")
    parser.add_argument('--base-url', default=os.getenv('ANNIE_API_URL', 'http://127.0.0.1:8000'))
    parser.add_argument('--token', default=os.getenv('ANNIE_API_TOKEN', ''))
    parser.add_argument('--timeout-s', type=float, default=120.0)
    args = parser.parse_args()

    headers = {'Authorization': f'Bearer {args.token}'} if args.token else {}
    terminal = {'completed', 'failed', 'unreachable'}

    with httpx.Client(base_url=args.base_url, headers=headers, timeout=5.0) as client:
        response = client.post('/api/messages', json={'author_id': args.author, 'text': args.text})
        response.raise_for_status()
        body = response.json()
        run_id = body['run_id']
        print(f'dispatched run {run_id} (status={body["status"]!r})')

        seen = 0
        last_status = body['status']
        deadline = time.monotonic() + args.timeout_s
        while time.monotonic() < deadline:
            run = client.get(f'/api/runs/{run_id}').json()
            if run['status'] != last_status:
                print(f'  status -> {run["status"]}')
                last_status = run['status']
            for event in run['events'][seen:]:
                print(f'  [{event["at"]}] {event["kind"]}: {event["payload"]}')
            seen = len(run['events'])
            if run['status'] in terminal:
                print(f'run finished: {run["status"]}')
                return 0 if run['status'] == 'completed' else 1
            time.sleep(0.3)

        print('TIMED OUT waiting for the run to finish')
        return 1


if __name__ == '__main__':
    sys.exit(main())
