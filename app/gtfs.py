"""RapidKL transit data layer: static GTFS + live vehicle positions + ETAs.

Static schedule data (stops, routes, shapes) rarely changes and is cached to
disk on first run. Live vehicle positions are polled on demand — whichever
code path needs fresh data calls ensure_fresh(), which refetches only if the
cache is more than LIVE_STALE_SECONDS old.

Two Prasarana categories: rapid-bus-kl and rapid-bus-mrtfeeder, loaded into
the same combined tables (last-category-wins on a colliding stop/route id,
same as where-bus). ETA math ports the approach already validated in
../where-bus's TransitService/LiveTrackingService/EtaCalculationService:
nearest-stop GPS snapping onto a cumulative-distance table built from the
route's shape polyline, not true polyline projection — except where the feed
itself provides shape_dist_traveled (mrtfeeder does, kl doesn't), which is
used directly instead of being re-derived.
"""

from __future__ import annotations

import csv
import difflib
import io
import json
import logging
import re
import threading
import time
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2

log = logging.getLogger("helo_buskl.gtfs")

from . import store

ROOT = Path(__file__).resolve().parent.parent
ALIASES_FILE = ROOT / "data" / "aliases.json"
STATIC_FILE = ROOT / "data" / "static.json"
CATEGORIES = ["rapid-bus-kl", "rapid-bus-mrtfeeder"]
RAIL_CATEGORY = "rapid-rail-kl"  # LRT/MRT/Monorail/BRT — static only, no realtime feed here
STATIC_URL_TMPL = "https://api.data.gov.my/gtfs-static/prasarana?category={category}"
RT_URL_TMPL = "https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana?category={category}"


def _cache_dir(category: str) -> Path:
    return ROOT / ".gtfs_cache" / category


KL_TZ = ZoneInfo("Asia/Kuala_Lumpur")
SERVICE_START_HOUR = 6
SERVICE_END_HOUR = 23  # last hour of the day RapidKL still runs

# data.gov.my allows 4 requests/minute total, shared across both realtime
# feeds (and anything else on the same network hitting their API). Two
# feeds fetched together every LIVE_STALE_SECONDS must stay under that, with
# margin — this app has no scheduler thread, so "every 45s" only happens
# while something is actively polling /api/events.
LIVE_STALE_SECONDS = 45
RT_BACKOFF_SECONDS = 90  # default skip window after a feed 429s
GTFS_RT_TIMEOUT = 3.0  # a tool call has a ~4s total budget; this must fit under it
ETA_HISTORY_SIZE = 3  # rolling average window, same as where-bus
MAX_ETA_SECONDS = 35 * 60
ARRIVING_METERS = 150.0
HAVERSINE_ROAD_FACTOR = 1.4  # fallback multiplier when no shape data
DEFAULT_SPEED_MPS = 3.0  # ~11 km/h, used when the feed omits speed
MIN_GUESS_SCORE = 0.5  # below this, a "did you mean" guess is worse than none

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
    # English word -> the Malay word GTFS actually uses, so a caller saying
    # "KL Central" or "National Mosque" still word-matches "KL Sentral" /
    # "Masjid Negara" without a per-place alias entry.
    "central": "sentral",
    "station": "stesen",
    "market": "pasar",
    "tower": "menara",
    "garden": "taman",
    "gardens": "taman",
    "lake": "tasik",
    "mosque": "masjid",
    "temple": "kuil",
    "museum": "muzium",
    "palace": "istana",
    "university": "universiti",
    "uni": "universiti",
    "school": "sekolah",
    "field": "padang",
    "road": "jalan",
    "street": "jalan",
    "bridge": "jambatan",
    "national": "negara",
    "faculty": "fakulti",
    "computer": "komputer",
    "science": "sains",
    "engineering": "kejuruteraan",
    "new": "baru",
    "old": "lama",
}

# Spoken variants that don't literally match GTFS text and aren't a simple
# word swap — abbreviations, landmark nicknames, informal names. Loaded from
# data/aliases.json (a human-editable seed list) rather than hardcoded here.
ALIASES: dict[str, str] = {}

