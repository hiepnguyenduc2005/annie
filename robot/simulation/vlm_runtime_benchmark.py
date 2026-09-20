#!/usr/bin/env python3
"""Compare local VLM runtimes on the robot's short person/posture question.

Times the same frozen JPEGs, prompt, and output cap against either the local
Ollama OpenAI-compatible endpoint or an in-process mlx-vlm model. This is a
LATENCY benchmark plus a well-formed-JSON sanity check; it does not measure
classification accuracy.

Each source image is downscaled once (Pillow thumbnail, JPEG quality 80, the
same recipe as vision_benchmark.py) and both runtimes receive the identical
base64 JPEG, so decode and preprocessing sit inside every timed call.

--frames novel (default) stamps a unique 2x2 corner on every call so no two
calls share pixels. This matters: Ollama answered a previously seen image
~10x faster than a never-seen one here, and a robot's frames are always new.
--frames repeat resends identical bytes each pass and measures that best case.
Payloads are built before timing starts; calls go round-robin over the images.

Prints one JSON line per timed run and a final summary line to stdout;
progress goes to stderr. Never prints image bytes. Network use is limited to
the local Ollama server and, for --runtime mlx, the Hugging Face download of
the requested model. Writes no files.

Run --runtime ollama from the repo .venv and --runtime mlx from
.cache/mlx-vlm/.venv; see docs/VISION_BENCHMARK.md for exact commands.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Callable

from PIL import Image


PROMPT = (
    "Describe the camera view. Reply with ONLY minified JSON: "
    '{"person":bool,"posture":"standing|sitting|lying|unknown",'
    '"location":"floor|bed|chair|unknown"}. No markdown, no extra text.'
)
POSTURES = {"standing", "sitting", "lying", "unknown"}
LOCATIONS = {"floor", "bed", "chair", "unknown"}


def downscale(jpeg: bytes, longest_side: int) -> Image.Image:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    img.thumbnail((longest_side, longest_side))
    return img


def encode_variant(img: Image.Image, variant: int | None, quality: int = 80) -> bytes:
    """JPEG-encode; a variant index stamps a unique 2x2 corner so no call repeats pixels.

    A robot never sees the same frame twice, but a runtime may memoise work per
    image. Unique pixels keep every timed call on the cold-image path.
    """
    if variant is not None:
        img = img.copy()
        colour = ((variant * 37 + 11) % 256, (variant * 91 + 5) % 256, (variant * 53 + 3) % 256)
        for xy in ((0, 0), (1, 0), (0, 1), (1, 1)):
            img.putpixel(xy, colour)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def check_answer(text: str) -> dict[str, Any]:
    """Strict: the whole reply is the JSON object. Lenient: first {...} span."""
    def valid(obj: Any) -> bool:
        return (
            isinstance(obj, dict)
            and isinstance(obj.get("person"), bool)
            and obj.get("posture") in POSTURES
            and obj.get("location") in LOCATIONS
        )

    parsed: Any = None
    strict = False
    try:
        parsed = json.loads(text.strip())
        strict = valid(parsed)
    except json.JSONDecodeError:
        pass
    lenient = strict
    if not strict:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                candidate = json.loads(text[start : end + 1])
                if valid(candidate):
                    parsed, lenient = candidate, True
            except json.JSONDecodeError:
                pass
    return {
        "json_ok": strict,
        "json_ok_lenient": lenient,
        "parsed": parsed if isinstance(parsed, dict) else None,
    }


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (numpy's default method)."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def make_ollama_caller(args: argparse.Namespace) -> tuple[Callable[[str], dict[str, Any]], dict[str, Any]]:
    import httpx

    client = httpx.Client()
    base = args.ollama_url.rstrip("/")
    meta: dict[str, Any] = {}
    try:
        tags = client.get(base.removesuffix("/v1") + "/api/tags", timeout=5.0).json()
        for entry in tags.get("models", []):
            if entry.get("name") == args.model:
                meta["ollama_digest"] = entry.get("digest")
                meta["quantization"] = entry.get("details", {}).get("quantization_level")
    except Exception as exc:  # noqa: BLE001 - metadata is best effort
        meta["ollama_tags_error"] = type(exc).__name__

    def call(data_url: str) -> dict[str, Any]:
        payload = {
            "model": args.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": args.max_tokens,
            "temperature": 0,
            "stream": False,
        }
        r = client.post(f"{base}/chat/completions", json=payload, timeout=args.timeout_s)
        r.raise_for_status()
        body = r.json()
        usage = body.get("usage", {})
        return {
            "text": body["choices"][0]["message"]["content"] or "",
            "prompt_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        }

    return call, meta


def _apply_min_image_tokens(processor: Any) -> dict[str, Any]:
    """Pin the image budget to the model's own configured minimum, if exposed."""
    ip = getattr(processor, "image_processor", None)
    if ip is None:
        return {"image_token_budget": "unsupported: no image_processor"}
    # LFM2-VL style: explicit token bounds.
    if hasattr(ip, "min_image_tokens") and hasattr(ip, "max_image_tokens"):
        ip.max_image_tokens = ip.min_image_tokens
        return {"image_token_budget": f"max_image_tokens={ip.min_image_tokens}"}
    # Qwen-VL style: pixel bounds (shortest_edge=min_pixels, longest_edge=max_pixels).
    size = getattr(ip, "size", None)
    if isinstance(size, dict) and "shortest_edge" in size and "longest_edge" in size:
        floor = int(size["shortest_edge"])
        size["longest_edge"] = floor
        for attr in ("max_pixels",):
            if hasattr(ip, attr):
                setattr(ip, attr, floor)
        return {"image_token_budget": f"max_pixels={floor}"}
    if hasattr(ip, "min_pixels") and hasattr(ip, "max_pixels"):
        ip.max_pixels = ip.min_pixels
        return {"image_token_budget": f"max_pixels={ip.min_pixels}"}
    return {"image_token_budget": "unsupported: no known knob"}


