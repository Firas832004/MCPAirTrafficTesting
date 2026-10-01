#!/usr/bin/env bash
# End-to-end pipeline test. Starts the REST API and MCP server with throwaway tokens, then:
#   1) discovers the tools, 2) calls each one and shows every layer it passes through, including
#   the matching rows read straight from the database, 3) runs the MCP client test suite,
#   4) runs security/log checks. Every output line is labelled with the component it comes from.
# Usage: ./run_tests.sh
cd "$(dirname "$0")" || exit 1
export REST_API_TOKEN=$(openssl rand -hex 16) MCP_API_TOKEN=$(openssl rand -hex 16)  # never written to disk

# --- Output helpers ---
if [ -t 1 ]; then G=$'\033[32m'; R=$'\033[31m'; B=$'\033[1m'; D=$'\033[2m'; N=$'\033[0m'; else G=; R=; B=; D=; N=; fi
CLIENT="[MCP CLIENT] "; MCP="[MCP SERVER] "; REST="[REST API]   "; DB="[DATABASE]   "; SCRIPT="[SCRIPT]     "
TALLY=""
record() { TALLY+="$1|$2"$'\n'; }
label() { sed "s/^/$1 /"; }
stage() { echo; echo "${B}━━ $1 ━━${N}"; }
pass() { echo "$1 ${G}PASS${N}  $2"; record "$1" PASS; }
fail() { echo "$1 ${R}FAIL${N}  $2"; record "$1" FAIL; }
check() { if (eval "$3") >/dev/null 2>&1; then pass "$1" "$2"; else fail "$1" "$2"; fi; }
DBFILE=rest-api/tracks.db
dbq() { sqlite3 -readonly -header -column "$DBFILE" "$1"; }   # independent read-only look at the data
COLS="track_id, callsign, aircraft_type, classification, lat, lon, altitude_ft, speed_kts, heading_deg"

# --- Banner ---
echo "${B}MCP air traffic pipeline test${N}  (fake data, everything on this Mac)"
echo
echo "  MCP CLIENT ──► MCP SERVER ──► REST API ──► DATABASE"
echo "  (scripts)      :8000          :8001        tracks.db (SQLite, read-only)"
echo "  bearer token   bearer token   validates + parameterized queries"

# --- Stage 1: start services ---
for p in 8000 8001; do
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
pass "$SCRIPT" "REST API up on :8001 and MCP server up on :8000 (throwaway tokens, fresh database)"
check "$DB" "tracks.db created with $(dbq 'SELECT COUNT(*) FROM tracks' | tail -1 | tr -d ' ') fake tracks" "[ -s $DBFILE ]"

# --- Stage 2: tool discovery + demo calls (the demo runs first so each call lines up with the logs) ---
demo=$(cd mcp-client && .venv/bin/python call_tools.py 2>&1); demo_rc=$?
stage "2  MCP client discovers the server's tools"
grep -E '^(discovered|tool) ' <<<"$demo" | label "$CLIENT"
[ $demo_rc -eq 0 ] && pass "$CLIENT" "demo client connected and completed all calls" || fail "$CLIENT" "demo client failed: $demo"

# --- Stage 3: follow each call through every layer ---
# SQL that reproduces what each demo call should return, and how to compare it with the client's result.
SQL=(
  "SELECT COUNT(*) AS record_count FROM tracks"
  "SELECT $COLS FROM tracks WHERE track_id = 3"
  "SELECT $COLS FROM tracks ORDER BY last_update DESC, track_id LIMIT 3"
  "SELECT $COLS FROM tracks WHERE lat BETWEEN 25 AND 28 AND lon BETWEEN 48 AND 56 ORDER BY track_id"
  "SELECT COUNT(*) AS n FROM tracks"
  "SELECT COUNT(*) AS n FROM tracks WHERE classification = 'civil'"
)
KIND=(count ids ids ids count count)   # count = compare a number, ids = compare track IDs in order
stage "3  Each tool call, followed through every layer"
calls=$(grep '^called ' <<<"$demo"); recvs=$(grep '^received ' <<<"$demo")
oklines=$(grep ' OK$' mcp-server/logs/mcp-server.log)
total=$(wc -l <<<"$calls" | tr -d ' ')
for i in $(seq 1 "$total"); do
  call=$(sed -n "${i}p" <<<"$calls"); recv=$(sed -n "${i}p" <<<"$recvs"); mline=$(sed -n "${i}p" <<<"$oklines")
  rid=$(sed -E 's/.*rid=([a-f0-9]+) .*/\1/' <<<"$mline")
  echo; echo "${D}── call $i of $total ──${N}"
  echo "$CLIENT called: ${call#called }"
  echo "$MCP $(cut -d' ' -f4- <<<"$mline")"
  echo "$REST $(grep "rid=$rid " rest-api/logs/rest-api.log | cut -d' ' -f4-)"
  echo "$DB query: ${SQL[$((i - 1))]}"
  rows=$(dbq "${SQL[$((i - 1))]}"); echo "$rows" | label "$DB"
  echo "$CLIENT received: ${recv#received }"
  if [ "${KIND[$((i - 1))]}" = ids ]; then
    want=$(awk 'NR>2{print $1}' <<<"$rows" | tr '\n' ' '); got=$(grep -o '#[0-9]*' <<<"$recv" | tr -d '#' | tr '\n' ' ')
  else
    want=$(tail -1 <<<"$rows" | tr -d ' '); got=$(grep -oE '[0-9]+(,|\})?$' <<<"$recv" | tr -d ',}')
  fi
  [ -n "$want" ] && [ "$want" = "$got" ] && pass "$SCRIPT" "result received by the MCP client matches the database (${want% })" \
    || fail "$SCRIPT" "client got [$got] but the database says [$want]"
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
check "$CLIENT" "code has no DB path or credentials"          "! grep -En 'tracks\\.db|sqlite|REST_API|[0-9a-f]{32}' mcp-client/connect.py mcp-client/call_tools.py"
check "$MCP"    "code has no database access"                 "! grep -En 'sqlite' mcp-server/server.py"
check "$SCRIPT" "tokens never appear in any log"              "! grep -rF -e '$REST_API_TOKEN' -e '$MCP_API_TOKEN' rest-api/logs mcp-server/logs"
check "$SCRIPT" "every MCP tool call appears in the REST log under the same request ID" \
  "for r in \$(grep ' OK\$' mcp-server/logs/mcp-server.log | grep -oE 'rid=[a-f0-9]+'); do grep -q \"\$r .*-> 200\" rest-api/logs/rest-api.log || exit 1; done"
check "$SCRIPT" "rejections are logged by both servers"       "grep -q REJECTED rest-api/logs/rest-api.log && grep -q REJECTED mcp-server/logs/mcp-server.log"

# --- Scorecard ---
stage "Scorecard"
failed=0
for lbl in "$CLIENT" "$MCP" "$REST" "$DB" "$SCRIPT"; do
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
exit $((failed > 0))
