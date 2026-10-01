#!/usr/bin/env bash
# End-to-end pipeline test. Starts the REST API and MCP server with throwaway tokens, then:
#   1) discovers the tools, 2) calls each one and shows every layer it passes through (database rows or
#   the live source's answer), 3) runs the MCP client test suite, 4) runs security/log checks.
# Every output line is labelled with the component it comes from.
# Usage: ./run_tests.sh [demo1|demo2] [chat]      or      ./run_tests.sh stop
#   demo2 (default) = fake data from the local database      demo1 = live flight data from OpenSky
#   chat            = after the checks pass, open the chat UI at http://127.0.0.1:8002 (Ctrl+C stops everything)
#   stop            = stop this project's leftover servers and chat UI (ports 8000, 8001, 8002) and exit
cd "$(dirname "$0")" || exit 1
# --- stop: free ports 8000/8001/8002, but only if the listener is one of this project's own servers ---
if [ "$1" = stop ]; then
  for p in 8000 8001 8002; do
    for pid in $(lsof -t -iTCP:$p -sTCP:LISTEN); do
      if ps -o command= -p "$pid" | grep -qE 'app\.py|server\.py|chat_server\.py'; then
        kill "$pid" && echo "Stopped process $pid on port $p"
      else
        echo "Port $p is used by another program (pid $pid); left alone."
      fi
    done
  done
  sleep 1
  [ -z "$(lsof -t -iTCP:8000 -iTCP:8001 -iTCP:8002 -sTCP:LISTEN)" ] && echo "Ports 8000, 8001 and 8002 are free." || echo "Some ports are still in use."
  exit 0
fi
MODE=db; CHAT=0
for a in "$@"; do case $a in demo1) MODE=api;; demo2) MODE=db;; chat) CHAT=1;; *) echo "Unknown option: $a (use demo1, demo2, chat, or stop on its own)"; exit 2;; esac; done
export DATA_MODE=$MODE DATA_SOURCE=$([ $MODE = api ] && echo opensky || echo db)
export REST_API_TOKEN=$(openssl rand -hex 16) MCP_API_TOKEN=$(openssl rand -hex 16)  # never written to disk

