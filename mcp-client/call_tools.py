"""Demo: discover the server's tools, call a few, print what comes back. Needs MCP_API_TOKEN.
Env: DATA_MODE = db (default, fake data) | api (live data)."""
import json, os

from connect import call, connect, run

LIVE = os.environ.get("DATA_MODE") == "api"
SAUDI = {"lat_min": 16, "lat_max": 33, "lon_min": 34, "lon_max": 56}


def aircraft(t) -> str:
    alt = "?" if t["altitude_ft"] is None else f"FL{t['altitude_ft'] // 100:03d}"
    return f"#{t['track_id']} {t['callsign'] or '-'} {t['aircraft_type'] or '?'} ({t['classification']}) {alt}"


def summarize(data) -> str:
    """Short human-readable form of a tool result."""
    rows = data if isinstance(data, list) else [data]
    if rows and isinstance(rows[0], dict) and "track_id" in rows[0]:
        return f"{len(rows)} aircraft: " + "; ".join(aircraft(r) for r in rows)
    return json.dumps(data)


async def step(client, tool: str, args: dict):
    print(f"called {tool} {json.dumps(args)}")
    status, data = await call(client, tool, args)
    print(f"received {summarize(data) if status == 'ok' else 'ERROR: ' + str(data)}")
    return data


async def main() -> None:
    async with connect() as client:
        tools = (await client.list_tools()).tools
        print(f"discovered {len(tools)} tools")
        for t in tools:
            print(f"tool {t.name}: {t.description}")
        await step(client, "get_server_status", {})
        recent = await step(client, "list_recent_aircraft", {"limit": 3})
        await step(client, "get_aircraft_by_id", {"track_id": recent[0]["track_id"]})
        await step(client, "find_aircraft_in_area", SAUDI)
        await step(client, "count_aircraft", {})
        await step(client, "count_aircraft", {"classification": "airborne" if LIVE else "civil"})


if __name__ == "__main__":
    run(main)
