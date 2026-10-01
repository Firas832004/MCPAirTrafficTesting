#!/usr/bin/env bash
# Runs the whole pipeline test: starts the REST API and MCP server with throwaway tokens,
# runs the client checks plus auth/log/secret checks, then stops everything.
# Every output line is labelled with the component it comes from.
# Usage: ./run_tests.sh          (add "demo" to also print the demo client's output)
cd "$(dirname "$0")" || exit 1
export REST_API_TOKEN=$(openssl rand -hex 16) MCP_API_TOKEN=$(openssl rand -hex 16)  # never written to disk
fails=0
label() { sed "s/^/$1 /"; }   # prefix every line of stdin
CLIENT="[MCP CLIENT] "; MCP="[MCP SERVER] "; REST="[REST API]   "; DB="[DATABASE]   "; SCRIPT="[SCRIPT]     "
check() { if (eval "$3") >/dev/null 2>&1; then echo "$1 PASS  $2"; else echo "$1 FAIL  $2"; fails=$((fails + 1)); fi; }

for p in 8000 8001; do
  lsof -ti tcp:$p -sTCP:LISTEN >/dev/null && { echo "Port $p is already in use; stop that process first."; exit 1; }
done
rm -f rest-api/tracks.db rest-api/logs/*.log mcp-server/logs/*.log   # fresh data and logs

mkdir -p rest-api/logs mcp-server/logs
# Server console output goes to logs/stderr.log (shown below only if a check fails).
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
echo "$SCRIPT started REST API on :8001 and MCP server on :8000 (throwaway tokens, fresh database)"

echo; echo "== MCP client tests: tool calls and the data they returned"
out=$(cd mcp-client && .venv/bin/python test_tools.py 2>&1); [ $? -eq 0 ] || fails=$((fails + 1))
echo "$out" | label "$CLIENT"
if [ "$1" = demo ]; then
  echo; echo "== MCP client demo"; (cd mcp-client && .venv/bin/python call_tools.py 2>&1) | label "$CLIENT"
fi

echo; echo "== Request trace: one request per tool, followed through each layer by its request ID"
grep ' OK$' mcp-server/logs/mcp-server.log | awk '!seen[$5]++' | while read -r line; do
  rid=$(sed -E 's/.*rid=([a-f0-9]+) .*/\1/' <<<"$line")
  echo "$CLIENT called $(sed -E 's/.*tool=([^ ]+) args=(.*) OK$/\1 \2/' <<<"$line")"
  echo "$MCP $(cut -d' ' -f4- <<<"$line")"
  echo "$REST $(grep "rid=$rid " rest-api/logs/rest-api.log | cut -d' ' -f4-)"
  echo "$DB read by the REST API (read-only), result returned up the chain"
  echo
done

echo "== Pipeline checks"
H="Authorization: Bearer"
check "$CLIENT" "demo MCP client runs against the server"        "cd mcp-client && .venv/bin/python call_tools.py"
check "$REST"   "rejects a missing token (401)"               "[ \$(curl -s -o /dev/null -w %{http_code} localhost:8001/v1/health) = 401 ]"
check "$REST"   "rejects a wrong token (401)"                 "[ \$(curl -s -o /dev/null -w %{http_code} -H '$H nope' localhost:8001/v1/health) = 401 ]"
check "$REST"   "accepts the right token (200)"               "[ \$(curl -s -o /dev/null -w %{http_code} -H '$H $REST_API_TOKEN' localhost:8001/v1/health) = 200 ]"
check "$MCP"    "endpoint rejects a missing token (401)"      "[ \$(curl -s -o /dev/null -w %{http_code} -X POST localhost:8000/mcp) = 401 ]"
check "$MCP"    "endpoint rejects the REST API's token (401)" "[ \$(curl -s -o /dev/null -w %{http_code} -X POST -H '$H $REST_API_TOKEN' localhost:8000/mcp) = 401 ]"
check "$CLIENT" "code has no DB path or credentials"          "! grep -En 'tracks\\.db|sqlite|REST_API|[0-9a-f]{32}' mcp-client/connect.py mcp-client/call_tools.py"
check "$MCP"    "code has no database access"                 "! grep -En 'sqlite' mcp-server/server.py"
check "$SCRIPT" "tokens never appear in any log"              "! grep -rF -e '$REST_API_TOKEN' -e '$MCP_API_TOKEN' rest-api/logs mcp-server/logs"
rid=$(grep 'tool=get_aircraft_by_id.* OK' mcp-server/logs/mcp-server.log | head -1 | sed -E 's/.*rid=([a-f0-9]+) .*/\1/')
check "$SCRIPT" "request $rid is in both the MCP and REST logs" "grep -q \"rid=$rid \" mcp-server/logs/mcp-server.log && grep -q \"rid=$rid .*-> 200\" rest-api/logs/rest-api.log"
check "$SCRIPT" "rejections are logged by both servers"       "grep -q REJECTED rest-api/logs/rest-api.log && grep -q REJECTED mcp-server/logs/mcp-server.log"

echo
if [ $fails -eq 0 ]; then echo "ALL CHECKS PASSED"; else
  echo "$fails CHECK GROUP(S) FAILED"
  for f in rest-api/logs/stderr.log mcp-server/logs/stderr.log; do [ -s $f ] && { echo "== $f"; tail -20 $f; }; done
fi
exit $((fails > 0))