# --- Output helpers ---
if [ -t 1 ]; then G=$'\033[32m'; R=$'\033[31m'; B=$'\033[1m'; D=$'\033[2m'; N=$'\033[0m'; else G=; R=; B=; D=; N=; fi
CLIENT="[MCP CLIENT] "; MCP="[MCP SERVER] "; REST="[REST API]   "; DB="[DATABASE]   "; LIVE="[OPENSKY]    "; SCRIPT="[SCRIPT]     "
TALLY=""
record() { TALLY+="$1|$2"$'\n'; }
label() { sed "s/^/$1 /"; }
stage() { echo; echo "${B}━━ $1 ━━${N}"; }
pass() { echo "$1 ${G}PASS${N}  $2"; record "$1" PASS; }
fail() { echo "$1 ${R}FAIL${N}  $2"; record "$1" FAIL; }
check() { if (eval "$3") >/dev/null 2>&1; then pass "$1" "$2"; else fail "$1" "$2"; fi; }
DBFILE=rest-api/tracks.db
dbq() { sqlite3 -readonly -header -column "$DBFILE" "$1"; }   # independent read-only look at the data
rest() { curl -s -H "Authorization: Bearer $REST_API_TOKEN" "localhost:8001$1"; }
rest_val() { rest "$1" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(' '.join(str(a['track_id']) for a in d['aircraft']) if 'aircraft' in d else d.get('count', d.get('record_count', d.get('track_id'))))"; }
COLS="track_id, callsign, aircraft_type, classification, lat, lon, altitude_ft, speed_kts, heading_deg"
if [ $MODE = api ]; then SRCLBL="$LIVE"; SRCDESC="LIVE data from the OpenSky Network"; else SRCLBL="$DB"; SRCDESC="FAKE data from the local database"; fi

# --- Banner ---
echo "${B}MCP air traffic pipeline test${N}  ($SRCDESC; everything else runs on this Mac)"
echo
echo "  MCP CLIENT ──► MCP SERVER ──► REST API ──► $([ $MODE = api ] && echo "OPENSKY API" || echo "DATABASE")"
echo "  (scripts)      :8000          :8001        $([ $MODE = api ] && echo "opensky-network.org (anonymous, cached 10 s)" || echo "tracks.db (SQLite, read-only)")"
echo "  bearer token   bearer token   validates + $([ $MODE = api ] && echo "rate-limit aware" || echo "parameterized queries")"

# --- Stage 1: start services ---
for p in 8000 8001 $([ $CHAT = 1 ] && echo 8002); do
  lsof -ti tcp:$p -sTCP:LISTEN >/dev/null && { echo "Port $p is already in use; stop that process first."; exit 1; }
done
rm -f rest-api/tracks.db rest-api/logs/*.log mcp-server/logs/*.log   # fresh data and logs
mkdir -p rest-api/logs mcp-server/logs
# Server console output goes to logs/stderr.log (shown at the end only if a check fails).
(cd rest-api && exec .venv/bin/python app.py >/dev/null 2>logs/stderr.log) & pids=($!)
(cd mcp-server && exec .venv/bin/python server.py >/dev/null 2>logs/stderr.log) & pids+=($!)
disown -a
cleanup() {
  kill "${pids[@]}" 2>/dev/null
  for _ in $(seq 50); do [ -z "$(lsof -t -iTCP:8000 -iTCP:8001 -sTCP:LISTEN)" ] && break; sleep 0.1; done
}
trap cleanup EXIT
for _ in $(seq 50); do
  curl -s -o /dev/null http://127.0.0.1:8001/ && curl -s -o /dev/null http://127.0.0.1:8000/ && break
  sleep 0.2
done
stage "1  Start services"
pass "$SCRIPT" "REST API up on :8001 and MCP server up on :8000 (throwaway tokens)"
if [ $MODE = api ]; then
  n=$(rest_val /v1/health)
  [ "${n:-0}" -gt 0 ] 2>/dev/null && pass "$LIVE" "live source reachable: $n aircraft currently in the Saudi Arabia area" \
    || fail "$LIVE" "live source returned no aircraft (rate limited or offline?)"
else
  check "$DB" "tracks.db created with $(dbq 'SELECT COUNT(*) FROM tracks' | tail -1 | tr -d ' ') fake tracks" "[ -s $DBFILE ]"
fi

# --- Stage 2: tool discovery + demo calls (the demo runs first so each call lines up with the logs) ---
demo=$(cd mcp-client && .venv/bin/python call_tools.py 2>&1); demo_rc=$?
stage "2  MCP client discovers the server's tools"
grep -E '^(discovered|tool) ' <<<"$demo" | label "$CLIENT"
[ $demo_rc -eq 0 ] && pass "$CLIENT" "demo client connected and completed all calls" || fail "$CLIENT" "demo client failed: $demo"

# --- Stage 3: follow each call through every layer ---
# Demo call order: status, list(3), get(first listed id), area(Saudi box), count, count by class.
KIND=(count ids ids ids count count)   # count = compare a number, ids = compare track IDs in order
PATHS=("/v1/health" "/v1/aircraft?limit=3" "" "/v1/aircraft/area?lat_min=16&lat_max=33&lon_min=34&lon_max=56&limit=20"
       "/v1/aircraft/count" "/v1/aircraft/count?classification=$([ $MODE = api ] && echo airborne || echo civil)")
stage "3  Each tool call, followed through every layer"
calls=$(grep '^called ' <<<"$demo"); recvs=$(grep '^received ' <<<"$demo")
oklines=$(grep ' OK$' mcp-server/logs/mcp-server.log)
total=$(wc -l <<<"$calls" | tr -d ' ')
for i in $(seq 1 "$total"); do
  call=$(sed -n "${i}p" <<<"$calls"); recv=$(sed -n "${i}p" <<<"$recvs"); mline=$(sed -n "${i}p" <<<"$oklines")
  rid=$(sed -E 's/.*rid=([a-f0-9]+) .*/\1/' <<<"$mline"); tid=$(grep -oE '"track_id": [0-9]+' <<<"$call" | grep -oE '[0-9]+')
  path="${PATHS[$((i - 1))]}"; [ $i -eq 3 ] && path="/v1/aircraft/$tid"
  echo; echo "${D}── call $i of $total ──${N}"
  echo "$CLIENT called: ${call#called }"
  echo "$MCP $(cut -d' ' -f4- <<<"$mline")"
  echo "$REST $(grep "rid=$rid " rest-api/logs/rest-api.log | cut -d' ' -f4-)"
  if [ $MODE = db ]; then
    case $i in
      1) sql="SELECT COUNT(*) AS record_count FROM tracks";;
      2) sql="SELECT $COLS FROM tracks ORDER BY last_update DESC, track_id LIMIT 3";;
      3) sql="SELECT $COLS FROM tracks WHERE track_id = $tid";;
      4) sql="SELECT $COLS FROM tracks WHERE lat BETWEEN 16 AND 33 AND lon BETWEEN 34 AND 56 ORDER BY track_id";;
      5) sql="SELECT COUNT(*) AS n FROM tracks";;
      6) sql="SELECT COUNT(*) AS n FROM tracks WHERE classification = 'civil'";;
    esac
    echo "$DB query: $sql"
    rows=$(dbq "$sql"); echo "$rows" | label "$DB"
    if [ "${KIND[$((i - 1))]}" = ids ]; then want=$(awk 'NR>2{print $1}' <<<"$rows" | tr '\n' ' '); else want=$(tail -1 <<<"$rows" | tr -d ' '); fi
  else
    echo "$LIVE live source: GET opensky-network.org/api/states/all (via the REST API, cached 10 s)"
    want=$(rest_val "$path"); echo "$LIVE the REST API's own answer for the same request: $want"
  fi
  echo "$CLIENT received: ${recv#received }"
  if [ "${KIND[$((i - 1))]}" = ids ]; then got=$(grep -o '#[0-9]*' <<<"$recv" | tr -d '#' | tr '\n' ' '); else got=$(grep -oE '[0-9]+(,|\})?$' <<<"$recv" | tr -d ',}'); fi
  want=$(echo $want); got=$(echo $got)   # normalise spacing
  [ -n "$want" ] && [ "$want" = "$got" ] && pass "$SCRIPT" "result received by the MCP client matches $([ $MODE = db ] && echo "the database" || echo "the live source via the REST API") (${want:0:60})" \
    || fail "$SCRIPT" "client got [$got] but expected [$want]"
