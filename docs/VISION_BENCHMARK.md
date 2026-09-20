# Local Vision Latency Benchmark

`robot/simulation/vision_benchmark.py` measures real end-to-end vision latency on
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

    .venv/bin/python robot/simulation/vision_benchmark.py --sizes 320 256 \
        --out output/vision/benchmark_results.json

The script touches only `output/vision/` and makes no paid or external calls.

## Runtime comparison: Ollama vs MLX (2026-09-19)

`robot/simulation/vlm_runtime_benchmark.py` asks whether a local VLM can answer
"is there a person, what posture, where" in under one second. It sends the same
six frozen images, prompt, and output cap to Ollama and to in-process
[mlx-vlm](https://github.com/Blaizzy/mlx-vlm), and prints one JSON line per run
plus a summary.

**Scope: this is a latency benchmark plus a well-formed-JSON sanity check. It
does not measure accuracy.** None of the six images shows a person lying on a
floor, so nothing here says any model can detect a fall. See "Not established".

Prompt (repo style, shortened answer), temperature 0, `max_tokens` 24:

    Describe the camera view. Reply with ONLY minified JSON:
    {"person":bool,"posture":"standing|sitting|lying|unknown",
    "location":"floor|bed|chair|unknown"}. No markdown, no extra text.

The answer costs 15-16 tokens with the Qwen tokenizer, so the ~12-token target
is not reachable with this schema.

### Repeated frames hide Ollama's real latency

Ollama answered an image it had seen before 8-10x faster than one it had not,
even with six images interleaved. Changing only a 2x2-pixel corner per call
moved the same model from 369 ms to 2978 ms p50 in the table below (250 ms to
2660 ms in the earlier, lighter-load pass). Image shape is not the cause:
two never-seen frames sharing an already-seen 320x240 shape were still slow
(2.28 s, 2.64 s). MLX shows no such gap (control rows below), so the pixel stamp
itself is free.

A robot never sees the same frame twice, so `--frames novel` (unique pixels on
every call, the default) is the number that matters. `--frames repeat` is kept
only to show the best case. The 0.62-5.21 s range recorded above is consistent
with a mix of the two, though which frames those runs reused was not checked.

### Results

Apple M1 Max 64 GB, macOS 26.5.1. 320 px longest side, JPEG quality 80, one
untimed warmup pass, then 5 timed passes over 6 images (n = 30), round-robin.
p50/p95 are linear-interpolated. "JSON" is strict: the whole reply parses and
matches the schema; "lenient" also accepts the first `{...}` span.

| Runtime | Model id | Frames | Image tokens | Out tok | n | p50 ms | p95 ms | <1 s | JSON | Lenient |
|---|---|---|---|---|---|---|---|---|---|---|
| Ollama 0.33.3 | `qwen3-vl:2b-instruct` (Q4_K_M) | novel | ~1040-1085 est. | 15 | 30 | 2978 | 3172 | 0/30 | 100% | 100% |
| Ollama 0.33.3 | `qwen3-vl:2b-instruct` (Q4_K_M) | repeat | ~1040-1085 est. | 15 | 30 | 369 | 727 | 30/30 | 100% | 100% |
| mlx-vlm 0.7.1 | `mlx-community/Qwen3-VL-2B-Instruct-4bit` | novel | default (66-80) | 15-16 | 30 | **478** | **668** | 30/30 | 100% | 100% |
| mlx-vlm 0.7.1 | `mlx-community/Qwen3-VL-2B-Instruct-4bit` | novel | min budget (54-72) | 15-16 | 30 | 426 | 765 | 29/30 | 100% | 100% |
| mlx-vlm 0.7.1 | `mlx-community/Qwen3-VL-2B-Instruct-4bit` | repeat | default | 15-16 | 30 | 449 | 628 | 30/30 | 100% | 100% |
| mlx-vlm 0.7.1 | `LiquidAI/LFM2.5-VL-450M-MLX-4bit` | novel | default | 18-24 | 30 | 267 | 474 | 29/30 | 7% | 7% |
| mlx-vlm 0.7.1 | `LiquidAI/LFM2.5-VL-450M-MLX-4bit` | novel | min budget | 18-24 | 30 | 201 | 268 | 30/30 | 0% | 0% |
| mlx-vlm 0.7.1 | `mlx-community/Qwen3.5-2B-MLX-4bit` (thinking off) | novel | default | 24 (cap) | 30 | 563 | 1234 | 26/30 | 0% | 27% |
| mlx-vlm 0.7.1 | `mlx-community/Qwen3.5-2B-MLX-4bit` (thinking off) | novel | min budget | 24 (cap) | 30 | 512 | 626 | 30/30 | 0% | 17% |
| mlx-vlm 0.7.1 | `mlx-community/LFM2.5-VL-450M-6bit` | novel | default | - | 0 | - | - | - | 30 errors | - |

Image-token counts are given only for Qwen3-VL: prompt tokens minus the 57
non-image tokens of its chat template (checked by tokenizing the template).
"min budget" is `--image-tokens min`, which pins the processor to the model's
own floor (`max_pixels=65536` for Qwen, `max_image_tokens=64` for LFM); the
realized count still varies with aspect ratio. The knob did take effect: prompt
tokens fell from 123-137 to 111-129 (Qwen3-VL) and 134-148 to 122-140 (LFM).

Ollama reports 1098-1142 prompt tokens for a 320 px image. Assuming the same
text overhead, that is roughly 1040-1085 image tokens, so Ollama appears to
upscale small images to about 1000 tokens, which would explain why 256 px never
beat 320 px above. Whether that floor is configurable was not tested; global
Ollama settings were left untouched.

**Best sub-second configuration:** mlx-vlm 0.7.1 with
`mlx-community/Qwen3-VL-2B-Instruct-4bit` at 320 px and the model's default
image budget: 478 ms p50, 668 ms p95, all 30 samples under 1 s, 100%
well-formed JSON, 2.08 GB peak memory. That is about 6x faster than the same
model family on Ollama for novel frames. Ollama is sub-second only on repeated
frames, which a moving robot does not produce.

Per model:

- **Qwen3-VL-2B on MLX** is the only configuration that was both sub-second and
  well-formed. The minimum image budget lowered p50 by 10-13% in both passes
  but its p95 effect is inside the load noise, so the default is recommended.
- **LFM2.5-VL-450M** is the fastest and fails the sanity check: it emits
  `"posture":true`, echoes the schema, or nests `{"person":{"person":...`.
  Unusable with this prompt. A model-specific prompt or constrained decoding
  was not tried, to keep the prompt identical across models.
- **Qwen3.5-2B** ignores "no markdown": it wraps replies in a ```` ```json ````
  fence and pretty-prints, so the 24-token cap truncates most answers.
- **`mlx-community/LFM2.5-VL-450M-6bit`** loads but every call raises
  `ValueError: [broadcast_shapes] Shapes (320,768) and (1,256,768)`. Its
  `processor_config.json` ships `max_num_patches: 256` next to
  `max_image_tokens: 256` (which implies 1024); a 320x240 frame needs 320
  patches, and the minimum budget still needs 264. The official LiquidAI build
  ships `max_num_patches: 1024` and is unaffected.
- **Moondream2 was not run.** mlx-vlm 0.7.1 has a `moondream2` architecture, but
  no MLX build exists under mlx-community and `vikhyatk/moondream2` is 3.86 GB,
  which would have exceeded the ~6 GB download cap (4.1 GB was used).

### Measurement conditions

The machine was shared with another agent's live simulation (MuJoCo
`viewer.py` at ~220% CPU, plus bridge and app backend). 1-minute load average
on this 10-core machine was 54-131 during the table above; each summary line
records it as `loadavg_1m_start_end`. Numbers are therefore pessimistic relative
to an idle machine, and p95 in particular reflects load spikes. Idle-machine
latency was not measured.

The Ollama novel-frame row ran under lighter load (54 rising to 121) than the
MLX rows (110-130), so the ~6x gap is, if anything, understated.

An earlier pass at lower load (52-73, with a Hugging Face download running
concurrently) shows the sensitivity. Same settings, n = 30 each:

| Runtime | Model | Frames | Image tokens | p50 ms | p95 ms | JSON |
|---|---|---|---|---|---|---|
| Ollama | `qwen3-vl:2b-instruct` | novel | ~1040-1085 | 2660 | 2882 | 100% |
| Ollama | `qwen3-vl:2b-instruct` | repeat | ~1040-1085 | 250 | 439 | 100% |
| mlx-vlm | `Qwen3-VL-2B-Instruct-4bit` | novel | default | 355 | 419 | 100% |
| mlx-vlm | `Qwen3-VL-2B-Instruct-4bit` | novel | min budget | 308 | 337 | 100% |
| mlx-vlm | `LFM2.5-VL-450M-MLX-4bit` | novel | default | 188 | 356 | 7% |
| mlx-vlm | `LFM2.5-VL-450M-MLX-4bit` | repeat | default | 166 | 184 | 0% |

Single observations, not benchmarked: Ollama's first call after a cold start
took 22.35 s, and `/api/ps` reported 32.9 GB `size_vram` for the loaded model.
MLX model load took 0.9-3.1 s and the first call after load 0.9-5.8 s.

### Not established

- **Accuracy.** No image shows a person lying down. Both Qwen3-VL runtimes
  answered `"posture":"standing","location":"floor"` for empty rooms
  (`"person":false`), for a head-and-shoulders photo, and (Ollama) for five
  seated people. "standing" and "floor" are the first options in each enum, so
  these may be defaults rather than observations. **Do not treat
  `location == "floor"` as "person on the floor"**; a labelled evaluation with
  lying-person frames is needed before any safety claim.
- **Answer stability.** With temperature 0, MLX Qwen3-VL gave `"unknown"` for 3
  and `"chair"` for 2 of 5 novel-frame variants of one photo: a 2x2-pixel change
  was enough to flip `location`.
- **Why Ollama is faster on repeats.** The content-keyed effect is measured; a
  per-image embedding cache is the likely cause but Ollama's source was not
  read.
- **Served MLX latency.** MLX was timed as in-process `generate()` calls. An
  HTTP wrapper such as `mlx_vlm.server` adds overhead that was not measured.

### Inputs

| Image | Sent size | Content |
|---|---|---|
| `output/current-dog-camera.jpg` | 320x240 | sim robot camera, empty room |
| `output/house-downstairs.jpg` | 320x240 | overhead house render, no person |
| `output/house-resident.jpg` | 320x240 | same view, seated mannequin ~10 px tall |
| `ultralytics/assets/bus.jpg` | 240x320 | people standing by a bus |
| `ultralytics/assets/zidane.jpg` | 320x180 | two men, head and shoulders |
| `moondream/assets/how-to-be-a-people-person-1662995088.jpg` | 320x168 | five people sitting |

The last three ship inside the `ultralytics` and `moondream` packages in
`.cache/dimos/.venv`; nothing was downloaded. `output/` and `.cache/` are
ignored, so each JSON line records `source_sha256` and `sent_sha256` to make
the inputs checkable.

### Models, revisions, and licenses

Licenses are as stated on each model card or by `ollama show`.

| Model | Revision | Size | License |
|---|---|---|---|
| Ollama `qwen3-vl:2b-instruct` | digest `ea422f1e7365` | 1.89 GB | Apache-2.0 |
| `mlx-community/Qwen3-VL-2B-Instruct-4bit` | `9c4f5209e57b31f4b9dfba735de3fb983739c9cc` | 1.80 GB | Apache-2.0 |
| `mlx-community/Qwen3.5-2B-MLX-4bit` | `93760be4f1f69842a46bc13dbdc0f19e291392a3` | 1.75 GB | Apache-2.0 |
| `LiquidAI/LFM2.5-VL-450M-MLX-4bit` | `f19926f17a25164d4cbcdc16d9eaf4714b807cfb` | 0.38 GB | LFM Open License v1.0 |
| `mlx-community/LFM2.5-VL-450M-6bit` | `9da6e34d06691a7c53943f4a21dc044b48c38cec` | 0.47 GB | LFM Open License v1.0 |
| `vikhyatk/moondream2` (not run) | `6b714b26eea5cbd9f31e4edb2541c170afa935ba` | 3.86 GB | Apache-2.0 |

The LFM Open License v1.0 limits commercial use by entities at or above a
$10M annual-revenue threshold (its Section 5); flagged for compliance review.
Runtime: mlx 0.32.2, mlx-vlm 0.7.1, transformers 5.17.0, Python 3.12.13.

### Commands

    uv venv .cache/mlx-vlm/.venv --python 3.12
    uv pip install --python .cache/mlx-vlm/.venv/bin/python mlx-vlm

    U=.cache/dimos/.venv/lib/python3.12/site-packages
    IMGS=(output/current-dog-camera.jpg output/house-downstairs.jpg \
      output/house-resident.jpg $U/ultralytics/assets/bus.jpg \
      $U/ultralytics/assets/zidane.jpg \
      $U/moondream/assets/how-to-be-a-people-person-1662995088.jpg)

    # Ollama baseline (repo venv); add --frames repeat for the best case
    .venv/bin/python robot/simulation/vlm_runtime_benchmark.py \
        --runtime ollama --model qwen3-vl:2b-instruct --images "${IMGS[@]}" \
        --runs 5 --max-tokens 24 --longest-side 320

    # MLX (mlx-vlm venv); add --image-tokens min for the minimum image budget
    export HF_HOME=$PWD/.cache/mlx-vlm/hf
    .cache/mlx-vlm/.venv/bin/python robot/simulation/vlm_runtime_benchmark.py \
        --runtime mlx --model mlx-community/Qwen3-VL-2B-Instruct-4bit \
        --revision 9c4f5209e57b31f4b9dfba735de3fb983739c9cc \
        --images "${IMGS[@]}" --runs 5 --max-tokens 24 --longest-side 320

Swap `--model`/`--revision` for the other rows. Use the array form for the
image list: zsh does not word-split an unquoted `$IMGS`. The runner writes no
files; redirect stdout to keep the JSON lines.
