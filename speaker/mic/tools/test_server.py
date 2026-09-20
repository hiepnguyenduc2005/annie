#!/usr/bin/env python3
"""Minimal stand-in backend for trying the Annie Audio iPhone app.

    pip install websockets
    python tools/test_server.py --mode tone      # phone speaker plays a beep
    python tools/test_server.py --mode echo      # phone hears its own mic, delayed
    python tools/test_server.py --mode sink      # just count what the phone sends

Speaks the app's protocol: binary WebSocket frames of raw PCM, signed 16-bit
little-endian, mono, 16 kHz. Prints how much audio it receives from the phone.

`echo` will feed back through the phone's loudspeaker into its microphone; use
headphones or keep the volume low.
"""
import argparse
import asyncio
import math
import struct
import time

import websockets

RATE = 16_000
FRAME_SECONDS = 0.020
FRAME_SAMPLES = int(RATE * FRAME_SECONDS)


def tone_frame(offset: int, hz: float = 440.0, amplitude: float = 0.2) -> bytes:
    samples = (
        int(amplitude * 32767 * math.sin(2 * math.pi * hz * (offset + i) / RATE))
        for i in range(FRAME_SAMPLES)
    )
    return struct.pack(f"<{FRAME_SAMPLES}h", *samples)


async def send_tone(ws) -> None:
    """One second of beep every three seconds, paced in real time."""
    offset, start = 0, time.monotonic()
    while True:
        for _ in range(int(1 / FRAME_SECONDS)):
            await ws.send(tone_frame(offset))
            offset += FRAME_SAMPLES
            start += FRAME_SECONDS
            await asyncio.sleep(max(0.0, start - time.monotonic()))
        await asyncio.sleep(2)
        start = time.monotonic()


async def handler(ws, mode: str) -> None:
    print(f"phone connected: {ws.remote_address}")
    sender = asyncio.create_task(send_tone(ws)) if mode == "tone" else None
    received = packets = 0
    last_report = time.monotonic()
    try:
        async for message in ws:
            if isinstance(message, str):
                print(f"unexpected text message ({len(message)} chars)")
                continue
            received += len(message)
            packets += 1
            if mode == "echo":
                await ws.send(message)
            if time.monotonic() - last_report >= 2:
                print(f"received {received / 1024:.0f} KiB in {packets} packets "
                      f"(~{received / 2 / RATE:.1f} s of audio)")
                last_report = time.monotonic()
    finally:
        if sender:
            sender.cancel()
        print("phone disconnected")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--mode", choices=["tone", "echo", "sink"], default="tone")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    async with websockets.serve(lambda ws: handler(ws, args.mode), args.host, args.port):
        print(f"listening on ws://{args.host}:{args.port}/audio  (mode: {args.mode})")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
