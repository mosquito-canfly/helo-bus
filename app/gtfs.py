"""RapidKL transit data layer: static GTFS + live vehicle positions + ETAs.

Static schedule data (stops, routes, shapes) rarely changes and is cached to
disk on first run. Live vehicle positions are polled on demand — whichever
code path needs fresh data calls ensure_fresh(), which refetches only if the
cache is more than LIVE_STALE_SECONDS old.

rapid-bus-kl only (see PLAN.md scope decision). ETA math ports the approach
already validated in ../where-bus's TransitService/LiveTrackingService/
EtaCalculationService: nearest-stop GPS snapping onto a cumulative-distance
table built from the route's shape polyline, not true polyline projection.
"""

from __future__ import annotations

import csv
import difflib
import io
import re
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2

from . import store

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".gtfs_cache" / "rapid-bus-kl"
STATIC_URL = "https://api.data.gov.my/gtfs-static/prasarana?category=rapid-bus-kl"
RT_URL = "https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana?category=rapid-bus-kl"

KL_TZ = ZoneInfo("Asia/Kuala_Lumpur")
SERVICE_START_HOUR = 6
SERVICE_END_HOUR = 23  # last hour of the day RapidKL still runs

LIVE_STALE_SECONDS = 30
ETA_HISTORY_SIZE = 3  # rolling average window, same as where-bus
MAX_ETA_SECONDS = 35 * 60
ARRIVING_METERS = 150.0
HAVERSINE_ROAD_FACTOR = 1.4  # fallback multiplier when no shape data
DEFAULT_SPEED_MPS = 3.0  # ~11 km/h, used when the feed omits speed

# Small fixed dict, not a general normaliser — RapidKL's own stop names use
# these abbreviations inconsistently.
ABBREVIATIONS = {
    "jln": "jalan",
    "tmn": "taman",
    "bdr": "bandar",
    "pers": "persiaran",
    "psn": "persiaran",
    "lrg": "lorong",
    "kg": "kampung",
    "bt": "bukit",
    "sg": "sungai",
}

# Spoken names that don't literally match GTFS text (checked before fuzzy
# match). "KL Sentral", "Pasar Seni" and "Mid Valley" already match the feed
# directly and need no entry here.
ALIASES = {
    "1 utama": "one utama",
}

