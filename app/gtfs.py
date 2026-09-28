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

# Same word-index shape as above, built over rail station names instead of
# bus stop names — lets find_stop/next_arrivals/plan_trip run the identical
# tiered/sound-alike matching over stations (see _tiered_match).
_rail_word_to_groups: dict[str, set[str]] = {}
_rail_soundex_to_words: dict[str, set[str]] = {}

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


def _raw_normalize(text: str) -> str:
    text = _CODE_PREFIX_RE.sub("", text.strip())
    return _PUNCT_RE.sub(" ", text.lower())


def _normalize(text: str) -> str:
    words = [ABBREVIATIONS.get(w, w) for w in _raw_normalize(text).split()]
    return " ".join(words)


def _canonical(text: str) -> str:
    """Query-side normalization: resolves data/aliases.json against the
    caller's RAW words first ("petaling street", "the gardens" as authored)
    — checking only the abbreviation-translated form ("petaling jalan",
    "the taman") silently broke any alias whose key contains a word
    ABBREVIATIONS also translates — then falls back to the translated form,
    for an alias written in already-canonical spelling, then that
    translated form itself when there's no alias at all."""
    raw = _raw_normalize(text)
    if raw in ALIASES:
        return ALIASES[raw]
    translated = _normalize(text)
    return ALIASES.get(translated, translated)


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
    """One (route_id, direction_id) entry per stop_id, even for a loop route
    that revisits the same physical stop more than once in its path — a
    duplicate entry here meant next_arrivals asked _arrivals_for the exact
    same question twice and announced the same bus arriving twice ("Route
    T815 in 3 minutes, Route T815 in 3 minutes"). Which occurrence in the
    path a given vehicle is actually approaching is _stop_positions'/
    _arrivals_for's job, not this index's."""
    for path_key, stop_ids in _route_paths.items():
        route_id, direction_id = path_key.rsplit("_", 1)
        key = (route_id, int(direction_id))
        for stop_id in set(stop_ids):
            _stop_to_routes.setdefault(stop_id, []).append(key)


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
        color = row.get("route_color", "").strip()
        _rail_routes[row["route_id"]] = {
            "short_name": row["route_short_name"].strip(),
            "long_name": row["route_long_name"].strip(),
            "color": f"#{color}" if color else None,  # official line colour, map-only — not part of any tool response
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

    for group_key in _rail_groups_by_name:
        for word in group_key.split():
            _rail_word_to_groups.setdefault(word, set()).add(group_key)
    for word in _rail_word_to_groups:
        _rail_soundex_to_words.setdefault(_soundex(word), set()).add(word)


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


def _word_candidates(
    normalized_query: str,
    word_to_groups: dict[str, set[str]] = None,
    soundex_to_words: dict[str, set[str]] = None,
    groups_by_name: dict[str, object] = None,
) -> list:
    """Order-independent word match. Shared by the bus-stop and rail-station
    indexes (see _tiered_match) — defaults to the bus dicts so every
    existing call site keeps working unchanged.

    A word actually present in some group name is trusted outright. A word
    that ISN'T is only ever expanded via Soundex/close-spelling when it's
    the sole word in the query — with other real words alongside it, a
    stray Soundex coincidence (e.g. "Malaya" sound-alikes to "Mall"/"Mila")
    must never mix noise groups in next to a real match, so multi-word
    queries use only the words that hit exactly, never the fuzzy fallback.
    """
    word_to_groups = _word_to_groups if word_to_groups is None else word_to_groups
    soundex_to_words = _soundex_to_words if soundex_to_words is None else soundex_to_words
    groups_by_name = _groups_by_name if groups_by_name is None else groups_by_name

    words = [w for w in normalized_query.split() if w not in _STOPWORDS]
    if not words:
        return []

    exact_sets = [word_to_groups.get(w, set()) for w in words]
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
        return [groups_by_name[k] for k in keys]

    # Nothing matched a real word at all. Only worth a Soundex/spelling
    # guess when the query is a single word — there's no real match here
    # for a coincidental fuzzy hit to contaminate.
    if len(words) != 1 or len(words[0]) < 4:
        return []
    word = words[0]
    candidates = set(soundex_to_words.get(_soundex(word), set()))
    candidates |= set(difflib.get_close_matches(word, word_to_groups.keys(), n=3, cutoff=0.84))
    groups: set[str] = set()
    for cand in candidates:
        groups |= word_to_groups.get(cand, set())
    return [groups_by_name[k] for k in groups]


_TIER_RANK = {"exact": 4, "contains": 3, "word": 2, "close": 1}


def _tiered_match(
    normalized: str,
    groups_by_name: dict[str, object],
    word_to_groups: dict[str, set[str]],
    soundex_to_words: dict[str, set[str]],
) -> tuple[list, str | None]:
    """One namespace's (bus stops, or rail stations) best match for an
    already-normalized query, ranked exact > contains > word > close (see
    _TIER_RANK) so find_stop/next_arrivals/plan_trip can compare a bus match
    against a rail match and take whichever is genuinely stronger — not just
    whichever namespace happened to try first. Returns ([], None) rather than
    a low-confidence guess; guessing is a separate, cross-namespace step (see
    find_stop and _resolve_bus_or_rail) so a weak bus guess can never shadow
    a weak rail guess or vice versa."""
    exact = groups_by_name.get(normalized)
    if exact:
        return [exact], "exact"

    contains = [g for name, g in groups_by_name.items() if normalized in name or name in normalized]
    if contains:
        contains.sort(key=lambda g: len(g.name))
        return contains[:5], "contains"

    word_matches = _word_candidates(normalized, word_to_groups, soundex_to_words, groups_by_name)
    if word_matches:
        word_matches.sort(key=lambda g: len(g.name))
        return word_matches[:5], "word"

    # Confident tier, so the bar is high — 0.72 let scrambled multi-word
    # noise ("fidudaman sara") slip through at ~0.74 and get reported as a
    # sure match instead of a guess. Real typos score .95+, well clear of 0.8.
    close = difflib.get_close_matches(normalized, groups_by_name.keys(), n=5, cutoff=0.8)
    if close:
        return [groups_by_name[n] for n in close], "close"

    return [], None


def _search_groups(query: str) -> tuple[list[StopGroup], bool]:
    """Bus-stop-only tiered match, kept for the call sites that only ever
    want a boardable physical stop (set_arrival_alert, debug_stop). Returns
    (matches, confident); confident=False means a best-effort guess."""
    normalized = _canonical(query)

    matches, tier = _tiered_match(normalized, _groups_by_name, _word_to_groups, _soundex_to_words)
    if tier:
        return matches, True

    # Last resort: a best-effort top-3 guess, marked low-confidence so
    # callers phrase this as "did you mean" not "found" — but only above
    # MIN_GUESS_SCORE. Below that, a guess is worse than no guess: dishing
    # out unrelated stop names reads as confident nonsense. An empty list
    # here is the signal to offer find_nearby_stops instead of guessing.
    guess = difflib.get_close_matches(normalized, _groups_by_name.keys(), n=3, cutoff=MIN_GUESS_SCORE)
    return [_groups_by_name[n] for n in guess], False


def _best_match(query: str) -> tuple[list, bool, bool]:
    """The one comparison used by find_stop and _resolve_bus_or_rail: the
    best bus-stop tier against the best rail-station tier for the same
    query, picking whichever is strictly stronger (ties favour the bus
    stop, this app's long-standing default for a name like "KL Sentral"
    that's both a bus hub and a station). Returns (matches, confident,
    is_station); confident=False means matches is a merged best-effort
    guess across BOTH namespaces, ranked together — so a mishearing like
    "Miojim Nagara" can surface the rail station "Muzium Negara" instead of
    losing to an unrelated bus-stop guess that happened to be tried first."""
    normalized = _canonical(query)

    bus_matches, bus_tier = _tiered_match(normalized, _groups_by_name, _word_to_groups, _soundex_to_words)
    rail_matches, rail_tier = _tiered_match(normalized, _rail_groups_by_name, _rail_word_to_groups, _rail_soundex_to_words)
    bus_rank = _TIER_RANK.get(bus_tier, 0)
    rail_rank = _TIER_RANK.get(rail_tier, 0)

    if rail_rank > bus_rank:
        return rail_matches, True, True
    if bus_rank > 0:
        return bus_matches, True, False
    if rail_rank > 0:
        return rail_matches, True, True

    combined = {**{k: (False, g) for k, g in _groups_by_name.items()}, **{k: (True, g) for k, g in _rail_groups_by_name.items()}}
    guess_keys = difflib.get_close_matches(normalized, combined.keys(), n=3, cutoff=MIN_GUESS_SCORE)
    if not guess_keys:
        return [], False, False
    is_station, _ = combined[guess_keys[0]]  # only meaningful when len==1; mixed guesses label per-item below
    guesses = [combined[k] for k in guess_keys]
    return guesses, False, is_station


def _label(name: str, is_station: bool) -> str:
    return f"{name} station" if is_station else name


def find_stop(query: str) -> dict:
    _load_static()
    matches, confident, is_station = _best_match(query)

    if not confident:
        if not matches:
            return {
                "ok": False,
                "reason": "not_found",
                "message": "I couldn't find a stop by that name. Want me to check what's nearby instead?",
            }
        # matches here is a list of (is_station, group) pairs from the
        # merged cross-namespace guess — label each one individually.
        names = [_label(g.name, st) for st, g in matches]
        return {
            "ok": False,
            "reason": "not_found",
            "candidates": names,
            "message": f"I didn't catch a stop by that name. Did you mean {_speak_list(names)}?",
        }
    if len(matches) == 1:
        group = matches[0]
        label = _label(group.name, is_station)
        return {"ok": True, "stop": group.name, "is_station": is_station, "message": f"Found {label}."}
    names = [_label(m.name, is_station) for m in matches]
    return {
        "ok": True,
        "ambiguous": True,
        "candidates": names,
        "is_station": is_station,
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


def _resolve_bus_or_rail(stop_text: str) -> tuple[StopGroup | None, RailStationGroup | None, dict | None]:
    """The shared resolver for next_arrivals and plan_trip (_resolve_trip_
    endpoint below): compares the best bus-stop tier against the best
    rail-station tier (_best_match) and returns whichever is genuinely
    stronger, not just whichever a caller-naming-an-interchange-by-its-rail-
    name ("Pasar Seni", ambiguous among 9+ bus platforms but an exact rail
    match) or a mishearing of a station name ("Muzium Negara") happens to
    hit first on the bus side. Only errors (not_found/ambiguous_stop) when
    neither namespace resolves to exactly one match."""
    matches, confident, is_station = _best_match(stop_text)
    if not confident:
        if not matches:
            return None, None, {
                "ok": False,
                "reason": "not_found",
                "message": "I couldn't find a stop by that name. Want me to check what's nearby instead?",
            }
        names = [_label(g.name, st) for st, g in matches]
        return None, None, {
            "ok": False,
            "reason": "not_found",
            "candidates": names,
            "message": f"I didn't catch a stop by that name. Did you mean {_speak_list(names)}?",
        }
    if len(matches) > 1:
        names = [_label(m.name, is_station) for m in matches]
        return None, None, {
            "ok": False,
            "reason": "ambiguous_stop",
            "candidates": names,
            "message": "There's more than one stop with that name: " + ", ".join(names) + ". Which one?",
        }
    return (None, matches[0], None) if is_station else (matches[0], None, None)


# plan_trip and debug_trip both resolve endpoints via _resolve_bus_or_rail.
_resolve_trip_endpoint = _resolve_bus_or_rail


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


def _stop_positions(path_key: str, stop_id: str) -> list[tuple[float, int]]:
    """Every position stop_id occupies on this route-direction's path — a
    loop route can revisit the same physical stop more than once (e.g. T815
    passes Perpustakaan Um at both index 8 and 17 of its own path), and a
    vehicle currently between two such visits has genuinely NOT passed the
    stop yet; picking only the first occurrence (the old behaviour) made
    _arrivals_for wrongly call it 'already passed'."""
    path = _route_paths.get(path_key)
    distances = _stop_cum_dist.get(path_key)
    if not path or not distances:
        return []
    return [(distances[i], i) for i, sid in enumerate(path) if sid == stop_id]


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
    targets = _stop_positions(path_key, stop_id)

    results = []
    for vehicle_id, vehicle in _vehicles.items():
        if vehicle["route_id"] != route_id or vehicle["direction_id"] != direction_id:
            continue

        if targets:
            projected = _project_vehicle(path_key, vehicle["lat"], vehicle["lon"])
        else:
            projected = None

        if targets and projected is not None:
            bus_dist, _bus_idx = projected
            # A loop route can pass this stop more than once — use whichever
            # occurrence is next ahead of the bus, not just the first one in
            # the path, or a bus approaching a later visit reads as having
            # already passed an earlier one it's nowhere near yet.
            upcoming = [d for d, _i in targets if d > bus_dist]
            if not upcoming:
                continue  # passed every occurrence of this stop on this loop
            target_dist = min(upcoming)
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


def debug_stop(name: str) -> dict:
    """Diagnostic-only: why did next_arrivals/plan_trip include or exclude
    each live vehicle for this stop, right now. Mirrors _arrivals_for's own
    logic exactly (so the reasons are trustworthy) but never mutates
    anything — in particular it must NOT call _smooth(), which appends to
    _eta_history as a side effect; a debug call must not skew the rolling
    average a real tool call later reads. Not a tool: no agent ever calls
    this, it exists for /api/debug/stop."""
    _load_static()
    group, error = _resolve_group(name)
    if error:
        return {"ok": False, "stop": name, "error": error}

    # (route_id, direction_id, stop_id) for every route-direction serving
    # any physical stop in this group — duplicated across stop_ids collapses
    # to one entry per (route_id, direction_id), keeping the first stop_id.
    seen: dict[tuple[str, int], str] = {}
    for stop_id in group.stop_ids:
        for r_id, direction_id in _stop_to_routes.get(stop_id, []):
            seen.setdefault((r_id, direction_id), stop_id)

    categories_needed = {_routes.get(r_id, {}).get("category") for (r_id, _d) in seen}
    within_hours, next_start = _service_window()
    ensure_fresh(categories_needed)
    now = time.time()

    feeds = []
    for category in sorted(categories_needed):
        if category is None:
            continue
        last_fetch = _last_fetch.get(category)
        feeds.append({
            "category": category,
            "fetched_ok_last_attempt": _category_ok.get(category, False),
            "age_seconds": round(now - last_fetch, 1) if last_fetch else None,
            "in_backoff": now < _rt_backoff_until.get(category, 0),
            "has_cached_vehicles": any(v["route_id"] in {r for r, _d in seen if _routes.get(r, {}).get("category") == category} for v in _vehicles.values()),
        })

    routes = [
        {
            "route_id": r_id,
            "route_short_name": _routes.get(r_id, {}).get("short_name", r_id),
            "category": _routes.get(r_id, {}).get("category"),
            "direction_id": direction_id,
            "board_stop_id": stop_id,
        }
        for (r_id, direction_id), stop_id in sorted(seen.items())
    ]

    route_ids_here = {r_id for r_id, _d in seen}
    vehicles_out = []
    for vehicle_id, vehicle in _vehicles.items():
        if vehicle["route_id"] not in route_ids_here:
            continue  # a different route entirely — not this stop's business

        # A vehicle can be tracked on a route this stop serves but in the
        # OTHER direction — that's a real, common exclusion reason, so it's
        # reported per every (route_id, direction_id) this vehicle's route
        # actually serves here, not just silently skipped.
        for (r_id, direction_id), stop_id in seen.items():
            if r_id != vehicle["route_id"]:
                continue
            path_key = f"{r_id}_{direction_id}"
            route_short_name = _routes.get(r_id, {}).get("short_name", r_id)
            entry = {
                "vehicle_id": vehicle_id,
                "route": route_short_name,
                "route_id": r_id,
                "vehicle_direction_id": vehicle["direction_id"],
                "checked_direction_id": direction_id,
                "seen_age_seconds": round(now - vehicle["seen_at"], 1),
            }
            if vehicle["direction_id"] != direction_id:
                entry["excluded_reason"] = "wrong_direction"
                vehicles_out.append(entry)
                continue

            targets = _stop_positions(path_key, stop_id)
            projected = _project_vehicle(path_key, vehicle["lat"], vehicle["lon"]) if targets else None
            if targets and projected is not None:
                bus_dist, bus_idx = projected
                entry["snapped_route_index"] = bus_idx
                entry["target_route_indices"] = [i for _d, i in targets]  # >1 means this stop is on a loop
                upcoming = [d for d, _i in targets if d > bus_dist]
                if not upcoming:
                    entry["excluded_reason"] = "passed_stop"
                    vehicles_out.append(entry)
                    continue
                target_dist = min(upcoming)
                distance_m = (target_dist - bus_dist) * 1000.0
            else:
                stop = _stops.get(stop_id)
                if stop is None:
                    entry["excluded_reason"] = "no_stop_coordinates"
                    vehicles_out.append(entry)
                    continue
                distance_m = _haversine_km(vehicle["lat"], vehicle["lon"], stop["lat"], stop["lon"]) * 1000.0 * HAVERSINE_ROAD_FACTOR
                entry["note"] = "no route-shape snap available — haversine fallback"

            raw_seconds = int(distance_m / _speed_mps(vehicle))
            entry["distance_m"] = round(distance_m, 1)
            entry["raw_eta_seconds"] = raw_seconds
            history = _eta_history.get((vehicle_id, stop_id))
            entry["current_smoothed_eta_seconds"] = round(sum(history) / len(history)) if history else None
            if raw_seconds > MAX_ETA_SECONDS:
                entry["excluded_reason"] = ">35_min"
            else:
                entry["excluded_reason"] = None  # would be included
            vehicles_out.append(entry)

    return {
        "ok": True,
        "stop": group.name,
        "within_service_hours": within_hours,
        "next_service_start": next_start if not within_hours else None,
        "routes": routes,
        "feeds": feeds,
        "vehicles": vehicles_out,
    }


def _service_window() -> tuple[bool, str]:
    """Returns (within_operating_hours, spoken_next_start)."""
    now = datetime.now(KL_TZ)
    if SERVICE_START_HOUR <= now.hour < SERVICE_END_HOUR:
        return True, ""
    next_start = f"{SERVICE_START_HOUR}:00 am"
    return False, next_start


def _station_as_stop_group(station: RailStationGroup) -> dict | StopGroup:
    """A caller asking for arrivals "at" a station means the buses at its
    nearest linked bus stops (there's no bus fleet running on the rail
    network itself) — so next_arrivals treats the station as a synthetic
    StopGroup covering every bus stop group _rail_to_bus links to it. Returns
    an error payload instead if the station has no linked bus stop at all."""
    linked_ids = [
        sid
        for name in _rail_to_bus.get(station.name, [])
        for sid in _bus_group_by_display_name.get(name, StopGroup(name="", stop_ids=[])).stop_ids
    ]
    if not linked_ids:
        return {
            "ok": False,
            "reason": "route_not_at_stop",
            "message": f"{station.name} doesn't have a nearby bus stop I can check arrivals for.",
        }
    return StopGroup(name=station.name, stop_ids=linked_ids)


def next_arrivals(stop_text: str, route_text: str | None = None) -> dict:
    _load_static()
    group, station, error = _resolve_bus_or_rail(stop_text)
    if error:
        return error
    if station:
        group_or_error = _station_as_stop_group(station)
        if isinstance(group_or_error, dict):
            return group_or_error
        group = group_or_error

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


def _bus_reachable_rail_stations(from_group: StopGroup) -> list[RailStationGroup]:
    """Rail stations reachable from from_group by a single direct bus ride —
    broader than _linked_rail_groups (walking distance only). Used as the
    no_direct_route fallback's "nearest useful rail station" suggestion: a
    stop whose only route is a campus shuttle (e.g. Kolej Kediaman Kesepuluh
    on T815) has no WALKING-distance station, but T815 itself terminates at
    Phileo Damansara MRT — worth naming even though that station's own line
    doesn't happen to reach the caller's destination within one transfer."""
    found: list[RailStationGroup] = []
    seen: set[str] = set()
    for rail_name, bus_names in _rail_to_bus.items():
        if rail_name in seen:
            continue
        station_group = _rail_group_by_display_name.get(rail_name)
        if station_group is None:
            continue
        for bus_name in bus_names:
            transfer_group = _bus_group_by_display_name.get(bus_name)
            if transfer_group is None or transfer_group.name == from_group.name:
                continue
            if _direct_route_options(from_group, transfer_group):
                found.append(station_group)
                seen.add(rail_name)
                break
    return found


_RAIL_PREFIX_RE = re.compile(r"^(LRT|MRT|BRT)\s+")


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
    }


def _rail_inclusive_options(
    from_group: StopGroup | None,
    to_group: StopGroup | None,
    from_rail: list[RailStationGroup],
    to_rail: list[RailStationGroup],
) -> list[list[tuple]]:
    """Fallback tier for plan_trip, only tried when direct bus alone can't
    fill 2 options: rail-only (both ends within walking distance/name match
    of a station) and one-transfer bus<->rail combos. Returns structural
    step-candidates — (mode, ...) tuples, not yet live-checked; the caller
    resolves each bus leg to a real ETA and drops any option that fails.

    from_rail/to_rail are pre-resolved by the caller: usually the group's own
    linked stations, but a caller-named endpoint like "Pasar Seni" that has
    no single matching bus platform resolves straight to its rail station,
    with from_group/to_group left None — that end simply skips the tiers
    that need an actual bus stop to board or alight at."""
    from_name = from_group.name if from_group else None
    to_name = to_group.name if to_group else None
    results: list[list[tuple]] = []

    for fr in from_rail:
        for tr in to_rail:
            for line_id, direction_id, board_idx, alight_idx in _rail_direct_options(fr, tr):
                results.append([("rail", line_id, direction_id, board_idx, alight_idx, fr.name, tr.name)])

    # bus -> rail: direct bus from from_group to a stop near some station,
    # then that station's line toward one near to_group.
    if to_rail and from_group is not None:
        for rail_name, bus_names in _rail_to_bus.items():
            station_group = _rail_group_by_display_name.get(rail_name)
            if station_group is None:
                continue
            for bus_name in bus_names:
                transfer_group = _bus_group_by_display_name.get(bus_name)
                if transfer_group is None or transfer_group.name in (from_name, to_name):
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
    if from_rail and to_group is not None:
        for rail_name, bus_names in _rail_to_bus.items():
            station_group = _rail_group_by_display_name.get(rail_name)
            if station_group is None:
                continue
            for bus_name in bus_names:
                transfer_group = _bus_group_by_display_name.get(bus_name)
                if transfer_group is None or transfer_group.name in (from_name, to_name):
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


def debug_trip(from_text: str, to_text: str) -> dict:
    """Diagnostic only, not a tool the agent can call: why plan_trip found
    (or didn't find) a route between these two stops right now. Shows every
    route touching the from-stop and whether/where it reaches the to-stop —
    checking ALL occurrences of each stop on the path, not just the first,
    so a loop route's real reach is visible even where the production
    matcher might still be using only the first one. See /api/debug/trip."""
    _load_static()
    from_group, from_rail_direct, from_err = _resolve_trip_endpoint(from_text)
    to_group, to_rail_direct, to_err = _resolve_trip_endpoint(to_text)

    out: dict = {
        "from_text": from_text,
        "to_text": to_text,
        "from_resolved": from_group.name if from_group else (from_rail_direct.name if from_rail_direct else None),
        "from_resolved_as_rail_station": from_group is None and from_rail_direct is not None,
        "from_error": from_err,
        "to_resolved": to_group.name if to_group else (to_rail_direct.name if to_rail_direct else None),
        "to_resolved_as_rail_station": to_group is None and to_rail_direct is not None,
        "to_error": to_err,
    }
    if from_err or to_err:
        return out

    from_rail = _linked_rail_groups(from_group) if from_group else []
    if from_rail_direct and from_rail_direct.name not in {g.name for g in from_rail}:
        from_rail = [*from_rail, from_rail_direct]
    to_rail = _linked_rail_groups(to_group) if to_group else []
    if to_rail_direct and to_rail_direct.name not in {g.name for g in to_rail}:
        to_rail = [*to_rail, to_rail_direct]

    candidates = []
    if from_group:
        from_ids = set(from_group.stop_ids)
        to_ids = set(to_group.stop_ids) if to_group else set()
        for path_key, path in _route_paths.items():
            from_positions = [i for i, sid in enumerate(path) if sid in from_ids]
            if not from_positions:
                continue
            to_positions = [i for i, sid in enumerate(path) if sid in to_ids]
            route_id, direction_id = path_key.rsplit("_", 1)
            reaches = any(t > f for f in from_positions for t in to_positions)
            candidates.append({
                "route": _routes.get(route_id, {}).get("short_name", route_id),
                "route_id": route_id,
                "direction_id": int(direction_id),
                "path_length": len(path),
                "from_positions": from_positions,
                "to_positions": to_positions,
                "reaches_to_stop": reaches,
                "rejected_reason": None if reaches else ("to_stop_not_on_this_route" if not to_positions else "to_stop_only_before_from_stop"),
            })

    out["candidate_bus_routes"] = candidates
    out["from_linked_rail_stations"] = [g.name for g in from_rail]
    out["to_linked_rail_stations"] = [g.name for g in to_rail]
    out["from_bus_reachable_rail_stations"] = [g.name for g in (_bus_reachable_rail_stations(from_group) if from_group else [])]
    out["direct_route_options"] = [
        {"route": _routes.get(r, {}).get("short_name", r), "direction_id": d, "board_stop_id": s}
        for r, d, s in (_direct_route_options(from_group, to_group) if from_group and to_group else [])
    ]
    rail_inclusive = _rail_inclusive_options(from_group, to_group, from_rail, to_rail)
    out["rail_inclusive_options_count"] = len(rail_inclusive)
    out["rail_inclusive_options"] = [
        [{"mode": t[0], **({"route": _routes.get(t[1], {}).get("short_name", t[1])} if t[0] == "bus" else {"line": t[1]})} for t in option]
        for option in rail_inclusive
    ]
    out["plan_trip_result"] = plan_trip(from_text, to_text)
    return out


def _step_phrase(step: dict) -> str:
    if step["mode"] == "bus":
        return f"Route {step['route']} from {step['board_at']} to {step['alight_at']}, next one in {step['eta_human']}"
    plural = "s" if step["stops"] != 1 else ""
    return f"the {step['line']} toward {step['direction']}, {step['stops']} stop{plural} to {step['to_station']}"


def plan_trip(from_stop_text: str, to_stop_text: str) -> dict:
    """Direct bus first (one hop, no transfers). If that alone can't offer
    2 options, falls back to one-transfer bus<->rail combos and rail-only
    journeys (LRT/MRT/Monorail/BRT) using the same station-order idea, just
    on the rail network's own static paths — no multi-transfer search here
    either. A rail leg reports line/direction/stop-count only: there's no
    realtime feed for rail in this app, so no rail ETA is ever invented."""
    _load_static()
    from_group, from_rail_direct, error = _resolve_trip_endpoint(from_stop_text)
    if error:
        return error
    to_group, to_rail_direct, error = _resolve_trip_endpoint(to_stop_text)
    if error:
        return error
    from_name = from_group.name if from_group else from_rail_direct.name
    to_name = to_group.name if to_group else to_rail_direct.name
    if from_name == to_name:
        return {"ok": False, "reason": "same_stop", "message": f"You're already at {from_name}."}

    from_rail = _linked_rail_groups(from_group) if from_group else []
    if from_rail_direct and from_rail_direct.name not in {g.name for g in from_rail}:
        from_rail = [*from_rail, from_rail_direct]
    to_rail = _linked_rail_groups(to_group) if to_group else []
    if to_rail_direct and to_rail_direct.name not in {g.name for g in to_rail}:
        to_rail = [*to_rail, to_rail_direct]

    step_candidates: list[list[tuple]] = [
        [("bus", route_id, direction_id, board_stop_id, from_name, to_name)]
        for route_id, direction_id, board_stop_id in (
            _direct_route_options(from_group, to_group) if from_group and to_group else []
        )
    ]
    if len(step_candidates) < 2:
        step_candidates += _rail_inclusive_options(from_group, to_group, from_rail, to_rail)

    if not step_candidates:
        nearby_rail_groups = from_rail or (_bus_reachable_rail_stations(from_group) if from_group else [])
        nearby_rail = [g.name for g in nearby_rail_groups]
        hint = (
            f"There's a rail station near {from_name} — {nearby_rail[0]} — worth checking from there."
            if nearby_rail
            else "Try checking arrivals at a bigger hub nearby instead."
        )
        return {
            "ok": False,
            "reason": "no_direct_route",
            "nearby_rail_stations": nearby_rail,
            "message": f"I don't see a direct bus or train from {from_name} to {to_name}. {hint}",
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
                _, line_id, r_dir, board_idx, alight_idx, leg_from_name, leg_to_name = step
                resolved_steps.append(_rail_step(line_id, r_dir, board_idx, alight_idx, leg_from_name, leg_to_name))
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
            "message": f"There's a route, but no buses are close enough to {from_name} right now for an ETA.",
        }

    phrases = [", then ".join(_step_phrase(s) for s in steps) for steps in resolved]
    message = "Take " + phrases[0] + "."
    if len(phrases) > 1:
        message += " Or " + phrases[1] + "."
    if stale:
        message += STALE_CAVEAT

    return {
        "ok": True,
        "from_stop": from_name,
        "to_stop": to_name,
        "options": [{"steps": steps} for steps in resolved],
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
