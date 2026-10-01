# MCP Air Traffic Demo

A small, fully local practice pipeline showing how an MCP (Model Context Protocol) client reaches flight data
through an MCP server and a private REST API, with a chat page where Claude chooses and calls the MCP tools.
The data source is switchable: **`demo2` = fake data in a read-only SQLite database (default)**, **`demo1` = live
public ADS-B data from the OpenSky Network**.

```
MCP CLIENT ──► MCP SERVER ──► REST API ──► DATABASE (demo2)  or  OPENSKY API (demo1)
(scripts/chat) :8000          :8001        tracks.db (SQLite)          opensky-network.org (anonymous)
bearer token   bearer token   validates + parameterized queries / cached, rate-limit aware
```

### Chat flow (`./run_tests.sh demo1 chat`)

1. You type a question in the chat page (`http://127.0.0.1:8002`).
2. The MCP client asks the MCP server for its tools and gets the 5 tools with descriptions and argument schemas.
3. The MCP client sends Claude your question plus those tool descriptions.
4. Claude chooses a tool and its arguments; the MCP client calls that tool on the MCP server.
5. The MCP server asks the REST API, which fetches the data (database or OpenSky) and returns it up the chain.
6. The MCP client gives the result to Claude, which may call more tools, then writes the answer for you.

Each layer is its own process, in its own folder with its own virtual environment. Everything runs on
one machine, so the separation comes from processes, ports and folders, not separate hosts.

## Run it

```bash
./run_tests.sh              # demo2 (default): fake data from the local database
./run_tests.sh demo1        # live flight data from OpenSky (needs internet; anonymous limit ~400 requests/day)
./run_tests.sh demo2 chat   # after the checks pass, also open the chat page at http://127.0.0.1:8002
```

The chat page needs Claude credentials, read by the Anthropic SDK from the terminal environment (never stored in
files): `export ANTHROPIC_API_KEY=...` in the same terminal before running. The model is `claude-haiku-4-5` unless
`CLAUDE_MODEL` is set. Press Ctrl+C to stop the chat page and both servers.

One command starts both servers with throwaway tokens, then:

1. Starts the services (`demo2` creates the fake database: 5 tracks over the Gulf region; `demo1` checks the live source).
2. Has the MCP client discover the server's tools.
3. Calls each tool and follows the request through every layer (matching request ID in each log),
   shows the rows read straight from the database (`demo2`) or the live source's answer (`demo1`), and checks the client's result matches.
4. Runs the MCP client test suite: valid calls plus 16 invalid inputs that must be refused safely.
5. Runs security and logging checks, then prints a per-component scorecard and stops the servers.

Every output line is labelled `[MCP CLIENT]`, `[MCP SERVER]`, `[REST API]`, `[DATABASE]`, `[OPENSKY]` or `[SCRIPT]`.

## Layout

| Path | What it is |
|---|---|
| `mcp-client/connect.py` | Connects over streamable HTTP with a bearer token; shared helpers |
| `mcp-client/call_tools.py` | Demo: lists tools, calls each, prints the result |
| `mcp-client/test_tools.py` | Test suite: valid data, invalid input, resources |
| `mcp-client/chat_server.py`, `chat.html` | Chat page: Claude + MCP tools, ChatGPT-style UI, local only (port 8002) |
| `mcp-server/server.py` | MCP server: 5 tools + 2 resources; no database access, calls the REST API |
| `rest-api/app.py` | Private REST API: token check, validation, request-ID logging |
| `rest-api/sources.py` | Data sources behind one interface: fake SQLite (`DATA_SOURCE=db`) or live OpenSky (`DATA_SOURCE=opensky`) |
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
- Tokens and Claude credentials come from environment variables and are never written to files or logs.
- Live mode: upstream calls are cached for 10 s, time-limited, and failures become generic errors (rate limit, unavailable).
- The chat endpoint only accepts requests from its own page (custom header) and listens on localhost only.
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

- `demo2` data is synthetic; field names (for example `classification`) are placeholders until real data is known.
- `demo1` uses public OpenSky data (non-commercial, research/personal use; community receivers, so coverage over the
  Gulf can be patchy). Live records have no aircraft type, `classification` is `airborne`/`ground`, and `track_id` is the
  24-bit ICAO address as an integer.
- Everything runs on one machine over localhost, so isolation is weaker than separate hosts.
- Claude only reads data through the tools; there are no write actions and no guardrail layer beyond validation yet.
- The Inspector's browser interface has not been tested against the token-protected server.
- The chat page was tested with a stand-in for Claude (tool loop against the real MCP server); a full run with real
  Claude credentials is done by the user.

---

## Versioning and change log (read this before changing anything, including if you are an AI assistant)

**Current version: 0.3.0**

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
5. **Tag releases:** after a release is merged into `main`, run `git tag vX.Y.Z` on that `main` commit, then `git push origin vX.Y.Z`.

### Rules for working on this repo

- **Branches:** `main` is always the latest tested release; `develop` is the shared branch the whole team integrates on.
  Start work from `develop` on a short-lived branch (`feature/<what>` or `fix/<what>`), open a pull request back into
  `develop`, and run `./run_tests.sh` before opening it. Merge `develop` into `main` (by pull request) only when
  `./run_tests.sh` passes and the version and change log are up to date. Nobody commits directly to `main` or `develop`.
- Never rewrite published history (no force-push to `main`).
- Never commit real or employer data. `demo2` data is fake; `demo1` may use only public data from the OpenSky API. Never commit tokens, passwords, `.env` files, real or employer data, `.venv/`, logs or `*.db`
  (the `.gitignore` already excludes them). Tokens come from environment variables.
- AI assistants: do not push, publish or create tags without the user's explicit approval for that action.
- Keep the architecture rule: the MCP client talks only to the MCP server; only the REST API touches the database.

### Change log

### 0.3.0 - 2026-10-01
- Added: live data source (OpenSky Network) in `rest-api/sources.py`, selected with `DATA_SOURCE=opensky`; the fake
  SQLite source moved into the same module (`DATA_SOURCE=db`, default). Records use the same shape in both.
- Added: `./run_tests.sh demo1` (live) / `demo2` (fake, default); per-call trace compares the client's result with the
  database rows (`demo2`) or with the REST API's own answer for the same request (`demo1`).
- Added: chat page (`mcp-client/chat_server.py`, `chat.html`): Claude (default `claude-haiku-4-5`) gets the MCP tools,
  chooses and calls them, and answers; `./run_tests.sh [demo1|demo2] chat` starts it.
- Changed: `/v1/health` now also reports `source`; `count_aircraft` description covers live classifications;
  demo and tests adapt to `DATA_MODE` (`db`/`api`); `anthropic` added to the client requirements.
- Tests: demo2 and demo1 pass (31 and 31 checks); chat tool loop verified against the real MCP server with a stand-in for Claude.

### 0.2.2 - 2026-10-01
- Added: shared `develop` branch for team collaboration.
- Changed: README branch rules now describe the `main` / `develop` / feature-branch flow and when to tag.
- Changed: `VERSION` constants bumped to 0.2.2 (docs and process only).
- Tests: 31 checks pass.

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