# Stop names in this feed are frequently prefixed with an internal code
# ("KL1079 KL SENTRAL", "(M) PPJ254 MRT PUTRAJAYA SENTRAL") that has to be
# stripped before two stops with the same real name will group together.
_CODE_PREFIX_RE = re.compile(r"^(\([A-Za-z]+\)\s+)?[A-Z]{1,4}\d{2,6}\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


@dataclass
class StopGroup:
    name: str  # display name, e.g. "KL Sentral"
    stop_ids: list[str] = field(default_factory=list)


_stops: dict[str, dict] = {}  # stop_id -> {name, lat, lon}
_routes: dict[str, dict] = {}  # route_id -> {short_name, long_name}
_short_name_to_route_id: dict[str, str] = {}
_route_paths: dict[str, list[str]] = {}  # "routeId_dir" -> ordered stop_ids
_stop_cum_dist: dict[str, list[float]] = {}  # parallel to _route_paths, km
_route_headsign: dict[str, str] = {}  # "routeId_dir" -> headsign
_stop_to_routes: dict[str, list[tuple[str, int]]] = {}  # stop_id -> [(route_id, dir)]
_trip_direction: dict[str, int] = {}  # trip_id -> direction_id, for the RT feed

_groups_by_name: dict[str, StopGroup] = {}  # normalised name -> group
_loaded = False


def _normalize(text: str) -> str:
    text = _CODE_PREFIX_RE.sub("", text.strip())
    text = _PUNCT_RE.sub(" ", text.lower())
    words = [ABBREVIATIONS.get(w, w) for w in text.split()]
    return " ".join(words)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return r * 2 * atan2(sqrt(a), sqrt(1 - a))


# --------------------------------------------------------------- static load


def _download_static() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    resp = httpx.get(STATIC_URL, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(CACHE_DIR)


def _read_csv(name: str) -> csv.DictReader:
    return csv.DictReader((CACHE_DIR / name).open(encoding="utf-8-sig"))


def _load_stops() -> None:
    for row in _read_csv("stops.txt"):
        _stops[row["stop_id"]] = {
            "name": row["stop_name"].strip(),
            "lat": float(row["stop_lat"]),
            "lon": float(row["stop_lon"]),
        }


def _load_routes() -> None:
    for row in _read_csv("routes.txt"):
        short_name = row["route_short_name"].strip() or row["route_long_name"].strip()
        _routes[row["route_id"]] = {"short_name": short_name, "long_name": row["route_long_name"].strip()}
        _short_name_to_route_id.setdefault(short_name, row["route_id"])


def _load_shapes() -> dict[str, list[tuple[float, float, float]]]:
    """shape_id -> ordered [(lat, lon, cumulative_km), ...]."""
    raw: dict[str, list[tuple[float, float, float]]] = {}
    for row in _read_csv("shapes.txt"):
        raw.setdefault(row["shape_id"], []).append(
            (float(row["shape_pt_sequence"]), float(row["shape_pt_lat"]), float(row["shape_pt_lon"]))
        )

    polylines: dict[str, list[tuple[float, float, float]]] = {}
    for shape_id, points in raw.items():
        points.sort(key=lambda p: p[0])
        polyline: list[tuple[float, float, float]] = []
        cum = 0.0
        for i, (_, lat, lon) in enumerate(points):
            if i > 0:
                plat, plon, _ = polyline[i - 1]
                cum += _haversine_km(plat, plon, lat, lon)
            polyline.append((lat, lon, cum))
        polylines[shape_id] = polyline
    return polylines


def _representative_trips() -> dict[str, tuple[str, int, str]]:
    """trip_id -> (route_id, direction_id, shape_id), one per route-direction."""
    seen: set[str] = set()
    trips: dict[str, tuple[str, int, str]] = {}
    for row in _read_csv("trips.txt"):
        route_id = row["route_id"]
        direction_id = int(row["direction_id"] or 0)
        trip_id = row["trip_id"]
        _trip_direction[trip_id] = direction_id

        path_key = f"{route_id}_{direction_id}"
        if path_key in seen:
            continue
        seen.add(path_key)
        trips[trip_id] = (route_id, direction_id, row["shape_id"])
        _route_paths.setdefault(path_key, [])
        headsign = row["trip_headsign"].strip()
        if headsign:
            _route_headsign[path_key] = headsign
    return trips


def _build_route_paths_and_distances(shapes: dict[str, list[tuple[float, float, float]]]) -> None:
    target_trips = _representative_trips()

    for row in _read_csv("stop_times.txt"):
        trip_id = row["trip_id"]
        if trip_id not in target_trips:
            continue

        route_id, direction_id, _shape_id = target_trips[trip_id]
        path_key = f"{route_id}_{direction_id}"
        path = _route_paths[path_key]
        stop_id = row["stop_id"]
        if not path or path[-1] != stop_id:
            path.append(stop_id)

    for trip_id, (route_id, direction_id, shape_id) in target_trips.items():
        path_key = f"{route_id}_{direction_id}"
        _stop_cum_dist[path_key] = _project_stops_onto_shape(_route_paths[path_key], shapes.get(shape_id))


def _project_stops_onto_shape(
    stop_ids: list[str], polyline: list[tuple[float, float, float]] | None
) -> list[float]:
    if not polyline:
        return list(range(len(stop_ids)))  # index placeholder, still monotonic

    distances = []
    for stop_id in stop_ids:
        stop = _stops.get(stop_id)
        if stop is None:
            distances.append(distances[-1] if distances else 0.0)
            continue
        best = min(polyline, key=lambda p: _haversine_km(stop["lat"], stop["lon"], p[0], p[1]))
        distances.append(best[2])
    return distances


def _build_stop_index() -> None:
    for path_key, stop_ids in _route_paths.items():
        route_id, direction_id = path_key.rsplit("_", 1)
        for stop_id in stop_ids:
            _stop_to_routes.setdefault(stop_id, []).append((route_id, int(direction_id)))


_VOWELS = set("aeiou")


def _cap_word(word: str) -> str:
    """Title-case a word but leave short vowel-less tokens (KL, LRT, SS2) as-is."""
    if word.isalpha() and len(word) <= 4 and not (_VOWELS & set(word.lower())):
        return word
    for i, ch in enumerate(word):
        if ch.isalpha():
            return word[:i] + ch.upper() + word[i + 1 :].lower()
    return word


def _display_name(raw_name: str) -> str:
    stripped = _CODE_PREFIX_RE.sub("", raw_name.strip())
    return " ".join(_cap_word(w) for w in stripped.split(" "))


def _build_stop_groups() -> None:
    for stop_id, stop in _stops.items():
        name = _normalize(stop["name"])
        group = _groups_by_name.setdefault(name, StopGroup(name=_display_name(stop["name"])))
        group.stop_ids.append(stop_id)


def _load_static() -> None:
    global _loaded
    if _loaded:
        return
    if not (CACHE_DIR / "stops.txt").exists():
        _download_static()

    _load_stops()
    _load_routes()
    shapes = _load_shapes()
    _build_route_paths_and_distances(shapes)
    _build_stop_index()
    _build_stop_groups()
    _loaded = True


# ----------------------------------------------------------------- find_stop


def _search_groups(query: str) -> list[StopGroup]:
    normalized = _normalize(query)
    normalized = ALIASES.get(normalized, normalized)

    exact = _groups_by_name.get(normalized)
    if exact:
        return [exact]

    contains = [g for name, g in _groups_by_name.items() if normalized in name or name in normalized]
    if contains:
        contains.sort(key=lambda g: len(g.name))
        return contains[:5]

    close = difflib.get_close_matches(normalized, _groups_by_name.keys(), n=5, cutoff=0.72)
    return [_groups_by_name[n] for n in close]


def find_stop(query: str) -> dict:
    _load_static()
    matches = _search_groups(query)
    if not matches:
        return {"ok": False, "reason": "not_found", "message": f"I couldn't find a stop called '{query}'."}
    if len(matches) == 1:
        group = matches[0]
        return {"ok": True, "stop": group.name, "message": f"Found {group.name}."}
    names = [m.name for m in matches]
    return {
        "ok": True,
        "ambiguous": True,
        "candidates": names,
        "message": "I found a few stops with that name: " + ", ".join(names) + ". Which one did you mean?",
    }


def _resolve_group(stop_text: str) -> tuple[StopGroup | None, dict | None]:
    """Returns (group, None) on a clean match, or (None, error_payload)."""
    matches = _search_groups(stop_text)
    if not matches:
        return None, {"ok": False, "reason": "not_found", "message": f"I couldn't find a stop called '{stop_text}'."}
    if len(matches) > 1:
        names = [m.name for m in matches]
        return None, {
            "ok": False,
            "reason": "ambiguous_stop",
            "candidates": names,
            "message": "There's more than one stop with that name: " + ", ".join(names) + ". Which one?",
        }
    return matches[0], None


def _resolve_route(route_text: str) -> str | None:
    """Route short name -> internal route_id, or None if unrecognised."""
    key = route_text.strip().upper()
    if key in _short_name_to_route_id:
        return _short_name_to_route_id[key]
    close = difflib.get_close_matches(key, _short_name_to_route_id.keys(), n=1, cutoff=0.75)
    return _short_name_to_route_id[close[0]] if close else None


# ------------------------------------------------------------- live polling

_vehicles: dict[str, dict] = {}
_last_fetch = 0.0
_last_fetch_ok = False


def ensure_fresh() -> None:
    global _last_fetch, _last_fetch_ok
    _load_static()
    now = time.time()
    if now - _last_fetch < LIVE_STALE_SECONDS:
        return
    _last_fetch = now
    try:
        resp = httpx.get(RT_URL, timeout=10, follow_redirects=True)
        resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
    except Exception:
        _last_fetch_ok = False
        store.check_alerts(_alert_arrivals)
        return

    _last_fetch_ok = True
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        if not v.HasField("trip") or not v.trip.route_id:
            continue
        trip_id = v.trip.trip_id
        direction_id = v.trip.direction_id if v.trip.HasField("direction_id") else _trip_direction.get(trip_id, 0)
        _vehicles[v.vehicle.id] = {
            "route_id": v.trip.route_id,
            "direction_id": direction_id,
            "lat": v.position.latitude,
            "lon": v.position.longitude,
            "speed_kmh": v.position.speed if v.position.HasField("speed") else None,
        }

    store.check_alerts(_alert_arrivals)


# --------------------------------------------------------------- ETA engine

_eta_history: dict[str, deque[int]] = {}


def _speed_mps(vehicle: dict) -> float:
    kmh = vehicle["speed_kmh"]
    if kmh is not None and 1.0 < kmh < 120.0:
        return kmh / 3.6
    return DEFAULT_SPEED_MPS


def _smooth(vehicle_id: str, stop_id: str, raw_seconds: int) -> int:
    key = f"{vehicle_id}_{stop_id}"
    history = _eta_history.setdefault(key, deque(maxlen=ETA_HISTORY_SIZE))
    history.append(raw_seconds)
    return round(sum(history) / len(history))


def _stop_position(path_key: str, stop_id: str) -> tuple[float, int] | None:
    path = _route_paths.get(path_key)
    distances = _stop_cum_dist.get(path_key)
    if not path or not distances or stop_id not in path:
        return None
    idx = path.index(stop_id)
    return distances[idx], idx


def _project_vehicle(path_key: str, lat: float, lon: float) -> tuple[float, int] | None:
    path = _route_paths.get(path_key)
    distances = _stop_cum_dist.get(path_key)
    if not path or not distances:
        return None
    best_idx, best_dist = None, float("inf")
    for i, stop_id in enumerate(path):
        stop = _stops.get(stop_id)
        if stop is None:
            continue
        d = _haversine_km(lat, lon, stop["lat"], stop["lon"])
        if d < best_dist:
            best_dist, best_idx = d, i
    if best_idx is None:
        return None
    return distances[best_idx], best_idx


def _eta_human(seconds: int, distance_m: float) -> str:
    if distance_m <= ARRIVING_METERS or seconds < 60:
        return "arriving now"
    minutes = seconds // 60
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _arrivals_for(route_id: str, direction_id: int, stop_id: str, route_short_name: str) -> list[dict]:
    path_key = f"{route_id}_{direction_id}"
    target = _stop_position(path_key, stop_id)

    results = []
    for vehicle_id, vehicle in _vehicles.items():
        if vehicle["route_id"] != route_id or vehicle["direction_id"] != direction_id:
            continue

        if target is not None:
            projected = _project_vehicle(path_key, vehicle["lat"], vehicle["lon"])
        else:
            projected = None

        if target is not None and projected is not None:
            bus_dist, _bus_idx = projected
            target_dist, _target_idx = target
            if bus_dist >= target_dist:
                continue  # already passed
            distance_m = (target_dist - bus_dist) * 1000.0
        else:
            stop = _stops.get(stop_id)
            if stop is None:
                continue
            distance_m = _haversine_km(vehicle["lat"], vehicle["lon"], stop["lat"], stop["lon"]) * 1000.0 * HAVERSINE_ROAD_FACTOR

        raw_seconds = int(distance_m / _speed_mps(vehicle))
        smoothed = _smooth(vehicle_id, stop_id, raw_seconds)
        if smoothed > MAX_ETA_SECONDS:
            continue

        results.append(
            {
                "route": route_short_name,
                "eta_seconds": smoothed,
                "eta_human": _eta_human(smoothed, distance_m),
            }
        )

    return results


def _service_window() -> tuple[bool, str]:
    """Returns (within_operating_hours, spoken_next_start)."""
    now = datetime.now(KL_TZ)
    if SERVICE_START_HOUR <= now.hour < SERVICE_END_HOUR:
        return True, ""
    next_start = f"{SERVICE_START_HOUR}:00 am"
    return False, next_start


def next_arrivals(stop_text: str, route_text: str | None = None) -> dict:
    _load_static()
    group, error = _resolve_group(stop_text)
    if error:
        return error

    route_id = None
    if route_text:
        route_id = _resolve_route(route_text)
        if route_id is None:
            return {"ok": False, "reason": "unknown_route", "message": f"I don't recognise the route '{route_text}'."}

    candidates: list[tuple[str, int, str]] = []  # (route_id, direction_id, stop_id)
    directions_seen: set[int] = set()
    for stop_id in group.stop_ids:
        for r_id, direction_id in _stop_to_routes.get(stop_id, []):
            if route_id and r_id != route_id:
                continue
            candidates.append((r_id, direction_id, stop_id))
            if route_id:
                directions_seen.add(direction_id)

    if not candidates:
        return {
            "ok": False,
            "reason": "route_not_at_stop",
            "message": f"{route_text or 'That route'} doesn't seem to serve {group.name}.",
        }

    if route_id and len(directions_seen) > 1:
        path_key0 = f"{route_id}_0"
        path_key1 = f"{route_id}_1"
        towards_0 = _route_headsign.get(path_key0, "one direction")
        towards_1 = _route_headsign.get(path_key1, "the other direction")
        return {
            "ok": False,
            "reason": "direction_ambiguous",
            "message": f"{route_text} passes {group.name} going both ways — towards {towards_0} or towards {towards_1}. Which one?",
        }

    within_hours, next_start = _service_window()
    ensure_fresh()

    if not within_hours and not _vehicles:
        return {
            "ok": False,
            "reason": "no_service_night",
            "message": f"RapidKL isn't running right now. Services start again around {next_start}.",
        }

    if not _last_fetch_ok and not _vehicles:
        return {
            "ok": False,
            "reason": "feed_unavailable",
            "message": "I can't reach live bus positions right now. Try again in a moment.",
        }

    arrivals: list[dict] = []
    for r_id, direction_id, stop_id in candidates:
        short_name = _routes.get(r_id, {}).get("short_name", r_id)
        arrivals.extend(_arrivals_for(r_id, direction_id, stop_id, short_name))

    if not arrivals:
        return {
            "ok": False,
            "reason": "no_buses_nearby",
            "message": f"No buses are currently close enough to {group.name} to give an ETA.",
        }

    arrivals.sort(key=lambda a: a["eta_seconds"])
    arrivals = arrivals[:4]
    spoken = ", ".join(f"Route {a['route']} in {a['eta_human']}" for a in arrivals)
    return {"ok": True, "stop": group.name, "arrivals": arrivals, "message": f"At {group.name}: {spoken}."}


# ------------------------------------------------------------------- alerts


def _alert_arrivals(stop_ids: list[str], route_id: str) -> list[dict]:
    """Used by store.check_alerts: minimum-ETA arrivals for one alert's route/stops."""
    arrivals: list[dict] = []
    for stop_id in stop_ids:
        for r_id, direction_id in _stop_to_routes.get(stop_id, []):
            if r_id != route_id:
                continue
            short_name = _routes.get(r_id, {}).get("short_name", r_id)
            arrivals.extend(_arrivals_for(r_id, direction_id, stop_id, short_name))
    return arrivals


def set_arrival_alert(stop_text: str, route_text: str, threshold_minutes: int) -> dict:
    _load_static()
    group, error = _resolve_group(stop_text)
    if error:
        return error

    route_id = _resolve_route(route_text)
    if route_id is None:
        return {"ok": False, "reason": "unknown_route", "message": f"I don't recognise the route '{route_text}'."}

    served = any(r_id == route_id for stop_id in group.stop_ids for r_id, _ in _stop_to_routes.get(stop_id, []))
    if not served:
        return {
            "ok": False,
            "reason": "route_not_at_stop",
            "message": f"{route_text} doesn't seem to serve {group.name}.",
        }

    short_name = _routes.get(route_id, {}).get("short_name", route_id)
    store.add_alert(group.name, group.stop_ids, route_id, short_name, threshold_minutes)
    return {
        "ok": True,
        "message": f"Got it — I'll let you know when Route {short_name} is about {threshold_minutes} minutes from {group.name}.",
    }


# ---------------------------------------------------------------- demo info


def network_summary() -> dict:
    _load_static()
    return {
        "stops": len(_stops),
        "routes": len(_routes),
        "active_vehicles": len(_vehicles),
        "service_hours": f"{SERVICE_START_HOUR}:00 am to {SERVICE_END_HOUR - 12}:00 pm",
    }


# --------------------------------------------------------------------- time


def get_now() -> dict:
    now = datetime.now(KL_TZ)
    return {
        "ok": True,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
        "message": f"It's {now.strftime('%A')}, {now.strftime('%I:%M %p').lstrip('0')} in Kuala Lumpur.",
    }