# Stop names in this feed are frequently prefixed with an internal code
# ("KL1079 KL SENTRAL", "(M) PPJ254 MRT PUTRAJAYA SENTRAL") that has to be
# stripped before two stops with the same real name will group together.
_CODE_PREFIX_RE = re.compile(r"^(\([A-Za-z]+\)\s+)?[A-Z]{1,4}\d{2,6}\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


@dataclass
class StopGroup:
    name: str  # display name, e.g. "KL Sentral"
    stop_ids: list[str] = field(default_factory=list)


@dataclass
class RailStationGroup:
    name: str  # display name, e.g. "Masjid Jamek"
    station_ids: list[str] = field(default_factory=list)  # one per line that serves it


_stops: dict[str, dict] = {}  # stop_id -> {name, lat, lon}
_routes: dict[str, dict] = {}  # route_id -> {short_name, long_name}
_short_name_to_route_id: dict[str, str] = {}
_route_paths: dict[str, list[str]] = {}  # "routeId_dir" -> ordered stop_ids
_stop_cum_dist: dict[str, list[float]] = {}  # parallel to _route_paths, km
_route_headsign: dict[str, str] = {}  # "routeId_dir" -> headsign
_stop_to_routes: dict[str, list[tuple[str, int]]] = {}  # stop_id -> [(route_id, dir)]
_trip_direction: dict[str, int] = {}  # trip_id -> direction_id, for the RT feed

_groups_by_name: dict[str, StopGroup] = {}  # normalised name -> group
_word_to_groups: dict[str, set[str]] = {}  # normalised word -> {group keys}
_word_soundex: dict[str, str] = {}  # normalised word -> soundex code
_soundex_to_words: dict[str, set[str]] = {}  # soundex code -> {words}

# Rail (LRT/MRT/Monorail/BRT): static only, kept in its own namespace so it
# never touches the bus vehicle-tracking path — there's no realtime feed for
# it here, so plan_trip only ever reports a rail leg's line/stops, never an
# ETA (see DESIGN.md-adjacent note in plan_trip itself).
_rail_stations: dict[str, dict] = {}  # station_id -> {name, lat, lon}
_rail_routes: dict[str, dict] = {}  # line_id -> {short_name, long_name}
_rail_paths: dict[str, list[str]] = {}  # "lineId_dir" -> ordered station_ids
_rail_headsign: dict[str, str] = {}  # "lineId_dir" -> headsign, e.g. "From Ampang to Sentul Timur"
_rail_groups_by_name: dict[str, RailStationGroup] = {}  # normalised name -> group
_bus_to_rail: dict[str, list[str]] = {}  # bus StopGroup.name -> [nearby RailStationGroup.name]
_rail_to_bus: dict[str, list[str]] = {}  # rail StationGroup.name -> [nearby bus StopGroup.name]
_bus_group_by_display_name: dict[str, StopGroup] = {}  # StopGroup.name -> group, built at load time
_rail_group_by_display_name: dict[str, RailStationGroup] = {}  # RailStationGroup.name -> group

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


_SOUNDEX_CODES = {
    **dict.fromkeys("bfpv", "1"),
    **dict.fromkeys("cgjkqsxz", "2"),
    **dict.fromkeys("dt", "3"),
    "l": "4",
    **dict.fromkeys("mn", "5"),
    "r": "6",
}


def _soundex(word: str) -> str:
    """Classic Soundex: catches mishearings like STT swapping similar
    consonants ("Damansaraa"/"Tamansara") without an external dependency."""
    word = word.lower()
    if not word:
        return ""
    codes = [_SOUNDEX_CODES.get(ch, "") for ch in word]
    out = word[0].upper()
    prev = codes[0]
    for code in codes[1:]:
        if code and code != prev:
            out += code
        prev = code
    return (out + "000")[:4]


# --------------------------------------------------------------- static load
#
# Everything from here to _build_route_paths_and_distances is build-time
# only: raw GTFS CSV parsing, called by scripts/build_static.py, never by
# the running server. Parsing both categories' stop_times.txt (5.5 MB +
# 14 MB) and shapes.txt at every cold start was what was pushing Render's
# free tier (512 MB) into an OOM kill. The server's own _load_static() below
# just reads the compact data/static.json that script produces.


def _download_static(category: str) -> None:
    cache_dir = _cache_dir(category)
    cache_dir.mkdir(parents=True, exist_ok=True)
    resp = httpx.get(STATIC_URL_TMPL.format(category=category), timeout=30, follow_redirects=True)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(cache_dir)


def _read_csv(category: str, name: str) -> list[dict]:
    with (_cache_dir(category) / name).open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _load_stops(category: str) -> None:
    for row in _read_csv(category, "stops.txt"):
        _stops[row["stop_id"]] = {
            "name": row["stop_name"].strip(),
            "lat": float(row["stop_lat"]),
            "lon": float(row["stop_lon"]),
        }


def _load_routes(category: str) -> None:
    for row in _read_csv(category, "routes.txt"):
        short_name = row["route_short_name"].strip() or row["route_long_name"].strip()
        _routes[row["route_id"]] = {
            "short_name": short_name,
            "long_name": row["route_long_name"].strip(),
            "category": category,
        }
        _short_name_to_route_id.setdefault(short_name, row["route_id"])


def _load_shapes(category: str) -> dict[str, list[tuple[float, float, float]]]:
    """shape_id -> ordered [(lat, lon, cumulative_km), ...].

    Uses the feed's own shape_dist_traveled when present (mrtfeeder) instead
    of re-deriving it from point-to-point haversine (kl, which omits it).
    """
    raw: dict[str, list[tuple[float, float, float, float]]] = {}
    for row in _read_csv(category, "shapes.txt"):
        dist = row.get("shape_dist_traveled", "").strip()
        raw.setdefault(row["shape_id"], []).append((
            float(row["shape_pt_sequence"]),
            float(row["shape_pt_lat"]),
            float(row["shape_pt_lon"]),
            float(dist) if dist else -1.0,
        ))

    polylines: dict[str, list[tuple[float, float, float]]] = {}
    for shape_id, points in raw.items():
        points.sort(key=lambda p: p[0])
        polyline: list[tuple[float, float, float]] = []
        cum = 0.0
        for i, (_, lat, lon, dist) in enumerate(points):
            if dist >= 0:
                cum = dist
            elif i > 0:
                plat, plon, _ = polyline[i - 1]
                cum += _haversine_km(plat, plon, lat, lon)
            polyline.append((lat, lon, cum))
        polylines[shape_id] = polyline
    return polylines


def _representative_trips(category: str) -> dict[str, tuple[str, int, str]]:
    """trip_id -> (route_id, direction_id, shape_id), one per route-direction."""
    seen: set[str] = set()
    trips: dict[str, tuple[str, int, str]] = {}
    for row in _read_csv(category, "trips.txt"):
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


def _build_route_paths_and_distances(category: str, shapes: dict[str, list[tuple[float, float, float]]]) -> None:
    target_trips = _representative_trips(category)

    # Per-stop shape_dist_traveled, when stop_times.txt itself provides it
    # (mrtfeeder) — more accurate than snapping to the nearest shape point.
    stop_time_dist: dict[str, list[float]] = {}

    for row in _read_csv(category, "stop_times.txt"):
        trip_id = row["trip_id"]
        if trip_id not in target_trips:
            continue

        route_id, direction_id, _shape_id = target_trips[trip_id]
        path_key = f"{route_id}_{direction_id}"
        path = _route_paths[path_key]
        stop_id = row["stop_id"]
        if not path or path[-1] != stop_id:
            path.append(stop_id)
            dist = row.get("shape_dist_traveled", "").strip()
            if dist:
                stop_time_dist.setdefault(path_key, []).append(float(dist))

    for trip_id, (route_id, direction_id, shape_id) in target_trips.items():
        path_key = f"{route_id}_{direction_id}"
        path = _route_paths[path_key]
        distances = stop_time_dist.get(path_key)
        if distances and len(distances) == len(path):
            _stop_cum_dist[path_key] = distances
        else:
            _stop_cum_dist[path_key] = _project_stops_onto_shape(path, shapes.get(shape_id))


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


def _load_rail_static() -> None:
    """Build-time only, mirrors the bus loaders above but far simpler: rail
    stop_times.txt has no shape_dist_traveled, and a rail leg never needs a
    distance/ETA (no realtime feed) — only stop ORDER, which stop_sequence
    already gives for free. So this skips shapes.txt entirely."""
    if not (_cache_dir(RAIL_CATEGORY) / "stops.txt").exists():
        _download_static(RAIL_CATEGORY)

    for row in _read_csv(RAIL_CATEGORY, "stops.txt"):
        _rail_stations[row["stop_id"]] = {
            "name": row["stop_name"].strip(),
            "lat": float(row["stop_lat"]),
            "lon": float(row["stop_lon"]),
        }
    for row in _read_csv(RAIL_CATEGORY, "routes.txt"):
        _rail_routes[row["route_id"]] = {
            "short_name": row["route_short_name"].strip(),
            "long_name": row["route_long_name"].strip(),
        }

    target_trips: dict[str, tuple[str, int]] = {}  # trip_id -> (route_id, direction_id)
    seen_paths: set[str] = set()
    for row in _read_csv(RAIL_CATEGORY, "trips.txt"):
        route_id = row["route_id"]
        direction_id = int(row["direction_id"] or 0)
        path_key = f"{route_id}_{direction_id}"
        headsign = row["trip_headsign"].strip()
        if headsign:
            _rail_headsign.setdefault(path_key, headsign)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        target_trips[row["trip_id"]] = (route_id, direction_id)

    stop_times: dict[str, list[tuple[int, str]]] = {}  # path_key -> [(sequence, stop_id)]
    for row in _read_csv(RAIL_CATEGORY, "stop_times.txt"):
        trip_id = row["trip_id"]
        if trip_id not in target_trips:
            continue
        route_id, direction_id = target_trips[trip_id]
        stop_times.setdefault(f"{route_id}_{direction_id}", []).append((int(row["stop_sequence"]), row["stop_id"]))

    for path_key, rows in stop_times.items():
        rows.sort(key=lambda r: r[0])
        _rail_paths[path_key] = [stop_id for _, stop_id in rows]


def _build_rail_station_groups() -> None:
    for station_id, s in _rail_stations.items():
        name = _normalize(s["name"])
        group = _rail_groups_by_name.setdefault(name, RailStationGroup(name=_display_name(s["name"])))
        group.station_ids.append(station_id)


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


def _build_word_index() -> None:
    for group_key in _groups_by_name:
        for word in group_key.split():
            _word_to_groups.setdefault(word, set()).add(group_key)
    for word in _word_to_groups:
        code = _soundex(word)
        _word_soundex[word] = code
        _soundex_to_words.setdefault(code, set()).add(word)


def _load_aliases() -> None:
    if ALIASES_FILE.exists():
        ALIASES.update(json.loads(ALIASES_FILE.read_text(encoding="utf-8")))


def _load_static() -> None:
    """Runtime load: read the compact precomputed file, no CSV parsing, no
    GTFS download. Run scripts/build_static.py to (re)generate it."""
    global _loaded
    if _loaded:
        return
    if not STATIC_FILE.exists():
        raise RuntimeError(f"{STATIC_FILE} is missing — run scripts/build_static.py first")

    data = json.loads(STATIC_FILE.read_text(encoding="utf-8"))
    for stop_id, s in data["stops"].items():
        _stops[stop_id] = {"name": s["name"], "lat": s["lat"], "lon": s["lon"]}
    _routes.update(data["routes"])
    _short_name_to_route_id.update(data["short_name_to_route_id"])
    _route_paths.update(data["route_paths"])
    _stop_cum_dist.update(data["stop_cum_dist"])
    _route_headsign.update(data["route_headsign"])
    _trip_direction.update(data["trip_direction"])
    for stop_id, pairs in data["stop_to_routes"].items():
        _stop_to_routes[stop_id] = [tuple(pair) for pair in pairs]
    for group in data["groups"]:
        _groups_by_name[group["key"]] = StopGroup(name=group["name"], stop_ids=group["stop_ids"])

    rail = data.get("rail", {})
    for station_id, s in rail.get("stations", {}).items():
        _rail_stations[station_id] = {"name": s["name"], "lat": s["lat"], "lon": s["lon"]}
    _rail_routes.update(rail.get("routes", {}))
    _rail_paths.update(rail.get("paths", {}))
    _rail_headsign.update(rail.get("headsign", {}))
    for group in rail.get("groups", []):
        _rail_groups_by_name[group["key"]] = RailStationGroup(name=group["name"], station_ids=group["station_ids"])
    _bus_to_rail.update(rail.get("bus_to_rail", {}))
    _rail_to_bus.update(rail.get("rail_to_bus", {}))
    _bus_group_by_display_name.update({g.name: g for g in _groups_by_name.values()})
    _rail_group_by_display_name.update({g.name: g for g in _rail_groups_by_name.values()})

    _build_word_index()
    _load_aliases()
    _loaded = True


# ----------------------------------------------------------------- find_stop


def _speak_list(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" or {items[-1]}"


# Connector words dropped before matching — not because they're rare, but
# because a couple of them are, coincidentally, real substrings of a few
# stop names ("Commission OF India", "Universiti OF Cyberjaya"). Treating
# "of" as a "real match" for "Faculty OF Computer Science" then blocked the
# intersection down to nothing and fell back to a noisy union across every
# meaning of every word instead of the one stop that has all three.
_STOPWORDS = {"of", "the", "a", "an", "at", "in", "near", "is", "to"}


def _word_candidates(normalized_query: str) -> list[StopGroup]:
    """Order-independent word match.

    A word actually present in some group name is trusted outright. A word
    that ISN'T is only ever expanded via Soundex/close-spelling when it's
    the sole word in the query — with other real words alongside it, a
    stray Soundex coincidence (e.g. "Malaya" sound-alikes to "Mall"/"Mila")
    must never mix noise groups in next to a real match, so multi-word
    queries use only the words that hit exactly, never the fuzzy fallback.
    """
    words = [w for w in normalized_query.split() if w not in _STOPWORDS]
    if not words:
        return []

    exact_sets = [_word_to_groups.get(w, set()) for w in words]
    recognized = sum(1 for s in exact_sets if s)

    if recognized > 0:
        if recognized <= len(words) / 2:
            return []  # most of the query is noise; let a lower-confidence tier handle it
        # Intersect whichever words DID match something real (ignoring an
        # unmatched stopword like "of" rather than letting it block the
        # intersection entirely) — falls back to the union only when even
        # that narrower intersection is empty.
        non_empty = [s for s in exact_sets if s]
        all_match = set.intersection(*non_empty)
        keys = all_match or set.union(*non_empty)
        return [_groups_by_name[k] for k in keys]

    # Nothing matched a real word at all. Only worth a Soundex/spelling
    # guess when the query is a single word — there's no real match here
    # for a coincidental fuzzy hit to contaminate.
    if len(words) != 1 or len(words[0]) < 4:
        return []
    word = words[0]
    candidates = set(_soundex_to_words.get(_soundex(word), set()))
    candidates |= set(difflib.get_close_matches(word, _word_to_groups.keys(), n=3, cutoff=0.84))
    groups: set[str] = set()
    for cand in candidates:
        groups |= _word_to_groups.get(cand, set())
    return [_groups_by_name[k] for k in groups]


def _search_groups(query: str) -> tuple[list[StopGroup], bool]:
    """Returns (matches, confident). confident=False means these are a
    best-effort guess (the caller heard something, we're not sure what)."""
    normalized = _normalize(query)
    normalized = ALIASES.get(normalized, normalized)

    exact = _groups_by_name.get(normalized)
    if exact:
        return [exact], True

    contains = [g for name, g in _groups_by_name.items() if normalized in name or name in normalized]
    if contains:
        contains.sort(key=lambda g: len(g.name))
        return contains[:5], True

    word_matches = _word_candidates(normalized)
    if word_matches:
        word_matches.sort(key=lambda g: len(g.name))
        return word_matches[:5], True

    # Confident tier, so the bar is high — 0.72 let scrambled multi-word
    # noise ("fidudaman sara") slip through at ~0.74 and get reported as a
    # sure match instead of a guess. Real typos score .95+, well clear of 0.8.
    close = difflib.get_close_matches(normalized, _groups_by_name.keys(), n=5, cutoff=0.8)
    if close:
        return [_groups_by_name[n] for n in close], True

    # Last resort: a best-effort top-3 guess, marked low-confidence so
    # callers phrase this as "did you mean" not "found" — but only above
    # MIN_GUESS_SCORE. Below that, a guess is worse than no guess: dishing
    # out unrelated stop names reads as confident nonsense. An empty list
    # here is the signal to offer find_nearby_stops instead of guessing.
    guess = difflib.get_close_matches(normalized, _groups_by_name.keys(), n=3, cutoff=MIN_GUESS_SCORE)
    return [_groups_by_name[n] for n in guess], False


def find_stop(query: str) -> dict:
    _load_static()
    matches, confident = _search_groups(query)
    if not confident:
        if not matches:
            return {
                "ok": False,
                "reason": "not_found",
                "message": "I couldn't find a stop by that name. Want me to check what's nearby instead?",
            }
        names = [m.name for m in matches]
        return {
            "ok": False,
            "reason": "not_found",
            "candidates": names,
            "message": f"I didn't catch a stop by that name. Did you mean {_speak_list(names)}?",
        }
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
    matches, confident = _search_groups(stop_text)
    if not confident:
        if not matches:
            return None, {
                "ok": False,
                "reason": "not_found",
                "message": "I couldn't find a stop by that name. Want me to check what's nearby instead?",
            }
        names = [m.name for m in matches]
        return None, {
            "ok": False,
            "reason": "not_found",
            "candidates": names,
            "message": f"I didn't catch a stop by that name. Did you mean {_speak_list(names)}?",
        }
    if len(matches) > 1:
        names = [m.name for m in matches]
        return None, {
            "ok": False,
            "reason": "ambiguous_stop",
            "candidates": names,
            "message": "There's more than one stop with that name: " + ", ".join(names) + ". Which one?",
        }
    return matches[0], None


# Spoken letters/digits STT sometimes spells out instead of transcribing as
# the compact route code ("tea eight one five" rather than "T815").
_SPOKEN_LETTERS = {
    "tea": "t", "bee": "b", "cee": "c", "see": "c", "dee": "d", "eff": "f",
    "gee": "g", "aitch": "h", "jay": "j", "kay": "k", "el": "l", "em": "m",
    "en": "n", "pee": "p", "cue": "q", "are": "r", "ar": "r", "es": "s",
    "you": "u", "vee": "v", "double-u": "w", "dub": "w", "ex": "x",
    "why": "y", "zed": "z", "zee": "z", "oh": "0",
}
_SPOKEN_DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}


def _despoken_route_code(text: str) -> str:
    """"tea eight one five" -> "T815"; "T 815" -> "T815"; leaves an
    already-compact code like "T815" unchanged."""
    words = _PUNCT_RE.sub(" ", text.lower()).split()
    out = []
    for word in words:
        if word in _SPOKEN_LETTERS:
            out.append(_SPOKEN_LETTERS[word])
        elif word in _SPOKEN_DIGITS:
            out.append(_SPOKEN_DIGITS[word])
        else:
            out.append(word)
    return "".join(out).upper()


def _resolve_route(route_text: str) -> str | None:
    """Route short name -> internal route_id, or None if unrecognised."""
    key = route_text.strip().upper()
    if key in _short_name_to_route_id:
        return _short_name_to_route_id[key]

    despoken = _despoken_route_code(route_text)
    if despoken in _short_name_to_route_id:
        return _short_name_to_route_id[despoken]

    close = difflib.get_close_matches(key, _short_name_to_route_id.keys(), n=1, cutoff=0.75)
    return _short_name_to_route_id[close[0]] if close else None


# ------------------------------------------------------------- live polling

_vehicles: dict[str, dict] = {}
_last_fetch: dict[str, float] = {}  # category -> epoch seconds of its last fetch attempt
_rt_backoff_until: dict[str, float] = {}  # category -> epoch seconds to skip until
_category_ok: dict[str, bool] = {}  # category -> did its most recent fetch attempt succeed
_fetch_lock = threading.Lock()


def _resolve_broadcast_route_id(broadcast: str) -> str | None:
    """The two feeds broadcast route_id differently: rapid-bus-kl sends the
    internal GTFS route_id directly; rapid-bus-mrtfeeder sends the public
    short name, sometimes with a direction word appended ("T155 Outbound").
    Try all three readings rather than branch on which feed a vehicle came
    from — either interpretation that resolves is correct."""
    if broadcast in _routes:
        return broadcast
    if broadcast in _short_name_to_route_id:
        return _short_name_to_route_id[broadcast]
    first_word = broadcast.split(" ", 1)[0]
    return _short_name_to_route_id.get(first_word)


def _fetch_vehicles(category: str) -> bool:
    """Returns True if this feed's vehicles were refreshed (fetched or
    skipped-but-still-in-backoff-with-no-data-needed isn't success — only an
    actual 200 counts)."""
    url = RT_URL_TMPL.format(category=category)
    start = time.time()
    try:
        resp = httpx.get(url, timeout=GTFS_RT_TIMEOUT, follow_redirects=True)
    except Exception as exc:
        log.info("rt fetch %s: FAILED after %.2fs (%s)", category, time.time() - start, exc)
        return False
    log.info("rt fetch %s: status=%s in %.2fs", category, resp.status_code, time.time() - start)

    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        try:
            backoff = float(retry_after) if retry_after else RT_BACKOFF_SECONDS
        except ValueError:
            backoff = RT_BACKOFF_SECONDS
        _rt_backoff_until[category] = time.time() + backoff
        return False
    if resp.status_code >= 400:
        return False

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        if not v.HasField("trip") or not v.trip.route_id:
            continue
        route_id = _resolve_broadcast_route_id(v.trip.route_id)
        if route_id is None:
            continue
        trip_id = v.trip.trip_id
        direction_id = v.trip.direction_id if v.trip.HasField("direction_id") else _trip_direction.get(trip_id, 0)
        _vehicles[v.vehicle.id] = {
            "route_id": route_id,
            "direction_id": direction_id,
            "lat": v.position.latitude,
            "lon": v.position.longitude,
            "speed_kmh": v.position.speed if v.position.HasField("speed") else None,
            "seen_at": time.time(),
        }
    return True


# A bus that stops broadcasting (out of service, GPS dropout) otherwise sits
# in _vehicles forever — unbounded growth over an instance's uptime, not a
# one-time load spike, which matches "OOM-killed mid-call" better than a
# a cold-start spike would. Same 10-minute threshold where-bus itself uses.
STALE_VEHICLE_SECONDS = 600


def _evict_stale_vehicles() -> None:
    now = time.time()
    stale_ids = [vid for vid, v in _vehicles.items() if now - v["seen_at"] > STALE_VEHICLE_SECONDS]
    if not stale_ids:
        return
    for vid in stale_ids:
        del _vehicles[vid]
    stale_set = set(stale_ids)
    for key in [k for k in _eta_history if k[0] in stale_set]:
        del _eta_history[key]


def ensure_fresh(categories: set[str] | None = None) -> None:
    """Refetch only the categories that are actually stale, without ever
    blocking a tool call on data.gov.my being slow: the lock is a try, not a
    wait — if a refresh is already running (the /api/events poller or
    another tool call), this returns immediately and the caller answers
    from whatever's cached, per-category truth about what's actually fresh
    is _category_has_data's job, not this function's. Needed categories are
    fetched in parallel with a short per-request timeout (GTFS_RT_TIMEOUT),
    so even a full cold-start refresh stays well under a tool call's ~4s
    budget. `categories=None` (the /api/events poller's default) means
    "keep everything warm"; a tool call passes just what it needs so it
    never pays for a feed it didn't ask about."""
    _load_static()
    wanted = categories if categories is not None else set(CATEGORIES)
    now = time.time()
    stale = [c for c in wanted if now - _last_fetch.get(c, 0) >= LIVE_STALE_SECONDS and now >= _rt_backoff_until.get(c, 0)]
    if not stale:
        return

    if not _fetch_lock.acquire(blocking=False):
        log.info("ensure_fresh: lock busy, using cache for %s", stale)
        return  # someone else is already refreshing; never wait on it

    try:
        now = time.time()
        stale = [c for c in stale if now - _last_fetch.get(c, 0) >= LIVE_STALE_SECONDS]
        if not stale:
            return  # refreshed by the time we got the lock

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=len(stale)) as pool:
            oks = list(pool.map(_fetch_vehicles, stale))
        for category, ok in zip(stale, oks):
            _last_fetch[category] = time.time()
            _category_ok[category] = ok
        _evict_stale_vehicles()
        store.check_alerts(_alert_arrivals)
        log.info("ensure_fresh: fetched %s in %.2fs", stale, time.time() - t0)
    finally:
        _fetch_lock.release()


