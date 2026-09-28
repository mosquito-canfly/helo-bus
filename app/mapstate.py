"""Tracks just enough about the most recent successful next_arrivals/
plan_trip answer to drive the live map (/api/map-state).

Geometry is derived on demand from data already in memory — stop/station
coordinates, _route_paths/_rail_paths (stop ID order, not full shape
polylines) — so this adds no real memory over what the ETA engine already
needs, and never re-fetches from data.gov.my: live vehicle positions here
are just a filtered read of gtfs._vehicles, whatever ensure_fresh() (called
elsewhere, by a tool call or the poller) has already cached.

Read-only with respect to gtfs.py: nothing here changes matching or ETA
logic, or gtfs.py's public function signatures — plan_trip's own tool
contract (what the agent sees) is untouched. When plan_trip itself came back
"no buses nearby" (a live-data gap, not a routing failure), this module
independently re-derives the same STRUCTURAL route plan_trip's direct-bus/
rail-inclusive tiers would have found, using the request's own from/to text,
so the map can still draw the trip without inventing an ETA.
"""

from __future__ import annotations

import json

from . import gtfs

_state: dict | None = None

# plan_trip reasons that mean "a real route exists, live data just didn't
# have an ETA for it" — worth drawing on the map anyway. Anything else
# (no_direct_route, same_stop, ambiguous_stop, not_found, no_service_night)
# means there's genuinely nothing to draw.
_LIVE_DATA_GAP_REASONS = {"no_buses_nearby", "feed_unavailable"}


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


def _any_point(display_name: str) -> dict | None:
    """A walk leg's endpoints can be either namespace — a rail station
    (the usual interchange case, e.g. Muzium Negara -> KL Sentral) or a bus
    stop (the final "walk to the destination" case)."""
    return _bus_point(display_name) or _rail_point(display_name)


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


def _walk_leg_points(step: dict) -> list[list[float]]:
    """A straight line — there's no footpath network loaded, just the two
    endpoints (real GTFS distance for these is short, <=500m, so a straight
    line is an honest-enough sketch of a walking leg on the map)."""
    a, b = _any_point(step["board_at"]), _any_point(step["alight_at"])
    if not (a and b):
        return []
    return [[a["lat"], a["lon"]], [b["lat"], b["lon"]]]


def _leg_geometry(step: dict) -> dict:
    if step["mode"] == "bus":
        return {"mode": "bus", "route": step["route"], "category": step.get("category"), "points": _bus_leg_points(step)}
    if step["mode"] == "walk":
        return {"mode": "walk", "points": _walk_leg_points(step)}
    line_id = _rail_line_id(step["line"])
    color = gtfs._rail_routes.get(line_id, {}).get("color") if line_id else None
    return {"mode": "rail", "line": step["line"], "color": color, "points": _rail_leg_points(step)}


def _route_vehicles(route_ids: set[str]) -> list[dict]:
    out = []
    for v in gtfs._vehicles.values():
        if v["route_id"] not in route_ids:
            continue
        route = gtfs._routes.get(v["route_id"], {})
        out.append({"lat": v["lat"], "lon": v["lon"], "route": route.get("short_name", v["route_id"]), "category": route.get("category")})
    return out


def _option_from_steps(steps: list[dict]) -> dict | None:
    """Builds one map-ready option (stops + legs + the vehicles relevant to
    just its own bus legs) from a plan_trip-shaped steps list. None if none
    of the legs could be geometrically resolved."""
    legs, stops = [], []
    for i, step in enumerate(steps):
        geo = _leg_geometry(step)
        if not geo["points"]:
            continue
        legs.append(geo)
        if step["mode"] == "bus":
            board_name, alight_name = step["board_at"], step["alight_at"]
            board_pt, alight_pt = _bus_point(board_name), _bus_point(alight_name)
        elif step["mode"] == "walk":
            board_name, alight_name = step["board_at"], step["alight_at"]
            board_pt, alight_pt = _any_point(board_name), _any_point(alight_name)
        else:
            board_name, alight_name = step["from_station"], step["to_station"]
            board_pt, alight_pt = _rail_point(board_name), _rail_point(alight_name)
        if board_pt:
            stops.append({"name": board_name, "role": "board" if i == 0 else "transfer", **board_pt})
        if alight_pt:
            stops.append({"name": alight_name, "role": "alight" if i == len(steps) - 1 else "transfer", **alight_pt})
    if not legs:
        return None
    bus_route_ids = {gtfs._short_name_to_route_id[s["route"]] for s in steps if s["mode"] == "bus" and s["route"] in gtfs._short_name_to_route_id}
    return {"stops": stops, "legs": legs, "vehicles": _route_vehicles(bus_route_ids)}


