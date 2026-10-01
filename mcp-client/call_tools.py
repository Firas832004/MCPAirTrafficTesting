"""Demo: list the server's tools, then call each one. Needs MCP_API_TOKEN (see connect.py)."""
from connect import call, connect, run

GULF = {"lat_min": 25, "lat_max": 28, "lon_min": 48, "lon_max": 56}
CALLS = [("get_server_status", {}), ("list_recent_aircraft", {"limit": 3}), ("get_aircraft_by_id", {"track_id": 3}),
         ("find_aircraft_in_area", GULF), ("count_aircraft", {}),
         ("count_aircraft", {"classification": "civil"})]


def line(t) -> str:
    return f"#{t['track_id']} {t['callsign']} {t['aircraft_type']} ({t['classification']}) FL{t['altitude_ft'] // 100:03d}"


async def main() -> None:
    async with connect() as client:
        print("Tools:")
        for t in (await client.list_tools()).tools:
            print(f"  {t.name}: {t.description}")
        for tool, args in CALLS:
            status, data = await call(client, tool, args)
            rows = data if isinstance(data, list) else [data]
            shown = [line(r) for r in rows] if status == "ok" and rows and isinstance(rows[0], dict) \
                and "track_id" in rows[0] else data
            print(f"\n> {tool}({args}) [{status}]\n  {shown}")
        print("\n> schema://aircraft\n  " + (await client.read_resource("schema://aircraft")).contents[0].text)


if __name__ == "__main__":
    run(main)
