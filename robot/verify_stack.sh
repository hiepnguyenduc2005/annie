#!/usr/bin/env bash
# End-to-end verification of the Annie stack: unit suites in both venvs, the family backend suite, contract and
# frontend checks, the iPhone app build, then LIVE probes of whatever is running (backend :8020, dog process :8011
# hardware/simulation/replay, errand :8010) and one family mission driven to a terminal state.
#   robot/verify_stack.sh            # everything
#   robot/verify_stack.sh --live     # live probes only (fast)
# Exit code is non-zero when any check fails; the summary at the end says exactly which.
set -u
cd "$(dirname "$0")/.."
TOKEN="${ANNIE_API_TOKEN:-$(grep -E '^ANNIE_API_TOKEN=' .env 2>/dev/null | cut -d= -f2-)}"
APP="${ANNIE_APP_URL:-http://127.0.0.1:8020}"
DOG="${ANNIE_BODY_URL:-http://127.0.0.1:8011}"
ERRAND="${ROBOT_BACKEND_URL:-http://127.0.0.1:8010}"
S=/tmp/annie-verify-$$; mkdir -p "$S"
pass=(); fail=(); skip=()
ok()   { pass+=("$1"); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { fail+=("$1"); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
skp()  { skip+=("$1"); printf '  \033[33mSKIP\033[0m %s\n' "$1"; }
auth() { curl -s -m "${2:-5}" -H "Authorization: Bearer $TOKEN" "$1"; }

if [[ "${1:-}" != "--live" ]]; then
  echo "== unit suites"
  if timeout 900 .venv/bin/python -m pytest robot/tests -q -p no:cacheprovider > "$S/robot-venv.log" 2>&1; then ok "robot/tests in .venv ($(tail -1 "$S/robot-venv.log"))"; else bad "robot/tests in .venv ($(tail -1 "$S/robot-venv.log"))"; fi
  if [[ -x .cache/dimos/.venv/bin/python ]]; then
    if timeout 900 .cache/dimos/.venv/bin/python -m pytest robot/tests -q -p no:cacheprovider > "$S/robot-dimos.log" 2>&1; then ok "robot/tests in dimOS venv ($(grep -v objc "$S/robot-dimos.log" | tail -1))"; else bad "robot/tests in dimOS venv ($(grep -v objc "$S/robot-dimos.log" | tail -1))"; fi
  else skp "dimOS venv missing"; fi
  if (cd app_backend && timeout 300 ../.venv/bin/python -m pytest tests -q -p no:cacheprovider > "$S/app.log" 2>&1); then ok "app_backend/tests ($(tail -1 "$S/app.log"))"; else bad "app_backend/tests ($(tail -1 "$S/app.log"))"; fi
  if timeout 300 .venv/bin/python -m pytest robot/app_backend/tests -q -p no:cacheprovider > "$S/sim-app.log" 2>&1; then ok "robot/app_backend/tests ($(tail -1 "$S/sim-app.log"))"; else bad "robot/app_backend/tests ($(tail -1 "$S/sim-app.log"))"; fi
  if .venv/bin/python robot/contract/export_schemas.py --check > "$S/contract.log" 2>&1; then ok "contract schemas in sync"; else bad "contract schemas ($(tail -1 "$S/contract.log"))"; fi
  if node --check robot/frontend/app.js 2>/dev/null; then ok "robot/frontend/app.js parses"; else bad "robot/frontend/app.js"; fi
  if node -e "const fs=require('fs');const s=fs.readFileSync('robot/dog/view/command_center.html','utf8');new Function(s.match(/<script>([\s\S]*)<\/script>/)[1])" 2>/dev/null; then ok "command_center.html script parses"; else bad "command_center.html script"; fi
  if [[ -d /Applications/Xcode.app ]]; then
    if (cd app_frontend && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer timeout 900 xcodebuild -project Annie.xcodeproj -target Annie -configuration Debug -sdk iphonesimulator -arch arm64 CODE_SIGNING_ALLOWED=NO SYMROOT="$S/build" build > "$S/xcode.log" 2>&1); then ok "iPhone app builds (simulator)"; else bad "iPhone app build ($(grep -m1 'error:' "$S/xcode.log"))"; fi
  else skp "Xcode not installed: iPhone build"; fi
fi

echo "== live probes"
if [[ -z "$TOKEN" ]]; then skp "no ANNIE_API_TOKEN: backend probes"; else
  st=$(auth "$APP/api/reminders" | head -c 1); [[ "$st" == "[" ]] && ok "backend :8020 reminders" || bad "backend :8020 reminders"
  dog=$(auth "$APP/api/dog/status"); echo "$dog" | grep -q '"available": *true' && ok "backend sees the dog process ($(echo "$dog" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("source"),d.get("mode"),"battery",d.get("battery"))' 2>/dev/null))" || bad "backend cannot see the dog process"
  ans=$(curl -s -m 5 -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$APP/api/ask" -d '{"question":"Where is Grandma?"}'); echo "$ans" | grep -qi "jeanine" && ok "Ask Annie answers about Grandma ($(echo "$ans" | head -c 90)...)" || bad "Ask Annie: $ans"
  auth "$APP/api/settings/voice" | grep -q '"available": *true' && ok "voice settings reachable" || skp "voice settings: dog process not answering"
  auth "$APP/api/people" | grep -q '"available": *true' && ok "people directory reachable" || skp "people: dog process not answering"
fi
curl -s -m 3 "$DOG/health" | grep -q '"ok": *true' && ok "dog process :8011 health" || bad "dog process :8011 health"
for r in telemetry.json spacetime_latest.json map.jpg frame.jpg "graph.json?voxels=10"; do
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$DOG/$r"); [[ "$code" == "200" ]] && ok "dog /$r" || bad "dog /$r ($code)"
done
curl -s -m 3 "$ERRAND/health" 2>/dev/null | grep -q "ok" && ok "errand :8010 health" || skp "errand :8010 (no /health or down)"

if [[ -n "$TOKEN" ]]; then
  echo "== one family mission end to end (needs a dog process that executes: hardware or --sim)"
  src=$(auth "$APP/api/dog/status" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("source",""))' 2>/dev/null)
  if [[ "$src" == "hardware" || "$src" == "simulation" ]] && curl -s -m 3 "$DOG/telemetry.json" | grep -q '"connected": *true'; then
    rid=$(curl -s -m 5 -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$APP/api/messages" -d '{"author_id":"zach","text":"go wave at Grandma"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["run_id"])')
    status=dispatched; for i in $(seq 1 60); do sleep 2; status=$(auth "$APP/api/runs/$rid" | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])' 2>/dev/null); [[ "$status" == "completed" || "$status" == "failed" ]] && break; done
    detail=$(auth "$APP/api/runs/$rid" | python3 -c 'import json,sys;r=json.load(sys.stdin);print(r.get("outcome"),[e["kind"] for e in r["events"]])' 2>/dev/null)
    [[ "$status" == "completed" ]] && ok "mission 'go wave at Grandma' completed: $detail" || bad "mission ended $status: $detail"
  else skp "mission e2e: dog source is '${src:-none}' (replay cannot execute)"; fi
fi

echo; echo "== summary: ${#pass[@]} pass, ${#fail[@]} fail, ${#skip[@]} skip  (logs in $S)"
for f in "${fail[@]}"; do echo "  FAIL $f"; done
[[ ${#fail[@]} -eq 0 ]]