def _category_has_data(category: str) -> bool:
    """A category is usable if its latest fetch succeeded, or if vehicles
    from an earlier success are still cached (eviction already bounds how
    stale those can be — see STALE_VEHICLE_SECONDS). This is what "fresh"
    actually means per query: one feed succeeding must not mask the other
    one failing when the caller only asked about a route on the failing
    feed — that's the bug 'no buses nearby' vs 'feed unavailable' was."""
    if _category_ok.get(category):
        return True
    return any(_routes.get(v["route_id"], {}).get("category") == category for v in _vehicles.values())


def _is_stale(categories: set[str]) -> bool:
    """True if none of these categories' most recent fetch attempt actually
    succeeded — any arrivals built from them are from an earlier round, not
    confirmed fresh just now. Callers with usable-but-stale data say so
    rather than silently presenting old positions as current."""
    return not any(_category_ok.get(c) for c in categories)


STALE_CAVEAT = " (Bus positions are a bit delayed reaching me, so that's approximate.)"


def _live_data_error(categories: set[str], within_hours: bool, next_start: str) -> dict | None:
    """None if live data for these categories is usable; otherwise the
    ok=false payload to return as-is."""
    if not within_hours and not _vehicles:
        return {
            "ok": False,
            "reason": "no_service_night",
            "message": f"RapidKL isn't running right now. Services start again around {next_start}.",
        }
    if not any(_category_has_data(c) for c in categories):
        return {
            "ok": False,
            "reason": "feed_unavailable",
            "message": "I can't reach live bus positions right now. Try again in a moment.",
        }
    return None


