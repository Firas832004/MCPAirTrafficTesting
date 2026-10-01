"""Practice MCP server for FAKE air traffic tracks. No database access: each tool validates its
input, then calls the private REST API with a token.
Env: MCP_API_TOKEN and REST_API_TOKEN (>= 16 chars, required), REST_API_URL, MCP_PORT (default 8000)."""
import contextvars, functools, hmac, json, logging, math, os, re, sys, urllib.error, urllib.parse
import urllib.request, uuid
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

HERE, VERSION, MAX = Path(__file__).parent, "0.3.0", 100
MCP_TOKEN, REST_TOKEN = os.environ.get("MCP_API_TOKEN", ""), os.environ.get("REST_API_TOKEN", "")
REST_URL = os.environ.get("REST_API_URL", "http://127.0.0.1:8001").rstrip("/")

# --- Logging (console + file; tokens are never logged) ---
(HERE / "logs").mkdir(exist_ok=True)
log = logging.getLogger("mcp-server")
log.setLevel(logging.INFO)
log.propagate = False
for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(HERE / "logs/mcp-server.log")):
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)

rid_var: contextvars.ContextVar[str] = contextvars.ContextVar("rid", default="-")  # forwarded to REST
mcp = MCPServer("air-traffic-practice", version=VERSION)


def num(v, name, lo, hi, kind=float):
    """Validate a numeric tool argument (the REST API validates again)."""
    if isinstance(v, bool) or not isinstance(v, int if kind is int else (int, float)) \
            or not math.isfinite(v) or not lo <= v <= hi:
        raise ToolError(f"{name} must be {'an integer' if kind is int else 'a number'} between {lo} and {hi}.")
    return v


def rest_get(path: str, params: dict | None = None) -> dict:
    """Call the REST API; every failure becomes a safe, generic tool error."""
    url = REST_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {REST_TOKEN}", "X-Request-ID": rid_var.get()})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (404, 422):  # caller's mistake: the REST API's own message is safe to relay
            raise ToolError(json.load(e)["error"]["message"]) from None
        log.error("rid=%s REST returned %s for %s", rid_var.get(), e.code, path)
    except OSError:
        log.error("rid=%s REST unreachable for %s", rid_var.get(), path)
    raise ToolError("The data service is unavailable.")


def tool(fn):
    """Register fn as an MCP tool with a request ID and one log line per call."""
    @functools.wraps(fn)  # keeps the signature the SDK builds the schema from
    def wrapper(**kw):
        rid = uuid.uuid4().hex[:12]
        rid_var.set(rid)
        try:
            out = fn(**kw)
        except ToolError as e:
            log.warning("rid=%s tool=%s args=%s REJECTED: %s", rid, fn.__name__, kw, e)
            raise
        except Exception:
            log.exception("rid=%s tool=%s args=%s ERROR", rid, fn.__name__, kw)
            raise ToolError("Internal error.") from None  # details stay in the log
        log.info("rid=%s tool=%s args=%s OK", rid, fn.__name__, kw)
        return out
    return mcp.tool()(wrapper)


@tool
def get_aircraft_by_id(track_id: int) -> dict:
    """Get one aircraft's record (position, altitude, heading, speed, last update) by track ID."""
    return rest_get(f"/v1/aircraft/{num(track_id, 'track_id', 1, 2**31 - 1, int)}")


@tool
def list_recent_aircraft(limit: int = 20) -> list[dict]:
    """List the most recently updated aircraft. limit is 1-100 (default 20)."""
    return rest_get("/v1/aircraft", {"limit": num(limit, "limit", 1, MAX, int)})["aircraft"]


@tool
def find_aircraft_in_area(lat_min: float, lat_max: float, lon_min: float, lon_max: float, limit: int = 20) -> list[dict]:
    """Find aircraft inside a lat/lon bounding box (decimal degrees; west longitudes negative)."""
    p = {"lat_min": num(lat_min, "lat_min", -90, 90), "lat_max": num(lat_max, "lat_max", -90, 90),
         "lon_min": num(lon_min, "lon_min", -180, 180), "lon_max": num(lon_max, "lon_max", -180, 180),
         "limit": num(limit, "limit", 1, MAX, int)}
    if p["lat_min"] > p["lat_max"] or p["lon_min"] > p["lon_max"]:
        raise ToolError("min values must not exceed max values.")
    return rest_get("/v1/aircraft/area", p)["aircraft"]


@tool
def count_aircraft(classification: str | None = None) -> int:
    """Count tracked aircraft, optionally only one classification (demo data: civil, cargo, unknown; live data: airborne, ground)."""
    if classification is not None and not re.fullmatch(r"[a-z]{1,20}", classification):
        raise ToolError("classification must be 1-20 lowercase letters.")
    return rest_get("/v1/aircraft/count", {"classification": classification} if classification else None)["count"]


@tool
def get_server_status() -> dict:
    """Confirm this server and its data layer are up; returns status, version and record count."""
    try:
        n = rest_get("/v1/health")["record_count"]
    except ToolError:
        return {"status": "degraded", "version": VERSION, "data_layer": "unreachable", "record_count": None}
    return {"status": "ok", "version": VERSION, "data_layer": "ok", "record_count": n}


# --- Resources: read-only context for a future agent ---
@mcp.resource("schema://aircraft", mime_type="text/plain", description="Fields of an aircraft record")
def aircraft_schema() -> str:
    return ("Aircraft record fields (all data is FAKE): track_id (int), callsign, icao24 (invented hex address), "
            "aircraft_type (ICAO code), classification (civil|cargo|unknown), lat/lon (decimal degrees, west "
            "negative), altitude_ft, speed_kts, heading_deg (0-359), squawk (4 digits), last_update (ISO 8601 UTC)")


@mcp.resource("info://server", mime_type="text/plain", description="Tools and limits of this server")
def server_info() -> str:
    return (f"air-traffic-practice v{VERSION}, read-only, fake data. Tools: get_aircraft_by_id, "
            f"list_recent_aircraft, find_aircraft_in_area, count_aircraft, get_server_status. "
            f"Limits: limit must be 1-{MAX}; no write actions exist.")


class TokenGuard:
    """Plain ASGI token check on the MCP endpoint (keeps streaming responses intact)."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            token = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(token.encode(), MCP_TOKEN.encode()):
                log.warning("rid=%s REJECTED 401 unauthorized %s %s", uuid.uuid4().hex[:12], scope["method"], scope["path"])
                body = b'{"error":{"code":"unauthorized","message":"Missing or invalid token."}}'
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                return await send({"type": "http.response.body", "body": body})
        await self.app(scope, receive, send)


if __name__ == "__main__":
    if len(MCP_TOKEN) < 16 or len(REST_TOKEN) < 16:
        sys.exit("Set MCP_API_TOKEN and REST_API_TOKEN (random strings, >= 16 chars each).")
    app = mcp.streamable_http_app(host="127.0.0.1")
    app.add_middleware(TokenGuard)
    # 127.0.0.1 = this Mac only.
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MCP_PORT", "8000")),
                access_log=False, log_level="warning")
