#!/usr/bin/env bash
# Cold start of Annie's brain side on the ASUS GX10 (Ubuntu ARM64, NVIDIA GB10). Idempotent; re-run freely.
# Usage (on the GX10): bash robot/gx10_setup.sh            # from a clone of this repository
#   or:                curl -fsSL <raw url of this file> | bash -s -- --clone
# What it does: system packages, uv + Python 3.12 venv, Python deps for
# the errand service and robot_backend brain, .env from the template, a systemd-free start script.
# What it does NOT do: touch the LLM server already running on :8091, join networks, or start anything
# that moves the dog (the body service stays on the machine that holds the dog's WebRTC link).
# Not yet executed on a GX10 (2026-09-20): written from the Mac; expect to fix small things on first run.
set -euo pipefail

REPO_URL="${ANNIE_REPO_URL:-https://github.com/hiepnguyenduc2005/annie.git}"
ROOT="${ANNIE_ROOT:-$HOME/robot-dog/annie}"
PY="${ANNIE_PYTHON:-3.12}"

log() { printf '\n== %s\n' "$*"; }

if [[ "${1:-}" == "--clone" ]]; then
  mkdir -p "$(dirname "$ROOT")"
  if [[ ! -d "$ROOT/.git" ]]; then log "cloning into $ROOT"; git clone "$REPO_URL" "$ROOT"; fi
  cd "$ROOT"
else
  cd "$(dirname "$0")/.."; ROOT="$(pwd)"
fi

# Do not initialize or commit the operator's surrounding directories. They may
# contain credentials and unrelated work; this script only manages its checkout.

log "system packages"
if command -v apt-get >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq git curl jq portaudio19-dev libgl1 libglib2.0-0 build-essential python3-dev >/dev/null
fi

log "uv + Python $PY venv"
command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
[[ -d .venv ]] || uv venv .venv --python "$PY"
uv pip install --python .venv/bin/python -r app_backend/requirements.txt >/dev/null
uv pip install --python .venv/bin/python -r robot/app_backend/requirements.txt >/dev/null 2>&1 || true
uv pip install --python .venv/bin/python -r robot/robot_backend/requirements.txt >/dev/null 2>&1 || true
uv pip install --python .venv/bin/python httpx fastapi uvicorn pydantic >/dev/null

log "environment file"
[[ -f .env ]] || cp .env.example .env
grep -q '^ANNIE_VISION_MODE=' .env || cat >> .env <<'EOF'
# GX10 brain: the local OpenAI-compatible server (Qwen2.5-Omni) on this machine's loopback
ANNIE_VISION_MODE=local
ANNIE_VISION_BASE_URL=http://127.0.0.1:8091/v1
ANNIE_VISION_MODEL=Qwen/Qwen2.5-Omni-3B
EOF
chmod 600 .env   # local secrets (ANNIE_INTERNAL_SECRET etc.); never world-readable

log "checks"
.venv/bin/python -c "import fastapi, httpx, uvicorn; print('python deps ok')"
if curl -s -m 5 http://127.0.0.1:8091/v1/models >/dev/null 2>&1; then echo "LLM server on :8091 reachable"; else echo "WARNING: nothing on :8091 (start the Qwen2.5-Omni server first)"; fi
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "nvidia-smi not available"

log "start script -> $ROOT/robot/gx10_start.sh"
cat > robot/gx10_start.sh <<'EOF'
#!/usr/bin/env bash
# Start Annie's brain side on the GX10. Fill these in first (Mac = the machine holding the dog link):
#   export MAC=100.110.197.63            # the Mac's Tailscale (or hotspot) address
#   export ANNIE_INTERNAL_SECRET=...     # same value as on the Mac's app_backend
#   export ANNIE_BODY_TOKEN=...          # only if the Mac's body/patrol was started with one
set -euo pipefail
cd "$(dirname "$0")/.."
: "${MAC:?set MAC to the Mac's address}"; : "${ANNIE_INTERNAL_SECRET:?set ANNIE_INTERNAL_SECRET}"
export ANNIE_BODY_URL="http://$MAC:8011" ANNIE_APP_URL="http://$MAC:8000"
set -a; source .env; set +a
echo "errand -> body $ANNIE_BODY_URL, app $ANNIE_APP_URL"
.venv/bin/python robot/go2_errand.py --host 0.0.0.0 --port 8010 &
echo "robot_backend brain on :8004 (vision at $ANNIE_VISION_BASE_URL); run from its own folder per robot/robot_backend/README.md"
( cd robot/robot_backend && ../../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8004 ) &
wait
EOF
chmod +x robot/gx10_start.sh
echo
echo "Done. Next: on the Mac set ROBOT_BACKEND_URL=http://<gx10-address>:8010 and add the GX10 address to ANNIE_ALLOWED_HOSTS;"
echo "start the patrol/body on the Mac with ANNIE_BODY_TOKEN if you want the token; then: MAC=... ANNIE_INTERNAL_SECRET=... robot/gx10_start.sh"
