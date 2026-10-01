"""Data sources for the REST API. Both return records in the same shape:
track_id, callsign, icao24, aircraft_type, classification, lat, lon, altitude_ft, speed_kts,
heading_deg, squawk, last_update.
  DbSource      - FAKE aircraft in a local SQLite file (read-only queries, time-limited).
  OpenSkySource - LIVE public ADS-B data from the OpenSky Network (anonymous, cached, rate-limit aware)."""
import json, math, sqlite3, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path


class ApiError(Exception):
    """An error that is safe to show to callers (no internals)."""
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


# ---------------------------------------------------------------- fake database
DB, TIMEOUT = Path(__file__).parent / "tracks.db", 2.0
COLUMNS = ("track_id INTEGER PRIMARY KEY, callsign, icao24, aircraft_type, classification, lat REAL, "
           "lon REAL, altitude_ft INT, speed_kts INT, heading_deg INT, squawk, last_update")
TRACKS = [  # 5 invented tracks over the Gulf / Arabian Peninsula
    (1, "TST401", "7C1A01", "B77W", "civil", 26.1520, 51.8830, 37000, 490, 310, "4521", "2026-01-01T11:59:42+00:00"),
    (2, "ALP218", "7C1A02", "A320", "civil", 25.4310, 55.2170, 33000, 455, 95, "2214", "2026-01-01T11:59:38+00:00"),
    (3, "CRG907", "7C1A03", "B748", "cargo", 29.3120, 47.9020, 35000, 480, 220, "5307", "2026-01-01T11:59:45+00:00"),
    (4, "BRV115", "7C1A04", "E190", "civil", 24.7650, 46.7010, 8500, 240, 175, "3142", "2026-01-01T11:59:30+00:00"),
    (5, "UNK005", "7C1A05", "C172", "unknown", 27.0480, 49.6100, 4500, 105, 60, "7000", "2026-01-01T11:59:20+00:00"),
]


class DbSource:
    name = "database (fake data)"

    def __init__(self):
        if not DB.exists():
            with sqlite3.connect(DB) as conn:
                conn.execute(f"CREATE TABLE tracks ({COLUMNS})")
                conn.executemany(f"INSERT INTO tracks VALUES ({','.join('?' * 12)})", TRACKS)

    def _q(self, sql: str, params: tuple = ()) -> list[dict]:
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

    def health(self) -> int:
        return self._q("SELECT COUNT(*) n FROM tracks")[0]["n"]

    def one(self, track_id: int):
        rows = self._q("SELECT * FROM tracks WHERE track_id = ?", (track_id,))
        return rows[0] if rows else None

    def recent(self, n: int) -> list[dict]:
        return self._q("SELECT * FROM tracks ORDER BY last_update DESC, track_id LIMIT ?", (n,))

    def area(self, lat: tuple, lon: tuple, n: int) -> list[dict]:
        return self._q("SELECT * FROM tracks WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
                       "ORDER BY track_id LIMIT ?", (*lat, *lon, n))

    def count(self, cls: str | None) -> int:
        where, args = ("WHERE classification = ?", (cls,)) if cls else ("", ())
        return self._q(f"SELECT COUNT(*) n FROM tracks {where}", args)[0]["n"]


# ---------------------------------------------------------------- live OpenSky data
OPENSKY_URL, CACHE_TTL = "https://opensky-network.org/api/states/all", 10.0   # anonymous: >= 10 s between pulls
DEFAULT_BOX = ((16.0, 33.0), (34.0, 56.0))   # (lat_min, lat_max), (lon_min, lon_max): Saudi Arabia


class OpenSkySource:
    name = "OpenSky Network (live)"

    def __init__(self):
        self._cache: dict = {}

    def _fetch(self, **params) -> list[dict]:
        """One cached call to /states/all -> records. Failures become safe errors."""
        key = tuple(sorted(params.items()))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        req = urllib.request.Request(f"{OPENSKY_URL}?{urllib.parse.urlencode(params)}",
                                     headers={"User-Agent": "mcp-air-traffic-demo"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                states = json.load(resp).get("states") or []
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise ApiError(503, "rate_limited", "The flight data provider's rate limit was reached; try again later.") from None
            raise ApiError(502, "upstream_unavailable", "The flight data provider is unavailable.") from None
        except (OSError, ValueError):
            raise ApiError(502, "upstream_unavailable", "The flight data provider is unavailable.") from None
        records = [r for r in map(self._record, states) if r]
        self._cache[key] = (time.monotonic(), records)
        return records

    @staticmethod
    def _record(s: list):
        """OpenSky state vector -> our record shape. track_id is the 24-bit ICAO address as an integer."""
        icao, lon, lat = s[0], s[5], s[6]
        if lat is None or lon is None:
            return None
        alt = s[7] if s[7] is not None else s[13]
        return {
            "track_id": int(icao, 16), "callsign": (s[1] or "").strip() or None, "icao24": icao.upper(),
            "aircraft_type": None,                       # not provided by this endpoint
            "classification": "ground" if s[8] else "airborne",
            "lat": round(lat, 4), "lon": round(lon, 4),
            "altitude_ft": None if alt is None else round(alt * 3.28084),
            "speed_kts": None if s[9] is None else round(s[9] * 1.94384),
            "heading_deg": None if s[10] is None else round(s[10]) % 360,
            "squawk": s[14],
            "last_update": datetime.fromtimestamp(s[4] or 0, timezone.utc).isoformat(),
        }

    def _region(self) -> list[dict]:
        (la0, la1), (lo0, lo1) = DEFAULT_BOX
        return self._fetch(lamin=la0, lamax=la1, lomin=lo0, lomax=lo1)

    def health(self) -> int:
        return len(self._region())

    def one(self, track_id: int):
        if not 1 <= track_id <= 0xFFFFFF:
            return None
        rows = self._fetch(icao24=f"{track_id:06x}")
        return rows[0] if rows else None

    def recent(self, n: int) -> list[dict]:
        return sorted(self._region(), key=lambda r: r["last_update"], reverse=True)[:n]

    def area(self, lat: tuple, lon: tuple, n: int) -> list[dict]:
        rows = self._fetch(lamin=lat[0], lamax=lat[1], lomin=lon[0], lomax=lon[1])
        return sorted(rows, key=lambda r: r["last_update"], reverse=True)[:n]

    def count(self, cls: str | None) -> int:
        return sum(1 for r in self._region() if not cls or r["classification"] == cls)
