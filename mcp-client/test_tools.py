"""Calls every tool with valid and invalid inputs; prints PASS/FAIL; exit 1 on any failure.
Needs MCP_API_TOKEN (see connect.py)."""
import json, sys

from connect import call, connect, run

LEAKS = ("Traceback", "sqlite", "tracks.db", "/Users/", "app.py", "server.py", "Bearer")  # never allowed in an error
G = {"lat_min": 25, "lat_max": 28, "lon_min": 48, "lon_max": 56}  # box over the Gulf
TOOLS = {"get_aircraft_by_id", "list_recent_aircraft", "find_aircraft_in_area", "count_aircraft", "get_server_status"}

# (label, tool, args, check) - check is a function for expected success, or None for expected safe error
CASES = [
    ("status ok", "get_server_status", {}, lambda r: r["status"] == "ok" and r["record_count"] == 5),
    ("get by id", "get_aircraft_by_id", {"track_id": 3}, lambda r: r["callsign"] == "CRG907"),
    ("list default", "list_recent_aircraft", {}, lambda r: len(r) == 5),
    ("list limit 2", "list_recent_aircraft", {"limit": 2}, lambda r: len(r) == 2),
    ("area over Gulf", "find_aircraft_in_area", G, lambda r: {t["track_id"] for t in r} == {1, 2, 5}),
    ("area empty", "find_aircraft_in_area", {**G, "lat_min": 0, "lat_max": 1}, lambda r: r == []),
    ("count all", "count_aircraft", {}, lambda r: r == 5),
    ("count civil", "count_aircraft", {"classification": "civil"}, lambda r: r == 3),
    ("id not found", "get_aircraft_by_id", {"track_id": 9999}, None),
    ("id zero", "get_aircraft_by_id", {"track_id": 0}, None),
    ("id negative", "get_aircraft_by_id", {"track_id": -1}, None),
    ("id huge", "get_aircraft_by_id", {"track_id": 2**40}, None),
    ("id wrong type", "get_aircraft_by_id", {"track_id": "abc"}, None),
    ("id missing", "get_aircraft_by_id", {}, None),
    ("limit huge", "list_recent_aircraft", {"limit": 100000}, None),
    ("limit zero", "list_recent_aircraft", {"limit": 0}, None),
    ("lat out of range", "find_aircraft_in_area", {**G, "lat_max": 999}, None),
    ("lon out of range", "find_aircraft_in_area", {**G, "lon_min": -999}, None),
    ("min > max", "find_aircraft_in_area", {**G, "lat_min": 30}, None),
    ("coordinate not a number", "find_aircraft_in_area", {**G, "lat_min": "north"}, None),
    ("coordinates missing", "find_aircraft_in_area", {"lat_min": 1}, None),
    ("area limit huge", "find_aircraft_in_area", {**G, "limit": 5000}, None),
    ("SQL-style filter", "count_aircraft", {"classification": "x' OR 1=1 --"}, None),
    ("unknown tool", "delete_everything", {}, None),
]


async def main() -> int:
    fails = 0

    def report(label, ok, detail=""):
        nonlocal fails
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail and not ok else ""))

    async with connect() as client:
        names = {t.name for t in (await client.list_tools()).tools}
        report("lists all five tools", names == TOOLS, str(sorted(names)))
        rejected = 0
        for label, tool, args, check in CASES:
            status, data = await call(client, tool, args)
            if check:
                ok = status == "ok" and bool(check(data))
                report(f"{label}: {json.dumps(data)[:110]}" if ok else label, ok, f"{status}: {str(data)[:100]}")
            elif status == "error" and not [x for x in LEAKS if x in str(data)]:
                rejected += 1  # expected: bad input must be refused with a safe error
            else:
                report(f"{label} -> safe error", False, f"{status}: {str(data)[:100]}")
        n_bad = sum(c[3] is None for c in CASES)
        print(f"{'PASS' if rejected == n_bad else 'FAIL'}  {rejected}/{n_bad} invalid inputs rejected with safe errors")
        for uri, word in (("schema://aircraft", "track_id"), ("info://server", "get_aircraft_by_id")):
            report(f"resource {uri}", word in (await client.read_resource(uri)).contents[0].text)
    print(f"\n{fails} failure(s)" if fails else "\nAll MCP client tests passed")
    return fails > 0


if __name__ == "__main__":
    sys.exit(run(main))
