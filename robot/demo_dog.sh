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

pids=()
cleanup() { echo; echo "stopping..."; pkill -INT -f "go2_patrol_greet.py --ip" 2>/dev/null || true; for p in "${pids[@]}"; do kill -INT "$p" 2>/dev/null || true; done; sleep 4; pkill -9 -f "go2_patrol_greet.py --ip" 2>/dev/null || true; for p in "${pids[@]}"; do kill -9 "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

.venv/bin/uvicorn app_backend.app.main:app --host 0.0.0.0 --port $APP_PORT --no-proxy-headers > "$LOGS/app.log" 2>&1 & pids+=($!)
.venv/bin/python robot/go2_errand.py --host 127.0.0.1 --port 8010 > "$LOGS/errand.log" 2>&1 & pids+=($!)
dog_loop() {  # relaunch the dog process whenever it exits (link drop, battery floor is final though)
  while true; do
    until ping -c 1 -W 1 "$DOG_IP" >/dev/null 2>&1; do sleep 3; done
    .cache/dimos/.venv/bin/python robot/go2_patrol_greet.py --ip "$DOG_IP" --duration "$DURATION" --brain --voice --speed 0.4 \
      --output "$LOGS/patrol-$(date +%Y%m%d-%H%M%S).json" >> "$LOGS/patrol.log" 2>&1 || true
    grep -q "battery_low" "$LOGS/patrol.log" 2>/dev/null && tail -1 "$LOGS/patrol.log" | grep -q battery_low && { echo "battery floor reached: charge the dog"; return; }
    echo "dog process exited $(date +%H:%M:%S); waiting for the link to relaunch"
    sleep 5
  done
}
dog_loop & pids+=($!)

sleep 6
echo "phone app     -> http://$MAC_HOTSPOT_IP:$APP_PORT  (POST /api/messages; needs the phone on the same hotspot)"
echo "live view     -> http://127.0.0.1:8011/   4D: http://127.0.0.1:8011/spacetime"
echo "try it:         curl -s -X POST http://127.0.0.1:$APP_PORT/api/messages -H 'Content-Type: application/json' -d '{\"author_id\":\"zach\",\"text\":\"go wave at Grandma\"}'"
echo "logs          -> $LOGS/{app,errand,patrol}.log     (Ctrl-C stops everything and sends StopMove)"
tail -n 0 -f "$LOGS/patrol.log" | grep --line-buffered -E "go2-patrol-greet: (connected|t=.*(person|mission|COLLISION|voice|brain:)|[a-z_:A-Z]+ greetings=)"
