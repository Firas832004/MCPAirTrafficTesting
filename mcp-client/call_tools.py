"""Demo: discover the server's tools, call a few, print what comes back. Needs MCP_API_TOKEN."""
import json

from connect import call, connect, run

GULF = {"lat_min": 25, "lat_max": 28, "lon_min": 48, "lon_max": 56}
CALLS = [("get_server_status", {}), ("get_aircraft_by_id", {"track_id": 3}), ("list_recent_aircraft", {"limit": 3}),
         ("find_aircraft_in_area", GULF), ("count_aircraft", {}), ("count_aircraft", {"classification": "civil"})]


def aircraft(t) -> str:
    return f"#{t['track_id']} {t['callsign']} {t['aircraft_type']} ({t['classification']}) FL{t['altitude_ft'] // 100:03d}"


def summarize(data) -> str:
    """Short human-readable form of a tool result."""
    rows = data if isinstance(data, list) else [data]
    if rows and isinstance(rows[0], dict) and "track_id" in rows[0]:
        return f"{len(rows)} aircraft: " + "; ".join(aircraft(r) for r in rows)
    return json.dumps(data)


async def main() -> None:
    async with connect() as client:
        tools = (await client.list_tools()).tools
        print(f"discovered {len(tools)} tools")
        for t in tools:
            print(f"tool {t.name}: {t.description}")
        for tool, args in CALLS:
            print(f"called {tool} {json.dumps(args)}")
            status, data = await call(client, tool, args)
            print(f"received {summarize(data) if status == 'ok' else 'ERROR: ' + str(data)}")


if __name__ == "__main__":
    run(main)