# --------------------------------------------------------------- ETA engine

_eta_history: dict[tuple[str, str], deque[int]] = {}  # (vehicle_id, stop_id) -> recent ETAs


def _speed_mps(vehicle: dict) -> float:
    kmh = vehicle["speed_kmh"]
    if kmh is not None and 1.0 < kmh < 120.0:
        return kmh / 3.6
    return DEFAULT_SPEED_MPS


def _smooth(vehicle_id: str, stop_id: str, raw_seconds: int) -> int:
    key = (vehicle_id, stop_id)
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
                "category": _routes.get(route_id, {}).get("category"),
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
    categories_needed = {_routes.get(r_id, {}).get("category") for r_id, _, _ in candidates}
    ensure_fresh(categories_needed)

    error = _live_data_error(categories_needed, within_hours, next_start)
    if error:
        return error

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
    caveat = STALE_CAVEAT if _is_stale(categories_needed) else ""
    return {"ok": True, "stop": group.name, "arrivals": arrivals, "message": f"At {group.name}: {spoken}.{caveat}"}


# ------------------------------------------------------------------- trips


def _direct_route_options(from_group: StopGroup, to_group: StopGroup) -> list[tuple[str, int, str]]:
    """Every route-direction whose path serves from_group before to_group,
    as (route_id, direction_id, board_stop_id) — one hop only, no transfers.
    Pure static-data lookup, no live positions."""
    from_ids = set(from_group.stop_ids)
    to_ids = set(to_group.stop_ids)
    options: list[tuple[str, int, str]] = []
    for path_key, path in _route_paths.items():
        board_stop_id = None
        for stop_id in path:
            if board_stop_id is None:
                if stop_id in from_ids:
                    board_stop_id = stop_id
                continue
            if stop_id in to_ids:
                route_id, direction_id = path_key.rsplit("_", 1)
                options.append((route_id, int(direction_id), board_stop_id))
                break
    return options


