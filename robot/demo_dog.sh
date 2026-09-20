#!/usr/bin/env bash
# One command for the physical demo on the Mac: family app backend + errand brain + the dog process.
#   robot/demo_dog.sh            # dog on the phone hotspot at 172.20.10.10, Mac tethered by USB
#   DOG_IP=... robot/demo_dog.sh
# Starts:  app_backend :8000 (reachable from the phone at the Mac's hotspot address)
#          go2_errand  :8010 (plain-language missions -> the dog process)
#          go2_patrol_greet :8011 (owns the dog: explore + greet when idle, missions when asked; live view + 4D)
# Stops everything on Ctrl-C (the dog gets StopMove). Keys/secrets come from the ignored .env only.
# Prerequisites: dog charged above 40 % and joined to the hotspot; .env with ANNIE_INTERNAL_SECRET (and the
# voice keys if wanted); AirPods (or any mic/speaker) as the Mac's default audio devices.
set -euo pipefail
cd "$(dirname "$0")/.."
DOG_IP="${DOG_IP:-172.20.10.10}"
DURATION="${DURATION:-3600}"
APP_PORT="${APP_PORT:-8000}"
LOGS=.data/hardware/demo-logs; mkdir -p "$LOGS"
DOG_WAIT_TIMEOUT_S="${DOG_WAIT_TIMEOUT_S:-900}"   # stop after 15 min waiting for the dog to ping
PING_CMD="${PING_CMD:-ping}"                        # overridable for tests (never changed in production)

if [[ ! -f .env ]]; then
  echo ".env not found: copy .env.example to .env and set ANNIE_INTERNAL_SECRET (never committed)"
  exit 2
fi
set -a; source .env; set +a
: "${ANNIE_INTERNAL_SECRET:?ANNIE_INTERNAL_SECRET must be set in .env}"
export UNITREE_AES_128_KEY="${UNITREE_AES_128_KEY:-$(python3 -c "import json;print(json.load(open('.cache/go2-private/connection.json'))['aes_key'])")}"
hotspot_ip() { ifconfig | awk '/inet 172\.20\.10\./{print $2; exit}'; }
MAC_HOTSPOT_IP="$(hotspot_ip)"
if [[ -z "$MAC_HOTSPOT_IP" ]]; then
  echo "no 172.20.10.x address on this Mac yet: plug the iPhone in (USB tether), open Personal Hotspot; waiting..."
  until MAC_HOTSPOT_IP="$(hotspot_ip)"; [[ -n "$MAC_HOTSPOT_IP" ]]; do sleep 3; done
  echo "tether up at $MAC_HOTSPOT_IP"
fi
ping -c 1 -W 1 "$DOG_IP" >/dev/null 2>&1 || echo "dog not answering at $DOG_IP yet: the dog process starts when it does (power on / hotspot join)"
for p in $APP_PORT 8010 8011; do lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1 && { echo "port $p is already in use; stop that process first"; exit 2; }; done

export ANNIE_ALLOWED_HOSTS="localhost,127.0.0.1,[::1],$MAC_HOTSPOT_IP"
export ROBOT_BACKEND_URL="http://127.0.0.1:8010" ANNIE_FAMILY_MOCK_ROBOT=false
export ANNIE_BODY_URL="http://127.0.0.1:8011" ANNIE_APP_URL="http://127.0.0.1:$APP_PORT"
# the phone's Dev tab reaches the dog process at the Mac's hotspot address: bind beyond loopback only with a token
export ANNIE_VIEW_HOSTS="${ANNIE_VIEW_HOSTS:-$MAC_HOTSPOT_IP}"
VIEW_HOST="127.0.0.1"; [[ -n "${ANNIE_BODY_TOKEN:-}" ]] && VIEW_HOST="0.0.0.0"

