#!/usr/bin/env python3
"""Measure local vision (qwen3-vl via Ollama) latency at reduced resolutions.

Fetches a real observation frame from the simulation viewer (GET /observation,
JPEG base64, with frame_id/ts preserved), downsamples it with Pillow, and calls
the local Ollama OpenAI-compatible endpoint. Read-only elsewhere: this file and
docs/VISION_BENCHMARK.md are the only files it touches. Never kills other
Ollama models or changes global settings.
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


PROMPT = (
    "Describe the camera view. Reply with ONLY minified JSON: "
    '{"person":bool,"posture":"standing|sitting|lying|unknown",'
    '"location":"<max 20 words>","confidence":0.0-1.0,'
    '"caption":"<max 80 chars>"}. No markdown, no extra text.'
)


def downscale_jpeg(jpeg: bytes, longest_side: int, quality: int = 80) -> bytes:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    img.thumbnail((longest_side, longest_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def call_ollama(
    client: httpx.Client, base_url: str, model: str, jpeg: bytes, max_tokens: int
) -> tuple[dict, float]:
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }
    t0 = time.monotonic()
    r = client.post(f"{base_url}/chat/completions", json=payload, timeout=60.0)
    elapsed = time.monotonic() - t0
    r.raise_for_status()
    body = r.json()
    text = body["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"raw": text}
    usage = body.get("usage", {})
    return {
        "elapsed_s": round(elapsed, 2),
        "completion_tokens": usage.get("completion_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "parsed": parsed,
    }, elapsed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--viewer-url", default="http://127.0.0.1:8766")
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--model", default="qwen3-vl:2b-instruct")
    ap.add_argument("--sizes", type=int, nargs="+", default=[256, 320])
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--out", default="output/vision/benchmark_results.json")
    args = ap.parse_args()

    # One real observation from the running viewer; preserve its identity.
    with httpx.Client() as hc:
        resp = hc.get(f"{args.viewer_url}/observation", timeout=10.0)
        resp.raise_for_status()
        obs = resp.json()
    jpeg = base64.b64decode(obs["jpeg_b64"])
    frame_id, ts = obs["frame_id"], obs["ts"]
    orig = Image.open(io.BytesIO(jpeg))
    print(f"frame_id={frame_id} ts={ts} source_size={orig.size}", file=sys.stderr)

    results = {
        "model": args.model,
        "frame_id": frame_id,
        "capture_ts_ms": ts,
        "source_size": list(orig.size),
        "runs": [],
    }
    with httpx.Client() as client:
        for size in args.sizes:
            small = downscale_jpeg(jpeg, size)
            entry = {
                "longest_side": size,
                "jpeg_bytes": len(small),
            }
            try:
                res, _ = call_ollama(client, args.ollama_url, args.model, small, args.max_tokens)
                entry.update(res)
                print(
                    f"size={size} {res['elapsed_s']}s tokens={res['completion_tokens']} "
                    f"parsed={res['parsed']}",
                    file=sys.stderr,
                )
            except Exception as exc:  # noqa: BLE001 - benchmark must not crash mid-run
                entry["error"] = repr(exc)
                print(f"size={size} error={exc!r}", file=sys.stderr)
            results["runs"].append(entry)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
