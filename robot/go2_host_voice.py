#!/usr/bin/env python3
"""Host voice daemon for the physical dog: speak the app's `say` commands, then listen.

The simulator's viewer plays speech and captures replies for the demo. This is
the hardware counterpart: it runs on the computer attached to the Go2 (the
Mac or GX10 with the USB speakerphone), polls the app backend for queued
`say` commands, renders them with the existing offline text-to-speech
adapter, plays them on the host, and reports accepted / executing /
completed / failed receipts with `source: host`. When the app opens a reply
window (`pending_checkin.phase == awaiting_reply`) it runs the live,
VAD-gated microphone listener once for that event.

It never touches motion: goto, look, stop and patrol commands are left in the
queue for the motion adapter, and are counted so the operator can see them.
Playback status describes the process, not human audibility. A completed
receipt means the host finished playing the clip, not that the resident heard it.

Run from the repository root:
  .cache/dimos/.venv/bin/python robot/go2_host_voice.py --app-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go2_probe import safe_error  # noqa: E402

SCRIPT_VERSION = "go2-host-voice/0.1"
MOTION_COMMANDS = {"goto", "look", "stop", "resume", "patrol", "turn"}
HANDLED_MEMORY = 200


def _say(text: str) -> None:
    print(f"go2-host-voice: {text}", file=sys.stderr, flush=True)


def _headers() -> dict:
    token = os.environ.get("ANNIE_API_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


async def run_host_voice(*, app_url: str, speaker, player, listener, http_transport=None,
                         poll_s: float = 0.25, duration_s: float | None = None, status=_say) -> dict:
    """Poll the app; speak `say` commands with receipts; listen when a reply window opens.

    speaker: async callable(text, command_id) -> {'file_path', 'duration_s', ...}
    player: blocking callable(path, duration_s); run in a worker thread
    listener: blocking callable() -> reply dict; run in a worker thread
    """
    loop = asyncio.get_running_loop()
    report = {"script": SCRIPT_VERSION, "app_url": app_url, "spoken": 0, "failed": 0, "ignored_motion": 0,
              "listened": 0, "listen_errors": 0, "app_errors": 0, "last_reply": None,
              "last_error": None, "reason": None, "completed": False}
    handled: dict[str, str] = {}
    listened_events: set[str] = set()
    seen_motion: set[str] = set()

    async with httpx.AsyncClient(base_url=app_url, timeout=5, trust_env=False,
                                 transport=http_transport, headers=_headers()) as app:
        async def receipt(command_id: str, state: str, detail: str | None = None):
            response = await app.post(f"/commands/{command_id}/receipt",
                                      json={"status": state, "source": "host", "detail": detail})
            response.raise_for_status()

        async def speak(item: dict):
            cid = item["command_id"]
            handled[cid] = "started"
            if len(handled) > HANDLED_MEMORY:
                del handled[next(iter(handled))]
            try:
                await receipt(cid, "accepted", "Host voice daemon accepted the clip.")
                clip = await speaker(item["text"], cid)
                await receipt(cid, "executing", "Clip synthesized; host playback started.")
                # The speech adapter reports a repo-relative path; the player
                # resolves relative paths against its own output directory.
                clip_path = str(Path(clip["file_path"]).absolute())
                await asyncio.to_thread(player, clip_path, float(clip["duration_s"]))
                await receipt(cid, "completed", "Host playback process finished.")
                report["spoken"] += 1
                handled[cid] = "completed"
                status(f"spoke {cid[:8]}: {str(item['text'])[:60]!r}")
            except Exception as exc:
                report["failed"] += 1
                handled[cid] = "failed"
                err = safe_error(exc)
                report["last_error"] = err
                try:
                    await receipt(cid, "failed", f"Host voice failed: {err['type']}")
                except Exception as receipt_exc:  # best effort: the failure is already recorded
                    report["last_error"] = safe_error(receipt_exc)

        start = loop.time()
        try:
            while duration_s is None or loop.time() - start < duration_s:
                try:
                    response = await app.get("/commands")
                    response.raise_for_status()
                    queued = response.json()
                    for item in queued:
                        cid = item.get("command_id")
                        if not isinstance(cid, str) or item.get("status") not in ("queued", None):
                            continue
                        if item.get("cmd") == "say" and cid not in handled and isinstance(item.get("text"), str):
                            await speak(item)
                        elif item.get("cmd") in MOTION_COMMANDS and cid not in seen_motion:
                            seen_motion.add(cid)
                            report["ignored_motion"] += 1
                    response = await app.get("/status")
                    response.raise_for_status()
                    pending = (response.json() or {}).get("pending_checkin") or {}
                    event_id = pending.get("event_id")
                    if pending.get("phase") == "awaiting_reply" and isinstance(event_id, str) \
                            and event_id not in listened_events:
                        listened_events.add(event_id)
                        status(f"reply window open for {event_id[:8]}; listening")
                        try:
                            reply = await asyncio.to_thread(listener)
                            report["listened"] += 1
                            report["last_reply"] = {k: reply.get(k) for k in
                                                    ("outcome", "speech_ms", "decision", "utterance_id") if k in reply}
                            status(f"reply outcome={reply.get('outcome')} decision={reply.get('decision')}")
                        except Exception as exc:
                            report["listen_errors"] += 1
                            report["last_error"] = safe_error(exc)
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    report["app_errors"] += 1
                    report["last_error"] = safe_error(exc)
                await asyncio.sleep(poll_s)
            report["reason"] = "duration_complete" if duration_s is not None else "stopped"
        except asyncio.CancelledError:
            report["reason"] = "operator_cancelled"
    report["completed"] = report["reason"] in ("duration_complete", "stopped", "operator_cancelled")
    return report


def _build_real_parts(app_url: str, max_ms: int):
    from robot.simulation.live_listener import listen, silero_vad, sounddevice_recorder, _default_transcriber
    from robot.simulation.native_audio import NativeAudioPlayer
    from robot.simulation.speech import SpeechAdapter

    speech = SpeechAdapter()
    player = NativeAudioPlayer()
    transcriber = _default_transcriber()
    vad = silero_vad()

    def listener():
        with httpx.Client(base_url=app_url, timeout=5, trust_env=False, headers=_headers()) as client:
            return listen(app=client, recorder=sounddevice_recorder(), vad=vad,
                          transcriber=transcriber, max_ms=max_ms)
    return speech.speak, player.play, listener


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Speak app say-commands on this host and listen for replies.")
    parser.add_argument("--app-url", default="http://127.0.0.1:8000")
    parser.add_argument("--poll", type=float, default=0.25)
    parser.add_argument("--max-ms", type=int, default=8000, help="reply listening cap per check-in")
    parser.add_argument("--duration", type=float, help="stop after this many seconds (default: until Ctrl-C)")
    parser.add_argument("--output", help="write the JSON report here")
    args = parser.parse_args(argv)
    if not 0.05 <= args.poll <= 5 or not 500 <= args.max_ms <= 20000:
        parser.error("--poll must be 0.05-5 s and --max-ms 500-20000")
    speaker, player, listener = _build_real_parts(args.app_url, args.max_ms)
    _say("ready: speech, playback, VAD and transcriber loaded; polling the app")
    try:
        report = asyncio.run(run_host_voice(app_url=args.app_url, speaker=speaker, player=player,
                                            listener=listener, poll_s=args.poll, duration_s=args.duration))
    except KeyboardInterrupt:
        _say("interrupted")
        return 130
    payload = json.dumps(report)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload, flush=True)
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    code = main()
    # ctranslate2 (faster-whisper) and onnxruntime can abort inside their
    # destructors at interpreter shutdown ("recursive_mutex lock failed").
    # The report is already written and flushed, so exit without teardown.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