pids=()   # every child this script started; cleanup touches only these
cleanup_done=0
cleanup() {  # idempotent; TERM first (the dog process handles TERM), bounded wait, KILL only what is still alive
  [[ "$cleanup_done" == 1 ]] && return
  cleanup_done=1
  trap - EXIT INT TERM
  echo; echo "stopping..."
  for p in "${pids[@]}"; do kill -TERM "$p" 2>/dev/null || true; done
  local i=0 alive=0
  while (( i < 40 )); do
    alive=0
    for p in "${pids[@]}"; do if kill -0 "$p" 2>/dev/null; then alive=1; fi; done
    (( alive == 0 )) && break
    sleep 0.25; i=$((i + 1))
  done
  for p in "${pids[@]}"; do
    if kill -0 "$p" 2>/dev/null; then kill -KILL "$p" 2>/dev/null || true; fi
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

.venv/bin/uvicorn app_backend.app.main:app --host 0.0.0.0 --port $APP_PORT --no-proxy-headers > "$LOGS/app.log" 2>&1 & pids+=($!)
.venv/bin/python robot/go2_errand.py --host 127.0.0.1 --port 8010 > "$LOGS/errand.log" 2>&1 & pids+=($!)
# Space-time memory into Elasticsearch (where was X last seen) whenever the local node answers; harmless otherwise
if curl -s -m 2 "${ANNIE_ES_URL:-http://127.0.0.1:9200}" >/dev/null 2>&1; then
  .venv/bin/python robot/go2_spacetime_elastic.py --tail --file .data/hardware/spacetime.jsonl > "$LOGS/elastic.log" 2>&1 & pids+=($!)
  echo "elastic       -> indexing sightings into ${ANNIE_ES_URL:-http://127.0.0.1:9200} (annie-spacetime)"
fi
dog_loop() {  # owns exactly one child (the dog process); relay TERM to it; no restart after a stop
  local child="" waited_s=0
  relay_and_exit() {  # child-scope: TERM my own child only, bounded, then leave (EXIT must not restart anything)
    trap - EXIT TERM INT
    if [[ -n "$child" ]]; then
      kill -TERM "$child" 2>/dev/null || true
      local i=0
      while kill -0 "$child" 2>/dev/null && (( i < 20 )); do sleep 0.25; i=$((i + 1)); done
      if kill -0 "$child" 2>/dev/null; then kill -KILL "$child" 2>/dev/null || true; fi
      wait "$child" 2>/dev/null || true
    fi
  }
  on_term() { relay_and_exit; exit 0; }
  trap on_term TERM INT
  trap relay_and_exit EXIT
  while true; do
    until "$PING_CMD" -c 1 -W 1 "$DOG_IP" >/dev/null 2>&1; do
      sleep 3; waited_s=$((waited_s + 3))
      if (( waited_s >= DOG_WAIT_TIMEOUT_S )); then
        echo "dog not reachable at $DOG_IP after ${DOG_WAIT_TIMEOUT_S}s: giving up (start demo_dog.sh again when the dog is on)"
        return
      fi
    done
    waited_s=0
    local report="$LOGS/patrol-$(date +%Y%m%d-%H%M%S).json"
    .cache/dimos/.venv/bin/python robot/go2_patrol_greet.py --ip "$DOG_IP" --duration "$DURATION" --brain --voice --speed 0.4 --view-host "$VIEW_HOST" \
      --output "$report" >> "$LOGS/patrol.log" 2>&1 &
    child=$!
    set +e; wait "$child"; set -e
    child=""
    # battery floor is a deliberate stop: the run report says so; exit 0 means the run completed normally
    if [[ -f "$report" ]] && python3 -c "import json,sys;sys.exit(0 if json.load(open(sys.argv[1])).get('reason')=='battery_low' else 1)" "$report" 2>/dev/null; then
      echo "battery floor reached: charge the dog"
      return
    fi
    echo "dog process exited $(date +%H:%M:%S); waiting for the link to relaunch"
    sleep 5
  done
}
dog_loop & dog_loop_pid=$!; pids+=("$dog_loop_pid")

sleep 6
echo "phone app     -> http://$MAC_HOTSPOT_IP:$APP_PORT  (POST /api/messages; needs the phone on the same hotspot)"
echo "live view     -> http://127.0.0.1:8011/   4D: http://127.0.0.1:8011/spacetime"
echo "try it:         curl -s -X POST http://127.0.0.1:$APP_PORT/api/messages -H 'Content-Type: application/json' -d '{\"author_id\":\"zach\",\"text\":\"go wave at Grandma\"}'"
echo "logs          -> $LOGS/{app,errand,patrol}.log     (Ctrl-C stops everything and sends StopMove)"
echo "follow logs: tail -f $LOGS/patrol.log"
# Keep signal handling responsive without an unowned foreground tail pipeline.
while kill -0 "$dog_loop_pid" 2>/dev/null; do
  for p in "${pids[@]}"; do
    if ! kill -0 "$p" 2>/dev/null; then echo "service $p exited; stopping this stack"; exit 1; fi
  done
  sleep 1
done
wait "$dog_loop_pid"
