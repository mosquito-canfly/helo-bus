"""Tracks just enough about the most recent successful next_arrivals/
plan_trip answer to drive the live map (/api/map-state).

Geometry is derived on demand from data already in memory — stop/station
coordinates, _route_paths/_rail_paths (stop ID order, not full shape
polylines) — so this adds no real memory over what the ETA engine already
needs, and never re-fetches from data.gov.my: live vehicle positions here
are just a filtered read of gtfs._vehicles, whatever ensure_fresh() (called
elsewhere, by a tool call or the poller) has already cached.

Read-only with respect to gtfs.py: nothing here changes matching or ETA
logic, or gtfs.py's public function signatures.
"""

from __future__ import annotations

import json

from . import gtfs

_state: dict | None = None


def _bus_point(display_name: str) -> dict | None:
    group = gtfs._bus_group_by_display_name.get(display_name)
    if not group:
        return None
    s = gtfs._stops[group.stop_ids[0]]
    return {"lat": s["lat"], "lon": s["lon"]}


def _rail_point(display_name: str) -> dict | None:
    group = gtfs._rail_group_by_display_name.get(display_name)
    if not group:
        return None
    s = gtfs._rail_stations[group.station_ids[0]]
    return {"lat": s["lat"], "lon": s["lon"]}


def _rail_line_id(display_name: str) -> str | None:
    for line_id, r in gtfs._rail_routes.items():
        name = gtfs._RAIL_PREFIX_RE.sub("", r.get("long_name") or r.get("short_name", line_id))
        if name == display_name:
            return line_id
    return None


def _bus_leg_points(step: dict) -> list[list[float]]:
    board_group = gtfs._bus_group_by_display_name.get(step["board_at"])
    alight_group = gtfs._bus_group_by_display_name.get(step["alight_at"])
    route_id = gtfs._short_name_to_route_id.get(step["route"])
    if not (board_group and alight_group and route_id):
        return []
    alight_ids = set(alight_group.stop_ids)
    for r_id, direction_id, board_stop_id in gtfs._direct_route_options(board_group, alight_group):
        if r_id != route_id:
            continue
        path = gtfs._route_paths[f"{r_id}_{direction_id}"]
        board_idx = path.index(board_stop_id)
        for j in range(board_idx, len(path)):
            if path[j] in alight_ids:
                return [[gtfs._stops[sid]["lat"], gtfs._stops[sid]["lon"]] for sid in path[board_idx : j + 1]]
        break
    return []


def _rail_leg_points(step: dict) -> list[list[float]]:
    from_group = gtfs._rail_group_by_display_name.get(step["from_station"])
    to_group = gtfs._rail_group_by_display_name.get(step["to_station"])
    line_id = _rail_line_id(step["line"])
    if not (from_group and to_group and line_id):
        return []
    for l_id, direction_id, board_idx, alight_idx in gtfs._rail_direct_options(from_group, to_group):
        if l_id != line_id:
            continue
        path = gtfs._rail_paths[f"{l_id}_{direction_id}"]
        return [[gtfs._rail_stations[sid]["lat"], gtfs._rail_stations[sid]["lon"]] for sid in path[board_idx : alight_idx + 1]]
    return []


def _leg_geometry(step: dict) -> dict:
    if step["mode"] == "bus":
        return {"mode": "bus", "route": step["route"], "category": step.get("category"), "points": _bus_leg_points(step)}
    return {"mode": "rail", "line": step["line"], "points": _rail_leg_points(step)}


def _route_vehicles(route_ids: set[str]) -> list[dict]:
    out = []
    for v in gtfs._vehicles.values():
        if v["route_id"] not in route_ids:
            continue
        route = gtfs._routes.get(v["route_id"], {})
        out.append({"lat": v["lat"], "lon": v["lon"], "route": route.get("short_name", v["route_id"]), "category": route.get("category")})
    return out


def set_from_next_arrivals(result: dict) -> None:
    global _state
    if not (result.get("ok") and result.get("stop")):
        return
    point = _bus_point(result["stop"])
    if not point:
        return
    route_ids = {gtfs._short_name_to_route_id[a["route"]] for a in result.get("arrivals", []) if a["route"] in gtfs._short_name_to_route_id}
    _state = {
        "kind": "stop",
        "stops": [{"name": result["stop"], "role": "stop", **point}],
        "legs": [],
        "vehicles": _route_vehicles(route_ids),
    }


def set_from_plan_trip(result: dict) -> None:
    global _state
    if not (result.get("ok") and result.get("options")):
        return
    steps = result["options"][0]["steps"]

    legs, stops = [], []
    for i, step in enumerate(steps):
        geo = _leg_geometry(step)
        if not geo["points"]:
            continue
        legs.append(geo)
        if step["mode"] == "bus":
            board_name, alight_name, board_pt, alight_pt = step["board_at"], step["alight_at"], _bus_point(step["board_at"]), _bus_point(step["alight_at"])
        else:
            board_name, alight_name, board_pt, alight_pt = step["from_station"], step["to_station"], _rail_point(step["from_station"]), _rail_point(step["to_station"])
        if board_pt:
            stops.append({"name": board_name, "role": "board" if i == 0 else "transfer", **board_pt})
        if alight_pt:
            stops.append({"name": alight_name, "role": "alight" if i == len(steps) - 1 else "transfer", **alight_pt})

    bus_route_ids = {gtfs._short_name_to_route_id[s["route"]] for s in steps if s["mode"] == "bus" and s["route"] in gtfs._short_name_to_route_id}
    _state = {"kind": "trip", "stops": stops, "legs": legs, "vehicles": _route_vehicles(bus_route_ids)}


def update(path: str, response_body: bytes) -> None:
    """Best-effort: a map-state hiccup must never break the tool call."""
    tool = path.rsplit("/", 1)[-1]
    if tool not in ("next_arrivals", "plan_trip"):
        return
    try:
        result = json.loads(response_body or b"{}")
        (set_from_next_arrivals if tool == "next_arrivals" else set_from_plan_trip)(result)
    except Exception:
        pass


def get() -> dict:
    return _state or {"kind": None, "stops": [], "legs": [], "vehicles": []}
