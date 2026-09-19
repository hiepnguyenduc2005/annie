#!/usr/bin/env python3
"""Print PASS/FAIL for reaching ROBOT_BACKEND_URL, so a demo-day network problem
is distinguishable from a code problem in about five seconds.

Run from the repository root:
    .venv/bin/python app_backend/scripts/check_robot_backend.py
Reads ROBOT_BACKEND_URL from the environment (see .env.example); pass
--url to override.
"""
import argparse
import os
import sys
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.getenv('ROBOT_BACKEND_URL', ''))
    parser.add_argument('--timeout-s', type=float, default=3.0)
    args = parser.parse_args()

    url = args.url.rstrip('/')
    if not url:
        print('FAIL: ROBOT_BACKEND_URL is not set (see .env.example)')
        return 1

    health_url = f'{url}/health'
    start = time.monotonic()
    try:
        response = httpx.get(health_url, timeout=args.timeout_s)
    except httpx.HTTPError as exc:
        print(f'FAIL: could not reach {health_url} ({type(exc).__name__}: {exc})')
        return 1
    elapsed_ms = (time.monotonic() - start) * 1000

    if response.status_code == 200:
        print(f'PASS: {health_url} responded {response.status_code} in {elapsed_ms:.0f} ms: {response.text.strip()}')
        return 0
    print(f'FAIL: {health_url} responded {response.status_code} in {elapsed_ms:.0f} ms')
    return 1


if __name__ == '__main__':
    sys.exit(main())