done

# --- Stage 4: MCP client test suite (valid data + invalid input) ---
stage "4  MCP client test suite (valid calls and invalid input)"
out=$(cd mcp-client && .venv/bin/python test_tools.py 2>&1)
while read -r word _; do case $word in PASS|FAIL) record "$CLIENT" "$word";; esac; done <<<"$out"
sed "s/^PASS/${G}PASS${N}/; s/^FAIL/${R}FAIL${N}/" <<<"$out" | label "$CLIENT"

# --- Stage 5: security and logging checks ---
stage "5  Security and logging checks"
H="Authorization: Bearer"
check "$REST"   "rejects a missing token (401)"               "[ \$(curl -s -o /dev/null -w %{http_code} localhost:8001/v1/health) = 401 ]"
check "$REST"   "rejects a wrong token (401)"                 "[ \$(curl -s -o /dev/null -w %{http_code} -H '$H nope' localhost:8001/v1/health) = 401 ]"
check "$REST"   "accepts the right token (200)"               "[ \$(curl -s -o /dev/null -w %{http_code} -H '$H $REST_API_TOKEN' localhost:8001/v1/health) = 200 ]"
check "$MCP"    "endpoint rejects a missing token (401)"      "[ \$(curl -s -o /dev/null -w %{http_code} -X POST localhost:8000/mcp) = 401 ]"
check "$MCP"    "endpoint rejects the REST API's token (401)" "[ \$(curl -s -o /dev/null -w %{http_code} -X POST -H '$H $REST_API_TOKEN' localhost:8000/mcp) = 401 ]"
check "$CLIENT" "code has no DB path or credentials"          "! grep -En 'tracks\\.db|sqlite|REST_API|[0-9a-f]{32}' mcp-client/connect.py mcp-client/call_tools.py mcp-client/chat_server.py"
check "$MCP"    "code has no database access"                 "! grep -En 'sqlite|opensky' mcp-server/server.py"
check "$SCRIPT" "tokens never appear in any log"              "! grep -rF -e '$REST_API_TOKEN' -e '$MCP_API_TOKEN' rest-api/logs mcp-server/logs"
check "$SCRIPT" "every MCP tool call appears in the REST log under the same request ID" \
  "for r in \$(grep ' OK\$' mcp-server/logs/mcp-server.log | grep -oE 'rid=[a-f0-9]+'); do grep -q \"\$r .*-> 200\" rest-api/logs/rest-api.log || exit 1; done"
check "$SCRIPT" "rejections are logged by both servers"       "grep -q REJECTED rest-api/logs/rest-api.log && grep -q REJECTED mcp-server/logs/mcp-server.log"

# --- Scorecard ---
stage "Scorecard"
failed=0
for lbl in "$CLIENT" "$MCP" "$REST" "$SRCLBL" "$SCRIPT"; do
  p=$(awk -F'|' -v l="$lbl" '$1==l && $2=="PASS"' <<<"$TALLY" | wc -l | tr -d ' ')
  f=$(awk -F'|' -v l="$lbl" '$1==l && $2=="FAIL"' <<<"$TALLY" | wc -l | tr -d ' ')
  failed=$((failed + f))
  [ "$f" -eq 0 ] && echo "$lbl ${G}$p passed${N}" || echo "$lbl ${G}$p passed${N}, ${R}$f failed${N}"
done
echo
if [ $failed -eq 0 ]; then echo "${G}${B}ALL CHECKS PASSED${N}"; else
  echo "${R}${B}$failed CHECK(S) FAILED${N}"
  for f in rest-api/logs/stderr.log mcp-server/logs/stderr.log; do [ -s $f ] && { echo "== $f"; tail -20 $f; }; done
fi

# --- Optional chat UI (stays up until Ctrl+C; the EXIT trap then stops the servers) ---
if [ $CHAT = 1 ] && [ $failed -eq 0 ]; then
  stage "Chat UI"
  echo "Open http://127.0.0.1:8002   (model: ${CLAUDE_MODEL:-claude-haiku-4-5}; Ctrl+C stops everything)"
  [ -z "$ANTHROPIC_API_KEY" ] && echo "Note: ANTHROPIC_API_KEY is not set in this terminal, so Claude's answers will fail until you export it and rerun."
  (cd mcp-client && .venv/bin/python chat_server.py)
fi
exit $((failed > 0))
