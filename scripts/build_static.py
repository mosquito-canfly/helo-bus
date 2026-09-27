"""Precompute the compact static-data file the server actually loads.

Render's free tier (512 MB) was getting OOM-killed: parsing both GTFS
categories' raw stop_times.txt (5.5 MB + 14 MB) and shapes.txt into Python
dicts at every cold start was the heavy part, not the derived tables the
tools actually use. This script does that parsing once, offline, and writes
just the derived tables to data/static.json — the server loads that one
file at startup and never downloads or parses a GTFS CSV.

Run after a GTFS refresh: python scripts/build_static.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `app` resolves when run as a script, not a module

from app import gtfs  # noqa: E402

OUT_FILE = ROOT / "data" / "static.json"
RAIL_LINK_METERS = 300.0  # walking-distance threshold for bus stop <-> rail station


def _build_rail_links() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """One representative point per group (its first member) is enough for
    a coarse 300m proximity check — group members are already clustered
    (same named stop/station, different platforms)."""
    bus_to_rail: dict[str, list[str]] = {}
    rail_to_bus: dict[str, list[str]] = {}
    for rail_group in gtfs._rail_groups_by_name.values():
        rail_pt = gtfs._rail_stations[rail_group.station_ids[0]]
        near: list[str] = []
        for bus_group in gtfs._groups_by_name.values():
            bus_pt = gtfs._stops[bus_group.stop_ids[0]]
            dist_m = gtfs._haversine_km(rail_pt["lat"], rail_pt["lon"], bus_pt["lat"], bus_pt["lon"]) * 1000.0
            if dist_m <= RAIL_LINK_METERS or gtfs._normalize(bus_group.name) == gtfs._normalize(rail_group.name):
                near.append(bus_group.name)
        if near:
            rail_to_bus[rail_group.name] = near
            for bus_name in near:
                bus_to_rail.setdefault(bus_name, []).append(rail_group.name)
    return bus_to_rail, rail_to_bus


def main() -> None:
    for category in gtfs.CATEGORIES:
        if not (gtfs._cache_dir(category) / "stops.txt").exists():
            gtfs._download_static(category)
        gtfs._load_stops(category)
        gtfs._load_routes(category)
        shapes = gtfs._load_shapes(category)
        gtfs._build_route_paths_and_distances(category, shapes)

    gtfs._build_stop_index()
    gtfs._build_stop_groups()

    gtfs._load_rail_static()
    gtfs._build_rail_station_groups()
    bus_to_rail, rail_to_bus = _build_rail_links()

    stops = {
        sid: {"name": s["name"], "normalized": gtfs._normalize(s["name"]), "lat": s["lat"], "lon": s["lon"]}
        for sid, s in gtfs._stops.items()
    }
    groups = [
        {"key": key, "name": group.name, "stop_ids": group.stop_ids}
        for key, group in gtfs._groups_by_name.items()
    ]
    rail_stations = {sid: {"name": s["name"], "lat": s["lat"], "lon": s["lon"]} for sid, s in gtfs._rail_stations.items()}
    rail_groups = [
        {"key": key, "name": group.name, "station_ids": group.station_ids}
        for key, group in gtfs._rail_groups_by_name.items()
    ]

    data = {
        "stops": stops,
        "routes": gtfs._routes,
        "short_name_to_route_id": gtfs._short_name_to_route_id,
        "route_paths": gtfs._route_paths,
        "stop_cum_dist": gtfs._stop_cum_dist,
        "route_headsign": gtfs._route_headsign,
        "trip_direction": gtfs._trip_direction,
        "stop_to_routes": gtfs._stop_to_routes,
        "groups": groups,
        "rail": {
            "stations": rail_stations,
            "routes": gtfs._rail_routes,
            "paths": gtfs._rail_paths,
            "headsign": gtfs._rail_headsign,
            "groups": rail_groups,
            "bus_to_rail": bus_to_rail,
            "rail_to_bus": rail_to_bus,
        },
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    size_kb = OUT_FILE.stat().st_size / 1024
    print(f"stops={len(stops)} routes={len(gtfs._routes)} route_paths={len(gtfs._route_paths)} groups={len(groups)}")
    print(f"rail: stations={len(rail_stations)} lines={len(gtfs._rail_routes)} groups={len(rail_groups)} "
          f"linked_bus_groups={len(bus_to_rail)}")
    print(f"wrote {OUT_FILE} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
