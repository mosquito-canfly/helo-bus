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

    stops = {
        sid: {"name": s["name"], "normalized": gtfs._normalize(s["name"]), "lat": s["lat"], "lon": s["lon"]}
        for sid, s in gtfs._stops.items()
    }
    groups = [
        {"key": key, "name": group.name, "stop_ids": group.stop_ids}
        for key, group in gtfs._groups_by_name.items()
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
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    size_kb = OUT_FILE.stat().st_size / 1024
    print(f"stops={len(stops)} routes={len(gtfs._routes)} route_paths={len(gtfs._route_paths)} groups={len(groups)}")
    print(f"wrote {OUT_FILE} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
