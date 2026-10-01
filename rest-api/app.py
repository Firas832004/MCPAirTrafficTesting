"""Private REST API over a SQLite track DB (FAKE data). Only the MCP server should call it.
Env: REST_API_TOKEN (>= 16 chars, required), REST_API_PORT (default 8001)."""
import hmac, logging, math, os, re, sqlite3, sys, time, uuid
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

HERE, VERSION, MAX, TIMEOUT = Path(__file__).parent, "0.1.0", 100, 2.0
DB, TOKEN = HERE / "tracks.db", os.environ.get("REST_API_TOKEN", "")

# --- Logging (console + file; tokens are never logged) ---
(HERE / "logs").mkdir(exist_ok=True)
log = logging.getLogger("rest-api")
log.setLevel(logging.INFO)
log.propagate = False
for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(HERE / "logs/rest-api.log")):
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)

# --- Fake data: 5 tracks over the Gulf / Arabian Peninsula (invented callsigns and addresses) ---
COLUMNS = ("track_id INTEGER PRIMARY KEY, callsign, icao24, aircraft_type, classification, lat REAL, "
           "lon REAL, altitude_ft INT, speed_kts INT, heading_deg INT, squawk, last_update")
TRACKS = [
    (1, "TST401", "7C1A01", "B77W", "civil", 26.1520, 51.8830, 37000, 490, 310, "4521", "2026-01-01T11:59:42+00:00"),
    (2, "ALP218", "7C1A02", "A320", "civil", 25.4310, 55.2170, 33000, 455, 95, "2214", "2026-01-01T11:59:38+00:00"),
    (3, "CRG907", "7C1A03", "B748", "cargo", 29.3120, 47.9020, 35000, 480, 220, "5307", "2026-01-01T11:59:45+00:00"),
    (4, "BRV115", "7C1A04", "E190", "civil", 24.7650, 46.7010, 8500, 240, 175, "3142", "2026-01-01T11:59:30+00:00"),
    (5, "UNK005", "7C1A05", "C172", "unknown", 27.0480, 49.6100, 4500, 105, 60, "7000", "2026-01-01T11:59:20+00:00"),
]


def seed() -> None:
    with sqlite3.connect(DB) as conn:
        conn.execute(f"CREATE TABLE tracks ({COLUMNS})")
        conn.executemany(f"INSERT INTO tracks VALUES ({','.join('?' * 12)})", TRACKS)


def db(sql: str, params: tuple = ()) -> list[dict]:
    """Read-only, parameterized query with a hard time limit."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=TIMEOUT)
    deadline = time.monotonic() + TIMEOUT
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)  # truthy return aborts
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError as e:
        raise ApiError(504, "query_timeout", "The query took too long.") if "interrupted" in str(e) else e
    finally:
        conn.close()


# --- Errors and validation ---
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def bad(msg: str) -> ApiError:
    return ApiError(422, "invalid_input", msg)


def num(raw, name, lo, hi, kind=float, default=None):
    if raw is None:
        if default is None:
            raise bad(f"{name} is required.")
        return default
    try:
        v = kind(raw)
    except ValueError:
        raise bad(f"{name} must be a {kind.__name__}.") from None
    if not (math.isfinite(v) and lo <= v <= hi):
        raise bad(f"{name} must be between {lo} and {hi}.")
    return v


# --- Endpoints ---
async def health(r):
    return JSONResponse({"status": "ok", "version": VERSION, "record_count": db("SELECT COUNT(*) n FROM tracks")[0]["n"]})


async def one(r):
    rows = db("SELECT * FROM tracks WHERE track_id = ?", (r.path_params["track_id"],))
    if not rows:
        raise ApiError(404, "not_found", "No aircraft with that track ID.")
    return JSONResponse(rows[0])


async def recent(r):
    n = num(r.query_params.get("limit"), "limit", 1, MAX, int, 20)
    return JSONResponse({"aircraft": db("SELECT * FROM tracks ORDER BY last_update DESC, track_id LIMIT ?", (n,))})


async def area(r):
    q = r.query_params
    lat = [num(q.get(k), k, -90, 90) for k in ("lat_min", "lat_max")]
    lon = [num(q.get(k), k, -180, 180) for k in ("lon_min", "lon_max")]
    if lat[0] > lat[1] or lon[0] > lon[1]:
        raise bad("min values must not exceed max values.")
    n = num(q.get("limit"), "limit", 1, MAX, int, 20)
    return JSONResponse({"aircraft": db(
        "SELECT * FROM tracks WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? ORDER BY track_id LIMIT ?",
        (*lat, *lon, n))})


async def count(r):
    c = r.query_params.get("classification")
    if c is not None and not re.fullmatch(r"[a-z]{1,20}", c):
        raise bad("classification must be 1-20 lowercase letters.")
    where, args = ("WHERE classification = ?", (c,)) if c else ("", ())
    return JSONResponse({"count": db(f"SELECT COUNT(*) n FROM tracks {where}", args)[0]["n"]})


# --- Middleware: request ID, token check, one log line per request, safe 500s ---
def err(status: int, code: str, msg: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": msg}}, status_code=status)


class Guard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id", "")  # reuse the caller's ID so logs chain
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", rid):
            rid = uuid.uuid4().hex[:12]
        where = f"{request.method} {request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        t0 = time.monotonic()
        if not hmac.compare_digest(token.encode(), TOKEN.encode()):
            log.warning("rid=%s REJECTED 401 unauthorized %s", rid, where)
            resp = err(401, "unauthorized", "Missing or invalid token.")
        else:
            try:
                resp = await call_next(request)
            except Exception:
                log.exception("rid=%s ERROR 500 %s", rid, where)  # details in the log only
                resp = err(500, "internal_error", "Internal server error.")
            else:
                log.log(logging.WARNING if resp.status_code >= 400 else logging.INFO, "rid=%s %s -> %s (%.0f ms)",
                        rid, where, resp.status_code, (time.monotonic() - t0) * 1000)
        resp.headers["x-request-id"] = rid
        return resp


async def http_error(request, exc):
    return err(exc.status_code, "not_found" if exc.status_code == 404 else "bad_request",
               "Not found." if exc.status_code == 404 else "Request not allowed.")


app = Starlette(
    routes=[Route("/v1/health", health), Route("/v1/aircraft/count", count), Route("/v1/aircraft/area", area),
            Route("/v1/aircraft/{track_id:int}", one), Route("/v1/aircraft", recent)],
    middleware=[Middleware(Guard)],
    exception_handlers={ApiError: lambda r, e: err(e.status, e.code, e.message), 404: http_error, 405: http_error},
)

if __name__ == "__main__":
    if len(TOKEN) < 16:
        sys.exit("Set REST_API_TOKEN to a random string of at least 16 characters.")
    if not DB.exists():
        seed()
    # 127.0.0.1 = this Mac only.
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("REST_API_PORT", "8001")),
                access_log=False, log_level="warning")