def _structural_step_candidates(from_group: gtfs.StopGroup, to_group: gtfs.StopGroup) -> list[list[tuple]]:
    """Mirrors plan_trip's own tiering (direct bus first, then rail-
    inclusive) but never checks live data — used only so the map can still
    draw a trip when the tool call itself came back with no usable ETA."""
    candidates = [
        [("bus", route_id, direction_id, board_stop_id, from_group.name, to_group.name)]
        for route_id, direction_id, board_stop_id in gtfs._direct_route_options(from_group, to_group)
    ]
    if len(candidates) < 2:
        from_rail = gtfs._linked_rail_groups(from_group)
        to_rail = gtfs._linked_rail_groups(to_group)
        candidates += gtfs._rail_inclusive_options(from_group, to_group, from_rail, to_rail)
    return candidates[:2]


def _tuple_to_step(t: tuple) -> dict:
    """A structural (mode, ...) tuple -> the minimal step-shaped dict
    _leg_geometry needs. No eta_human/etc. — this is the no-live-data path."""
    if t[0] == "bus":
        _, route_id, _direction_id, _board_stop_id, board_name, alight_name = t
        route = gtfs._routes.get(route_id, {})
        return {"mode": "bus", "route": route.get("short_name", route_id), "category": route.get("category"), "board_at": board_name, "alight_at": alight_name}
    if t[0] == "walk":
        _, meters, from_name, to_name = t
        return {"mode": "walk", "board_at": from_name, "alight_at": to_name, "meters": meters}
    _, line_id, _direction_id, _board_idx, _alight_idx, from_name, to_name = t
    line = gtfs._rail_routes.get(line_id, {})
    line_name = gtfs._RAIL_PREFIX_RE.sub("", line.get("long_name") or line.get("short_name", line_id))
    return {"mode": "rail", "line": line_name, "from_station": from_name, "to_station": to_name}


def set_from_next_arrivals(result: dict) -> None:
    global _state
    if not (result.get("ok") and result.get("stop")):
        return
    point = _bus_point(result["stop"])
    if not point:
        return
    route_ids = {gtfs._short_name_to_route_id[a["route"]] for a in result.get("arrivals", []) if a["route"] in gtfs._short_name_to_route_id}
    option = {"stops": [{"name": result["stop"], "role": "stop", **point}], "legs": [], "vehicles": _route_vehicles(route_ids)}
    _state = {"kind": "stop", "options": [option]}


def set_from_plan_trip(result: dict, args: dict | None = None) -> None:
    global _state
    options: list[dict] = []

    if result.get("ok") and result.get("options"):
        for opt in result["options"]:
            built = _option_from_steps(opt["steps"])
            if built:
                options.append(built)
    elif result.get("reason") in _LIVE_DATA_GAP_REASONS and args:
        from_group, err1 = gtfs._resolve_group(args.get("from_stop", ""))
        to_group, err2 = gtfs._resolve_group(args.get("to_stop", ""))
        if from_group and to_group and not err1 and not err2:
            for candidate in _structural_step_candidates(from_group, to_group):
                built = _option_from_steps([_tuple_to_step(t) for t in candidate])
                if built:
                    options.append(built)

    if not options:
        return
    _state = {"kind": "trip", "options": options}


def update(path: str, request_body: bytes, response_body: bytes) -> None:
    """Best-effort: a map-state hiccup must never break the tool call."""
    tool = path.rsplit("/", 1)[-1]
    if tool not in ("next_arrivals", "plan_trip"):
        return
    try:
        result = json.loads(response_body or b"{}")
        if tool == "next_arrivals":
            set_from_next_arrivals(result)
        else:
            args = json.loads(request_body or b"{}")
            set_from_plan_trip(result, args)
    except Exception:
        pass


def get() -> dict:
    return _state or {"kind": None, "options": []}
