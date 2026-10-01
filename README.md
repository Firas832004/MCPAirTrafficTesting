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

---

## Versioning and change log (read this before changing anything, including if you are an AI assistant)

**Current version: 0.2.1**

This project uses [Semantic Versioning](https://semver.org): `MAJOR.MINOR.PATCH` (for example `0.2.1`).
It is pre-1.0, so the public interface may still change; breaking changes bump MINOR and are marked **BREAKING**.

### What to bump

| Kind of change | Bump | Examples here |
|---|---|---|
| Breaking change to the public interface (after 1.0 bump MAJOR; before 1.0 bump MINOR and mark **BREAKING**) | MAJOR / MINOR | Renaming or removing an MCP tool, changing a tool's arguments or return shape, changing a REST path, changing the token scheme or ports |
| New backward-compatible feature | MINOR | A new tool, resource or endpoint; new test stages in `run_tests.sh` |
| Fix, refactor, documentation, test-only or log-only change that does not alter behavior | PATCH | Fixing validation, wording changes, README updates |

If a change fits more than one row, use the highest. Never reuse or renumber a published version.

### Every change must do all of these

1. **Run `./run_tests.sh` and make sure it passes.** Do not bump the version for a failing build.
2. **Bump the version everywhere it appears** (they must always match):
   - `VERSION` in `rest-api/app.py`
   - `VERSION` in `mcp-server/server.py`
   - "Current version" above
   - a new heading in the change log below
3. **Add a change log entry** (newest first) under a heading `### X.Y.Z - YYYY-MM-DD`. List each change under
   `Added`, `Changed`, `Fixed`, `Removed` or `Security` (omit empty ones), say what changed and why in one line each,
   mark breaking changes **BREAKING**, and finish with the test result (for example "31 checks pass").
4. **Write a commit message** whose first line summarises the change, followed by a body that lists the changes
   (same content as the change log entry). Keep AI co-author trailers if an AI assistant helped.
5. **Tag releases:** `git tag vX.Y.Z` on the commit that bumps the version, then `git push origin vX.Y.Z`.

### Rules for working on this repo

- Work on a short-lived branch (`feature/<what>` or `fix/<what>`), not directly on `main`; merge by pull request,
  and run `./run_tests.sh` before opening it.
- Never rewrite published history (no force-push to `main`).
- Fake data only. Never commit tokens, passwords, `.env` files, real or employer data, `.venv/`, logs or `*.db`
  (the `.gitignore` already excludes them). Tokens come from environment variables.
- AI assistants: do not push, publish or create tags without the user's explicit approval for that action.
- Keep the architecture rule: the MCP client talks only to the MCP server; only the REST API touches the database.

### Change log

### 0.2.1 - 2026-10-01
- Changed: README now documents versioning, the change log process, and contribution rules (docs only).
- Changed: `VERSION` constants in `rest-api/app.py` and `mcp-server/server.py` bumped to 0.2.1 so they match this
  document (visible in the `/v1/health` response and the `get_server_status` tool).
- Tests: 31 checks pass.

### 0.2.0 - 2026-09-30
- Changed: `run_tests.sh` rewritten into five labelled stages with a banner and a per-component scorecard.
- Added: each demo call is traced through MCP client, MCP server, REST API and database by request ID; the matching rows
  are read straight from `tracks.db` and compared with what the MCP client received.
- Added: `README.md` with architecture, usage, security design and limitations.
- Changed: `mcp-client/call_tools.py` prints `called`/`received` lines; `mcp-client/test_tools.py` final message
  reworded.
- Tests: 31 checks pass.

### 0.1.0 - 2026-09-30
- Added: first working pipeline: MCP client scripts, MCP server (5 tools, 2 resources), private REST API, SQLite
  database with 5 fake Gulf-region aircraft tracks.
- Added: bearer-token authentication on both servers, input validation at both layers, read-only database access,
  request-ID logging in both servers, and `run_tests.sh` (end-to-end test).
