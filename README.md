# MCP Air Traffic Demo

A small, fully local practice pipeline showing how an MCP (Model Context Protocol) client reaches data
through an MCP server, a private REST API, and a read-only SQLite database. **All data is fake.**

```
MCP CLIENT ──► MCP SERVER ──► REST API ──► DATABASE
(scripts)      :8000          :8001        tracks.db (SQLite, read-only)
bearer token   bearer token   validates + parameterized queries
```

Each layer is its own process, in its own folder with its own virtual environment. Everything runs on
one machine, so the separation comes from processes, ports and folders, not separate hosts.

## Run it

```bash
./run_tests.sh
```

One command starts both servers with throwaway tokens, then:

1. Starts the services and creates the fake database (5 aircraft tracks over the Gulf region).
2. Has the MCP client discover the server's tools.
3. Calls each tool and follows the request through every layer (matching request ID in each log),
   shows the rows read straight from the database, and checks the client's result matches them.
4. Runs the MCP client test suite: valid calls plus 16 invalid inputs that must be refused safely.
5. Runs security and logging checks, then prints a per-component scorecard and stops the servers.

Every output line is labelled `[MCP CLIENT]`, `[MCP SERVER]`, `[REST API]`, `[DATABASE]` or `[SCRIPT]`.

## Layout

| Path | What it is |
|---|---|
| `mcp-client/connect.py` | Connects over streamable HTTP with a bearer token; shared helpers |
| `mcp-client/call_tools.py` | Demo: lists tools, calls each, prints the result |
| `mcp-client/test_tools.py` | Test suite: valid data, invalid input, resources |
| `mcp-server/server.py` | MCP server: 5 tools + 2 resources; no database access, calls the REST API |
| `rest-api/app.py` | Private REST API: validation, read-only queries, query timeout; also seeds the fake data |
| `run_tests.sh` | End-to-end pipeline test |

## Tools exposed by the MCP server

| Tool | Purpose |
|---|---|
| `get_aircraft_by_id` | One aircraft's record |
| `list_recent_aircraft` | Most recently updated aircraft (limit 1-100) |
| `find_aircraft_in_area` | Aircraft inside a latitude/longitude box |
| `count_aircraft` | Count, optionally by classification |
| `get_server_status` | Confirms the server and data layer are up |

Resources: `schema://aircraft` (record fields) and `info://server` (tools and limits).

## Security design

- Bearer token on both the MCP endpoint and the REST API; they use different tokens.
- Input validated at both the MCP server and the REST API; bad input returns a short, safe error.
- Database opened read-only; queries are parameterized; results capped at 100 rows; 2 s query timeout.
- The client contains no database path or credentials, and the MCP server has no database access.
- Tokens come from environment variables and are never written to files or logs.
- Every request gets an ID that is logged by both servers, so a request can be traced across layers.

## Setup (first time)

Each folder has its own `.venv` and `requirements.txt` (Python 3.13):

```bash
for d in mcp-client mcp-server rest-api; do
  python3 -m venv $d/.venv && $d/.venv/bin/pip install -r $d/requirements.txt
done
```

The MCP Inspector (optional, needs Node.js) is run with `npx @modelcontextprotocol/inspector@2.8.0`;
it must be given the MCP server's token as an `Authorization: Bearer ...` header.

## Limitations and next steps

- All data is synthetic; field names (for example `classification`) are placeholders until real data is known.
- Everything runs on one machine over localhost, so isolation is weaker than separate hosts.
- No LLM, natural-language interface, write actions, or guardrail layer yet; tools are read-only.
- The Inspector's browser interface has not been tested against the token-protected server.
