#!/usr/bin/env python3
"""Bounded paid probe: real simulation frame -> Gemini 2.5 Flash Lite (floor).

Fetches one real observation frame from the simulation viewer (GET
/observation), downsamples it, and sends a single OpenRouter chat request.
Saves timing/usage only (never the API key) to output/vision/fast-probe*.json.
At most 2 paid calls per budget reservation; no identical retries, no fallback.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path

import httpx
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
MODEL = "google/gemini-2.5-flash-lite:floor"
PRICE_IN_PER_M = 0.10
PRICE_OUT_PER_M = 0.40
RESERVED_PER_CALL = 0.50
MAX_CALLS = 2
RESERVATION_PATH = ROOT / ".data" / "fast-vision-reservation.json"

PROMPT = (
    "Describe this camera view. Reply with ONLY minified JSON: "
    '{"person":bool,"posture":"standing|sitting|lying|unknown",'
    '"location":"<max 20 words>","confidence":0.0-1.0,'
    '"caption":"<max 100 chars>"}. No markdown, no extra text.'
)


def load_api_key():
    for env_name in (".env",):
        env_path = ROOT / env_name
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("OPENROUTER_API_KEY missing in .env")


def load_reservation():
    RESERVATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not RESERVATION_PATH.exists():
        RESERVATION_PATH.write_text(json.dumps({
            "task": "fast_vision_candidate",
            "provider": "openrouter",
            "model": MODEL,
            "reserved_usd_per_call": RESERVED_PER_CALL,
            "max_calls": MAX_CALLS,
            "reserved_usd_total": round(RESERVED_PER_CALL * MAX_CALLS, 2),
            "calls_made": 0,
            "actual_spent_usd": 0.0,
            "note": "Owner must count this reservation within the global 20 USD cap.",
        }, indent=2) + "\n")
    return json.loads(RESERVATION_PATH.read_text())


def save_reservation(res):
    res["updated_ts_ms"] = int(time.time() * 1000)
    RESERVATION_PATH.write_text(json.dumps(res, indent=2) + "\n")


def downscale_jpeg(jpeg, longest_side, quality=80):
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    img.thumbnail((longest_side, longest_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--viewer-url", default="http://127.0.0.1:8766")
    ap.add_argument("--longest-side", type=int, default=384)
    ap.add_argument("--deadline-s", type=float, default=10.0)
    ap.add_argument("--out-prefix", default="output/vision/fast-probe")
    args = ap.parse_args()
    if not 320 <= args.longest_side <= 384:
        raise SystemExit("--longest-side must be within 320..384")

    reservation = load_reservation()
    if reservation.get("calls_made", 0) >= MAX_CALLS:
        raise SystemExit("call budget exhausted (%d calls made); refusing to spend" % MAX_CALLS)

    key = load_api_key()
    t_start = time.monotonic()
    result = {"model": MODEL, "probe_started_ts_ms": int(time.time() * 1000)}

    try:
        with httpx.Client() as hc:
            t_fetch = time.monotonic()
            obs_resp = hc.get(args.viewer_url + "/observation", timeout=10.0)
            obs_resp.raise_for_status()
            result["frame_fetch_s"] = round(time.monotonic() - t_fetch, 3)
            obs = obs_resp.json()
        jpeg = base64.b64decode(obs["jpeg_b64"])
        small = downscale_jpeg(jpeg, args.longest_side)
        result.update({
            "frame_id": obs["frame_id"],
            "capture_ts_ms": obs["ts"],
            "source_size": list(Image.open(io.BytesIO(jpeg)).size),
            "probe_size": list(Image.open(io.BytesIO(small)).size),
            "probe_jpeg_bytes": len(small),
        })

        data_url = "data:image/jpeg;base64," + base64.b64encode(small).decode()
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            "max_tokens": 128,
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
            "reasoning": {"enabled": False},
            "provider": {"max_price": {"prompt": 0.11, "completion": 0.41}},
        }
        headers = {"Authorization": "Bearer " + key}
        t_req = time.monotonic()
        with httpx.Client(timeout=args.deadline_s) as hc:
            api_resp = hc.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload, headers=headers,
            )
        result["request_latency_s"] = round(time.monotonic() - t_req, 3)
        result["http_status"] = api_resp.status_code
        api_resp.raise_for_status()
        body = api_resp.json()
        content_txt = body["choices"][0]["message"]["content"]
        usage = body.get("usage", {})
        cost = (usage.get("prompt_tokens", 0) * PRICE_IN_PER_M
                + usage.get("completion_tokens", 0) * PRICE_OUT_PER_M) / 1e6
        result["usage"] = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "actual_cost_usd": round(cost, 6),
        }
        try:
            parsed = json.loads(content_txt)
        except json.JSONDecodeError:
            parsed = None
            result["raw_content"] = content_txt[:400]
        result["parsed"] = parsed
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)

    total_s = round(time.monotonic() - t_start, 3)
    result["total_pipeline_s"] = total_s
    latency_ok = total_s < 5.0 and result.get("request_latency_s", 99) < 5.0
    result["success"] = bool(isinstance(result.get("parsed"), dict) and latency_ok)
    result["latency_under_5s"] = latency_ok

    reservation["calls_made"] = reservation.get("calls_made", 0) + 1
    actual = result.get("usage", {}).get("actual_cost_usd")
    if actual is not None:
        reservation["actual_spent_usd"] = round(
            reservation.get("actual_spent_usd", 0.0) + actual, 6)
    save_reservation(reservation)

    out_dir = ROOT / "output" / "vision"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (Path(args.out_prefix).name + "-" + str(int(time.time() * 1000)) + ".json")
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({
        "out": str(out_path.relative_to(ROOT)),
        "success": result["success"],
        "total_pipeline_s": total_s,
        "request_latency_s": result.get("request_latency_s"),
        "parsed": result.get("parsed"),
        "error": result.get("error"),
        "calls_made": reservation["calls_made"],
    }, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