def _rail_direct_options(from_group: RailStationGroup, to_group: RailStationGroup) -> list[tuple[str, int, int, int]]:
    """Same idea as _direct_route_options but over the rail network's own
    static station order: every line-direction serving from_group before
    to_group, as (line_id, direction_id, board_index, alight_index)."""
    from_ids = set(from_group.station_ids)
    to_ids = set(to_group.station_ids)
    options: list[tuple[str, int, int, int]] = []
    for path_key, path in _rail_paths.items():
        board_idx = None
        for i, station_id in enumerate(path):
            if board_idx is None:
                if station_id in from_ids:
                    board_idx = i
                continue
            if station_id in to_ids:
                line_id, direction_id = path_key.rsplit("_", 1)
                options.append((line_id, int(direction_id), board_idx, i))
                break
    return options


def _linked_rail_groups(bus_group: StopGroup) -> list[RailStationGroup]:
    names = _bus_to_rail.get(bus_group.name, [])
    return [_rail_group_by_display_name[n] for n in names if n in _rail_group_by_display_name]


_RAIL_PREFIX_RE = re.compile(r"^(LRT|MRT|BRT)\s+")

# Fare data: none of the three GTFS feeds (rapid-bus-kl, rapid-bus-mrtfeeder,
# rapid-rail-kl) ship fare_attributes.txt or fare_rules.txt — checked
# directly against the downloaded feeds, nothing to compute a per-route or
# per-OD fare from.
#
# The one number below that IS used is an official, citable flat fare:
# MRT Feeder Bus, RM1.00 per trip — Prasarana's own page,
# https://www.myrapid.com.my/traveling-with-us/how-to-travel-with-us/rapid-kl/mrt/mrt-feeder-bus
# (checked 2026-09-28). It applies to every route in rapid-bus-mrtfeeder,
# since that category IS the MRT Feeder Bus service.
#
# rapid-bus-kl fares are NOT one flat rate: Prasarana's own fare page
# (https://myrapid.com.my/bus-train/rapid-kl/bus/) shows Bandar/Tempatan
# services at a flat RM1, Ekspres at a flat RM3.80, and Utama services on a
# zonal RM1-RM3 table — and nothing in the GTFS feed cleanly tags which tier
# a given route_id belongs to. Rather than guess, every rapid-bus-kl leg's
# fare is left unknown (None). Same for every rail leg: LRT/MRT/Monorail
# fares are distance/zone-based per official sources, not a flat rate, and
# there's no fare table in the feed to compute an exact one from.
MRTFEEDER_FARE_RM = 1.00


