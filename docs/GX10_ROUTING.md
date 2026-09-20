# Routing the physical demo through the GX10

Goal (team decision 2026-09-19 evening): the dog is a hand and an arm; every
piece of thinking runs on the GX10 (privacy) or, until the GX10 is wired in, on
the Mac. The contract between the brain and the dog is **command + video**
([`contract/body_commands.md`](../contract/body_commands.md)); the contract
between the phone app and the brain is the family message relay
([`contract/family_messages.md`](../contract/family_messages.md)).

## Who runs where

| Process | Host today | Host with the GX10 | Port | Talks to |
| --- | --- | --- | --- | --- |
| `robot/go2_body.py` (dog link: WebRTC, camera, LiDAR, host mic/speaker) | Mac (USB-tethered to the phone hotspot, `en7` 172.20.10.9) | Mac, unchanged (the Mac's Wi-Fi must stay on the venue network; the dog is on the hotspot) | 8001 | dog at 172.20.10.10 |
| `app_backend` (phone app API, family thread, WebSocket) | Mac | Mac | 8000 | errand `/dispatch` |
| `robot/go2_errand.py` (errand brain: find → say → listen → report) | Mac | **GX10** | 8002 | body 8001, app_backend 8000 |
| `robot/robot_backend` planner + vision (`/plan`, `/infer`) | Mac (Ollama / mlx on loopback) | **GX10** (Nemotron/vLLM on its own loopback) | 8004 | — |

Everything is on one LAN: the iPhone Personal Hotspot ("Henry's iPhone",
172.20.10.0/28). The dog and the GX10 join it over Wi-Fi; the Mac reaches it
over USB tethering so its own Wi-Fi association never changes (AGENTS.md rule).
The phone app reaches `app_backend` at the Mac's hotspot address.

## Wiring the GX10 (about ten minutes once it is on the hotspot)

1. Join the GX10 to the phone hotspot; note its address (`ip -4 addr`), e.g. `172.20.10.11`.
2. On the Mac, body + app backend:
   ```sh
   export UNITREE_AES_128_KEY=<from .cache/go2-private/connection.json>   # never commit
   export ANNIE_BODY_TOKEN=<shared secret>
   .cache/dimos/.venv/bin/python robot/go2_body.py --ip 172.20.10.10 --host 0.0.0.0 --port 8001
   # app_backend: ANNIE_ALLOWED_HOSTS must include 172.20.10.9 (the Mac) and 172.20.10.11 (the GX10);
   # ROBOT_BACKEND_URL=http://172.20.10.11:8002  ANNIE_INTERNAL_SECRET=<secret>
   ```
3. On the GX10, the brain:
   ```sh
   export ANNIE_BODY_URL=http://172.20.10.9:8001 ANNIE_BODY_TOKEN=<shared secret>
   export ANNIE_APP_URL=http://172.20.10.9:8000 ANNIE_INTERNAL_SECRET=<secret>
   python robot/go2_errand.py --host 0.0.0.0 --port 8002
   ```
4. Check: `curl http://172.20.10.11:8002/health` from the Mac,
   `curl -H "X-Body-Token: ..." http://172.20.10.9:8001/status` from the GX10,
   then send a message from the phone app and watch the thread fill.

## Moving inference onto the GX10 without touching code

`robot_backend`'s vision provider only accepts **loopback HTTP** in `local`
mode (a privacy guard: frames never go to a LAN address in the clear). Two ways
to keep that guard and still run the model on the GX10:

- **Run `robot_backend` on the GX10** with `ANNIE_VISION_BASE_URL=http://127.0.0.1:<port>/v1`
  pointing at the GX10's own OpenAI-compatible server (vLLM/NIM serving
  Nemotron or Qwen-VL). The Mac-side pieces then point their brain URL at the
  GX10 (`--brain-url http://172.20.10.11:8004` where the simulation bridge
  takes it).
- **Or tunnel**: on the Mac, `ssh -N -L 11434:127.0.0.1:<port> user@172.20.10.11`
  makes the GX10's endpoint appear at the Mac's loopback; leave
  `ANNIE_VISION_BASE_URL=http://127.0.0.1:11434/v1` and change only
  `ANNIE_VISION_MODEL`. Same env, same code, inference on the GX10.

Both keep `ANNIE_VISION_MODE=local`; no cloud key, no budget counter.

## What is verified and what is not

- Body vocabulary, receipts, refusals and the HTTP surface: simulated-dog tests
  (`robot/tests/test_go2_body.py`). Live on the dog so far: walk, circle, hello,
  stretch, patrol-and-greet (7 greetings), all through the same driver calls.
- Errand sequence and app events: fakes (`robot/tests/test_go2_errand.py`).
  End-to-end phone → GX10 → dog → phone has **not** been run yet; the GX10 has
  not been on the hotspot yet.
- Speech and listening happen on the body host (Mac), not on the dog. The
  planned Anker S330 path is unchanged; see `robot/SETUP.md`.
