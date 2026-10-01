"""Private REST API over a switchable flight-data source. Only the MCP server should call it.
Env: REST_API_TOKEN (>= 16 chars, required), REST_API_PORT (default 8001),
     DATA_SOURCE = db (default, fake SQLite data) | opensky (live OpenSky Network data)."""
import hmac, logging, math, os, re, sys, time, uuid
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from sources import ApiError, DbSource, OpenSkySource

HERE, VERSION, MAX = Path(__file__).parent, "0.3.0", 100
TOKEN = os.environ.get("REST_API_TOKEN", "")
SRC = OpenSkySource() if os.environ.get("DATA_SOURCE", "db") == "opensky" else DbSource()

# --- Logging (console + file; tokens are never logged) ---
(HERE / "logs").mkdir(exist_ok=True)
log = logging.getLogger("rest-api")
log.setLevel(logging.INFO)
log.propagate = False
for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(HERE / "logs/rest-api.log")):
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)


# --- Validation ---
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


# --- Endpoints (the source runs in a thread so slow upstream calls never block the server) ---
async def health(r):
    return JSONResponse({"status": "ok", "version": VERSION, "source": SRC.name,
                         "record_count": await run_in_threadpool(SRC.health)})


async def one(r):
    rec = await run_in_threadpool(SRC.one, r.path_params["track_id"])
    if rec is None:
        raise ApiError(404, "not_found", "No aircraft with that track ID.")
    return JSONResponse(rec)


async def recent(r):
    n = num(r.query_params.get("limit"), "limit", 1, MAX, int, 20)
    return JSONResponse({"aircraft": await run_in_threadpool(SRC.recent, n)})


async def area(r):
    q = r.query_params
    lat = tuple(num(q.get(k), k, -90, 90) for k in ("lat_min", "lat_max"))
    lon = tuple(num(q.get(k), k, -180, 180) for k in ("lon_min", "lon_max"))
    if lat[0] > lat[1] or lon[0] > lon[1]:
        raise bad("min values must not exceed max values.")
    n = num(q.get("limit"), "limit", 1, MAX, int, 20)
    return JSONResponse({"aircraft": await run_in_threadpool(SRC.area, lat, lon, n)})


async def count(r):
    c = r.query_params.get("classification")
    if c is not None and not re.fullmatch(r"[a-z]{1,20}", c):
        raise bad("classification must be 1-20 lowercase letters.")
    return JSONResponse({"count": await run_in_threadpool(SRC.count, c)})


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
    log.info("data source: %s", SRC.name)
    # 127.0.0.1 = this Mac only.
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("REST_API_PORT", "8001")),
                access_log=False, log_level="warning")