def _leg_fare(category: str | None) -> float | None:
    return MRTFEEDER_FARE_RM if category == "rapid-bus-mrtfeeder" else None


def _rail_step(line_id: str, direction_id: int, board_idx: int, alight_idx: int, from_name: str, to_name: str) -> dict:
    path_key = f"{line_id}_{direction_id}"
    line = _rail_routes.get(line_id, {})
    line_name = _RAIL_PREFIX_RE.sub("", line.get("long_name") or line.get("short_name", line_id))
    headsign = _rail_headsign.get(path_key, "")
    terminal = headsign.rsplit(" to ", 1)[-1].strip() if " to " in headsign else line_name
    return {
        "mode": "rail",
        "line": line_name,
        "direction": terminal,
        "from_station": from_name,
        "to_station": to_name,
        "stops": alight_idx - board_idx,
        "fare": None,  # rail fares are distance/zone-based; not in the feed, not guessed
    }


def _bus_step(route_id: str, direction_id: int, board_stop_id: str, board_name: str, alight_name: str) -> dict | None:
    """None if no live vehicle can back up this leg with an ETA — the
    caller drops the whole option rather than announce a boarding time it
    doesn't have."""
    short_name = _routes.get(route_id, {}).get("short_name", route_id)
    arrivals = _arrivals_for(route_id, direction_id, board_stop_id, short_name)
    if not arrivals:
        return None
    best = min(arrivals, key=lambda a: a["eta_seconds"])
    return {
        "mode": "bus",
        "route": best["route"],
        "category": best["category"],
        "board_at": board_name,
        "alight_at": alight_name,
        "eta_seconds": best["eta_seconds"],
        "eta_human": best["eta_human"],
        "fare": _leg_fare(best["category"]),
    }


