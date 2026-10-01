"""Shared client helpers. Env: MCP_API_TOKEN (required, never stored in a file), MCP_SERVER_URL
(default http://127.0.0.1:8000/mcp)."""
import asyncio, json, os, sys
from contextlib import asynccontextmanager

from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")


@asynccontextmanager
async def connect():
    """Connect over streamable HTTP with the bearer token; entering runs the MCP initialize handshake."""
    token = os.environ.get("MCP_API_TOKEN") or sys.exit("Set MCP_API_TOKEN to the token the server uses.")
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with Client(streamable_http_client(URL, http_client=http)) as client:
            yield client


async def call(client, tool: str, args: dict | None = None):
    """Call a tool -> ('ok', data) or ('error', message). Never raises for tool/schema errors."""
    try:
        r = await client.call_tool(tool, args or {})
    except Exception as e:  # protocol-level rejection, e.g. arguments failing the schema
        return "error", str(e)
    if r.is_error:
        return "error", r.content[0].text
    sc = r.structured_content  # lists/ints arrive wrapped as {"result": ...}; plain dicts as JSON text
    return "ok", (sc["result"] if sc and set(sc) == {"result"} else sc or json.loads(r.content[0].text))


def run(main):
    """Run an async main(); print one short line instead of a traceback on failure."""
    try:
        return asyncio.run(main())
    except Exception as e:
        while getattr(e, "exceptions", None):  # unwrap ExceptionGroup to the root cause
            e = e.exceptions[0]
        sys.exit(f"Error: {type(e).__name__}: {e} (is the server running and the token correct?)")
