# Local Vision Latency Benchmark

`simulation/vision_benchmark.py` measures real end-to-end vision latency on
the local Ollama OpenAI endpoint (`qwen3-vl:2b-instruct`). It fetches a live
frame from the simulation viewer (`GET /observation`: 640x480 JPEG with
`frame_id`/`ts`), downsamples it with Pillow, calls
`http://127.0.0.1:11434/v1/chat/completions`, and records parsed JSON plus
timings. Frame identity and capture timestamps are preserved verbatim; results
land in `output/vision/`.

## Recommended config (under the 5s app-freshness budget)

    longest side 320 px, JPEG quality 80, max_tokens 128, temperature 0

Strict JSON prompt returning person/posture/location/confidence/caption
(caption <= 80 chars).

## Measured results (M1 Max 64 GB, warm model, one in-flight call)

| Config          | Warm latency | Notes                          |
|-----------------|--------------|--------------------------------|
| 640x480 (prev)  | 16.36 s      | Baseline; 30 s cold timeout    |
| 320 px          | 0.62-5.21 s  | Typical warm ~1-4 s            |
| 256 px          | 1.05-3.08 s  | No consistent win over 320     |

First call after idle adds ~2-4 s image-preprocessing overhead. All observed
warm runs completed well under the 5 s freshness budget. Output stayed valid
strict JSON in every run (30-38 completion tokens).

## Classification caveat

On live `floor_lying-000`/`safe_bed-000` observations the model reported
"no person": the mannequin settles out of the robot-front camera frustum after
scene load (scene physics, not a model limitation). Feed the same scene's
staged render PNG and the model correctly reports "a person lying on the
floor" at 320 px in 3.45 s. Demo runs should verify resident visibility after
scene load or re-point the camera before claiming classification accuracy.

## Usage

    .venv/bin/python simulation/vision_benchmark.py --sizes 320 256 \
        --out output/vision/benchmark_results.json

The script touches only `output/vision/` and makes no paid or external calls.