def _rail_inclusive_options(from_group: StopGroup, to_group: StopGroup) -> list[list[tuple]]:
    """Fallback tier for plan_trip, only tried when direct bus alone can't
    fill 2 options: rail-only (both ends within walking distance/name match
    of a station) and one-transfer bus<->rail combos. Returns structural
    step-candidates — (mode, ...) tuples, not yet live-checked; the caller
    resolves each bus leg to a real ETA and drops any option that fails."""
    from_rail = _linked_rail_groups(from_group)
    to_rail = _linked_rail_groups(to_group)
    results: list[list[tuple]] = []

    for fr in from_rail:
        for tr in to_rail:
            for line_id, direction_id, board_idx, alight_idx in _rail_direct_options(fr, tr):
                results.append([("rail", line_id, direction_id, board_idx, alight_idx, fr.name, tr.name)])

    # bus -> rail: direct bus from from_group to a stop near some station,
    # then that station's line toward one near to_group.
    if to_rail:
        for rail_name, bus_names in _rail_to_bus.items():
            station_group = _rail_group_by_display_name.get(rail_name)
            if station_group is None:
                continue
            for bus_name in bus_names:
                transfer_group = _bus_group_by_display_name.get(bus_name)
                if transfer_group is None or transfer_group.name in (from_group.name, to_group.name):
                    continue
                bus_legs = _direct_route_options(from_group, transfer_group)
                if not bus_legs:
                    continue
                route_id, direction_id, board_stop_id = bus_legs[0]
                for tr in to_rail:
                    for line_id, r_dir, board_idx, alight_idx in _rail_direct_options(station_group, tr):
                        results.append([
                            ("bus", route_id, direction_id, board_stop_id, from_group.name, transfer_group.name),
                            ("rail", line_id, r_dir, board_idx, alight_idx, station_group.name, tr.name),
                        ])
                break  # one transfer candidate at this station is enough

    # rail -> bus: symmetric.
    if from_rail:
        for rail_name, bus_names in _rail_to_bus.items():
            station_group = _rail_group_by_display_name.get(rail_name)
            if station_group is None:
                continue
            for bus_name in bus_names:
                transfer_group = _bus_group_by_display_name.get(bus_name)
                if transfer_group is None or transfer_group.name in (from_group.name, to_group.name):
                    continue
                bus_legs = _direct_route_options(transfer_group, to_group)
                if not bus_legs:
                    continue
                route_id, direction_id, board_stop_id = bus_legs[0]
                for fr in from_rail:
                    for line_id, r_dir, board_idx, alight_idx in _rail_direct_options(fr, station_group):
                        results.append([
                            ("rail", line_id, r_dir, board_idx, alight_idx, fr.name, station_group.name),
                            ("bus", route_id, direction_id, board_stop_id, transfer_group.name, to_group.name),
                        ])
                break

    return results


def _step_phrase(step: dict) -> str:
    if step["mode"] == "bus":
        return f"Route {step['route']} from {step['board_at']} to {step['alight_at']}, next one in {step['eta_human']}"
    plural = "s" if step["stops"] != 1 else ""
    return f"the {step['line']} toward {step['direction']}, {step['stops']} stop{plural} to {step['to_station']}"


def _step_leg_label(step: dict) -> str:
    return f"the {step['route']}" if step["mode"] == "bus" else f"the {step['line']}"


def _fare_total(steps: list[dict]) -> dict:
    known = [s["fare"] for s in steps if s.get("fare") is not None]
    return {"amount": round(sum(known), 2) if known else None, "all_known": len(known) == len(steps)}


def _fare_phrase(steps: list[dict]) -> str:
    """Only ever states a fare that came from a step's own 'fare' field —
    never invents or estimates one for a leg that doesn't have it."""
    known = [s for s in steps if s.get("fare") is not None]
    unknown = [s for s in steps if s.get("fare") is None]
    if not unknown:
        total = sum(s["fare"] for s in known)
        return f" Fare is about RM {total:.2f} in total."
    if known:
        parts = "; ".join(f"{_step_leg_label(s)} fare is RM {s['fare']:.2f}" for s in known)
        return f" {parts[0].upper()}{parts[1:]}; the rest of the fare isn't in my data."
    return ""