def make_mlx_caller(args: argparse.Namespace) -> tuple[Callable[[str], dict[str, Any]], dict[str, Any]]:
    import mlx_vlm
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template

    t0 = time.monotonic()
    model, processor = load(args.model, revision=args.revision)
    meta: dict[str, Any] = {
        "mlx_vlm": mlx_vlm.__version__,
        "revision": args.revision,
        "load_s": round(time.monotonic() - t0, 2),
    }
    if args.image_tokens == "min":
        meta.update(_apply_min_image_tokens(processor))
    else:
        meta["image_token_budget"] = "model default"
    # enable_thinking=False keeps hybrid-thinking models (Qwen3.5) in answer mode.
    prompt = apply_chat_template(
        processor, model.config, PROMPT, num_images=1, enable_thinking=False
    )

    def call(data_url: str) -> dict[str, Any]:
        result = generate(
            model,
            processor,
            prompt,
            image=[data_url],
            max_tokens=args.max_tokens,
            temperature=0.0,
            verbose=False,
        )
        return {
            "text": result.text,
            "prompt_tokens": result.prompt_tokens,
            "output_tokens": result.generation_tokens,
            "prompt_tps": round(result.prompt_tps, 1),
            "generation_tps": round(result.generation_tps, 1),
            "peak_memory_gb": round(result.peak_memory, 2),
        }

    return call, meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runtime", choices=["ollama", "mlx"], required=True)
    ap.add_argument("--model", required=True, help="Ollama tag or Hugging Face repo id")
    ap.add_argument("--images", nargs="+", required=True, help="frozen JPEG/PNG paths")
    ap.add_argument("--runs", type=int, default=5, help="timed passes over the image set")
    ap.add_argument("--max-tokens", type=int, default=24)
    ap.add_argument("--longest-side", type=int, default=320)
    ap.add_argument("--warmup", type=int, default=1, help="untimed passes over the image set")
    ap.add_argument("--revision", default=None, help="mlx: Hugging Face commit to pin")
    ap.add_argument(
        "--image-tokens",
        choices=["default", "min"],
        default="default",
        help="mlx: 'min' pins the processor to the model's configured minimum image budget",
    )
    ap.add_argument(
        "--frames",
        choices=["novel", "repeat"],
        default="novel",
        help="novel: unique pixels per call (what a robot sees); repeat: identical bytes each pass",
    )
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--timeout-s", type=float, default=60.0)
    args = ap.parse_args()
    if args.runs < 1 or args.warmup < 0:
        ap.error("--runs must be >= 1 and --warmup >= 0")

    sources = []
    for raw in args.images:
        path = Path(raw)
        source = path.read_bytes()
        sources.append(
            {
                "image": path.name,
                "source_sha256": hashlib.sha256(source).hexdigest()[:16],
                "small": downscale(source, args.longest_side),
            }
        )

    # Every payload is built before timing starts: passes 0..warmup-1 are warmup.
    def payload(pass_index: int, image_index: int) -> dict[str, Any]:
        src = sources[image_index]
        variant = pass_index * len(sources) + image_index if args.frames == "novel" else None
        jpeg = encode_variant(src["small"], variant)
        return {
            "image": src["image"],
            "source_sha256": src["source_sha256"],
            "sent_sha256": hashlib.sha256(jpeg).hexdigest()[:16],
            "sent_size": list(src["small"].size),
            "sent_bytes": len(jpeg),
            "data_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(),
        }

    passes = [
        [payload(p, i) for i in range(len(sources))] for p in range(args.warmup + args.runs)
    ]
    sent = [f["sent_sha256"] for p in passes for f in p]
    if args.frames == "novel" and len(set(sent)) != len(sent):
        print("novel-frame payloads are not unique; refusing to time", file=sys.stderr)
        return 2

    factory = make_ollama_caller if args.runtime == "ollama" else make_mlx_caller
    call, meta = factory(args)
    # Latency on a shared machine is only interpretable next to its load.
    load_start = os.getloadavg()[0]

    for i in range(args.warmup):
        for frame in passes[i]:
            t0 = time.monotonic()
            try:
                call(frame["data_url"])
                note = f"{time.monotonic() - t0:.2f}s"
            except Exception as exc:  # noqa: BLE001 - timed rows will record the failure
                note = f"error={repr(exc)[:160]}"
            print(f"warmup {i} {frame['image']} {note}", file=sys.stderr)

    rows: list[dict[str, Any]] = []
    for run in range(args.runs):
        for frame in passes[args.warmup + run]:
            row: dict[str, Any] = {
                "runtime": args.runtime,
                "model": args.model,
                "run": run,
                **{k: v for k, v in frame.items() if k != "data_url"},
            }
            t0 = time.monotonic()
            try:
                out = call(frame["data_url"])
                row["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
                text = out.pop("text")
                row.update(out)
                row.update(check_answer(text))
                row["text"] = text[:200]
            except Exception as exc:  # noqa: BLE001 - benchmark must not crash mid-run
                row["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
                row["error"] = repr(exc)[:200]
            rows.append(row)
            print(json.dumps(row), flush=True)

    ok = [r for r in rows if "error" not in r]
    lat = [r["elapsed_ms"] for r in ok]
    out_tokens = [r["output_tokens"] for r in ok if r.get("output_tokens") is not None]
    prompt_tokens = [r["prompt_tokens"] for r in ok if r.get("prompt_tokens") is not None]
    per_image = {
        f["image"]: round(percentile([r["elapsed_ms"] for r in ok if r["image"] == f["image"]], 0.5), 1)
        for f in sources
        if any(r["image"] == f["image"] for r in ok)
    }
    summary = {
        "summary": True,
        "runtime": args.runtime,
        "model": args.model,
        **meta,
        "host": f"{platform.machine()} {platform.platform()}",
        "cpu_count": os.cpu_count(),
        "loadavg_1m_start_end": [round(load_start, 1), round(os.getloadavg()[0], 1)],
        "frames": args.frames,
        "unique_payloads": len(set(sent)),
        "longest_side": args.longest_side,
        "max_tokens": args.max_tokens,
        "warmup_passes": args.warmup,
        "n": len(ok),
        "errors": len(rows) - len(ok),
        "p50_ms": round(percentile(lat, 0.5), 1) if lat else None,
        "p95_ms": round(percentile(lat, 0.95), 1) if lat else None,
        "min_ms": min(lat) if lat else None,
        "max_ms": max(lat) if lat else None,
        "output_tokens_range": [min(out_tokens), max(out_tokens)] if out_tokens else None,
        "prompt_tokens_range": [min(prompt_tokens), max(prompt_tokens)] if prompt_tokens else None,
        "json_ok_rate": round(sum(r["json_ok"] for r in ok) / len(ok), 3) if ok else None,
        "json_ok_lenient_rate": round(sum(r["json_ok_lenient"] for r in ok) / len(ok), 3) if ok else None,
        "per_image_p50_ms": per_image,
    }
    print(json.dumps(summary), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