def plan_trip(from_stop_text: str, to_stop_text: str) -> dict:
    """Direct bus first (one hop, no transfers). If that alone can't offer
    2 options, falls back to one-transfer bus<->rail combos and rail-only
    journeys (LRT/MRT/Monorail/BRT) using the same station-order idea, just
    on the rail network's own static paths — no multi-transfer search here
    either. A rail leg reports line/direction/stop-count only: there's no
    realtime feed for rail in this app, so no rail ETA is ever invented."""
    _load_static()
    from_group, error = _resolve_group(from_stop_text)
    if error:
        return error
    to_group, error = _resolve_group(to_stop_text)
    if error:
        return error
    if from_group.name == to_group.name:
        return {"ok": False, "reason": "same_stop", "message": f"You're already at {from_group.name}."}

    step_candidates: list[list[tuple]] = [
        [("bus", route_id, direction_id, board_stop_id, from_group.name, to_group.name)]
        for route_id, direction_id, board_stop_id in _direct_route_options(from_group, to_group)
    ]
    if len(step_candidates) < 2:
        step_candidates += _rail_inclusive_options(from_group, to_group)

    if not step_candidates:
        return {
            "ok": False,
            "reason": "no_direct_route",
            "message": (
                f"I don't see a direct bus or train from {from_group.name} to {to_group.name}. "
                "Try checking arrivals at a bigger hub nearby instead."
            ),
        }

    categories_needed = {
        _routes.get(step[1], {}).get("category") for steps in step_candidates for step in steps if step[0] == "bus"
    }
    feed_error = None
    stale = False
    if categories_needed:
        within_hours, next_start = _service_window()
        ensure_fresh(categories_needed)  # one shared fetch for every option, not one per option
        feed_error = _live_data_error(categories_needed, within_hours, next_start)
        stale = _is_stale(categories_needed)

    resolved: list[list[dict]] = []
    for steps in step_candidates:
        resolved_steps: list[dict] | None = []
        for step in steps:
            if step[0] == "bus":
                _, route_id, direction_id, board_stop_id, board_name, alight_name = step
                bus_step = _bus_step(route_id, direction_id, board_stop_id, board_name, alight_name)
                if bus_step is None:
                    resolved_steps = None
                    break
                resolved_steps.append(bus_step)
            else:
                _, line_id, r_dir, board_idx, alight_idx, from_name, to_name = step
                resolved_steps.append(_rail_step(line_id, r_dir, board_idx, alight_idx, from_name, to_name))
        if resolved_steps:
            resolved.append(resolved_steps)
        if len(resolved) >= 2:
            break

    if not resolved:
        if feed_error:
            return feed_error
        return {
            "ok": False,
            "reason": "no_buses_nearby",
            "message": f"There's a route, but no buses are close enough to {from_group.name} right now for an ETA.",
        }

    phrases = [", then ".join(_step_phrase(s) for s in steps) for steps in resolved]
    message = "Take " + phrases[0] + "."
    message += _fare_phrase(resolved[0])
    if len(phrases) > 1:
        message += " Or " + phrases[1] + "."
    if stale:
        message += STALE_CAVEAT

    return {
        "ok": True,
        "from_stop": from_group.name,
        "to_stop": to_group.name,
        "options": [{"steps": steps, "fare_total": _fare_total(steps)} for steps in resolved],
        "message": message,
    }


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


# ------------------------------------------------------------- nearby stops

WALK_SPEED_MPS = 1.4  # ~5 km/h; straight-line distance, not a footpath route

# ponytail: single global slot, same as the rest of this demo's in-memory
# state (one event log, one alert list) — fine for one caller at a time,
# would need a per-session key if this ever serves concurrent callers.
_last_location: tuple[float, float] | None = None


def set_location(lat: float, lon: float) -> None:
    global _last_location
    _last_location = (lat, lon)


def find_nearby_stops(limit: int = 3) -> dict:
    _load_static()
    if _last_location is None:
        return {
            "ok": False,
            "reason": "no_location",
            "message": "I don't have the caller's location. Ask them to share it from the web page, or name a stop instead.",
        }

    lat, lon = _last_location
    ranked = []
    for group in _groups_by_name.values():
        nearest_m = min(
            _haversine_km(lat, lon, _stops[sid]["lat"], _stops[sid]["lon"]) * 1000.0
            for sid in group.stop_ids
            if sid in _stops
        )
        ranked.append((nearest_m, group))
    ranked.sort(key=lambda t: t[0])

    nearby = []
    for distance_m, group in ranked[:limit]:
        minutes = max(1, round(distance_m / WALK_SPEED_MPS / 60))
        nearby.append({"stop": group.name, "distance_meters": round(distance_m), "walk_minutes": minutes})

    spoken = ", ".join(f"{n['stop']} ({n['walk_minutes']} min walk)" for n in nearby)
    return {"ok": True, "nearby": nearby, "message": f"Nearest stops: {spoken}."}


# ---------------------------------------------------------------- demo info


def top_stop_names(limit: int = 80) -> list[str]:
    """Stop group names ranked by number of distinct routes serving them."""
    _load_static()
    ranked = []
    for group in _groups_by_name.values():
        route_count = len({r_id for sid in group.stop_ids for r_id, _ in _stop_to_routes.get(sid, [])})
        ranked.append((route_count, group.name))
    ranked.sort(key=lambda t: (-t[0], t[1]))

    seen: set[str] = set()
    names = []
    for _count, name in ranked:
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= limit:
            break
    return names


def top_route_short_names(limit: int | None = None) -> list[str]:
    """Route short names ranked by number of distinct stops on the route."""
    _load_static()
    stop_counts: dict[str, int] = {}
    for path_key, stop_ids in _route_paths.items():
        route_id = path_key.rsplit("_", 1)[0]
        stop_counts[route_id] = stop_counts.get(route_id, 0) + len(set(stop_ids))
    ranked = sorted(stop_counts.items(), key=lambda kv: -kv[1])

    seen: set[str] = set()
    names = []
    for route_id, _count in ranked:
        name = _routes.get(route_id, {}).get("short_name")
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
        if limit and len(names) >= limit:
            break
    return names


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
