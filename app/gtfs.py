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
ALIASES_FILE = ROOT / "data" / "aliases.json"
CATEGORIES = ["rapid-bus-kl", "rapid-bus-mrtfeeder"]
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
        _routes[row["route_id"]] = {"short_name": short_name, "long_name": row["route_long_name"].strip()}
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
    global _loaded
    if _loaded:
        return
    for category in CATEGORIES:
        if not (_cache_dir(category) / "stops.txt").exists():
            _download_static(category)
        _load_stops(category)
        _load_routes(category)
        shapes = _load_shapes(category)
        _build_route_paths_and_distances(category, shapes)

    _build_stop_index()
    _build_stop_groups()
    _build_word_index()
    _load_aliases()
    _loaded = True


# ----------------------------------------------------------------- find_stop


def _speak_list(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" or {items[-1]}"


def _word_candidates(normalized_query: str) -> list[StopGroup]:
    """Order-independent word match.

    A word actually present in some group name is trusted outright. A word
    that ISN'T is only ever expanded via Soundex/close-spelling when it's
    the sole word in the query — with other real words alongside it, a
    stray Soundex coincidence (e.g. "Malaya" sound-alikes to "Mall"/"Mila")
    must never mix noise groups in next to a real match, so multi-word
    queries use only the words that hit exactly, never the fuzzy fallback.
    """
    words = normalized_query.split()
    if not words:
        return []

    exact_sets = [_word_to_groups.get(w, set()) for w in words]
    recognized = sum(1 for s in exact_sets if s)

    if recognized > 0:
        if recognized <= len(words) / 2:
            return []  # most of the query is noise; let a lower-confidence tier handle it
        non_empty = [s for s in exact_sets if s]
        all_match = set.intersection(*non_empty) if len(non_empty) == recognized else set()
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
_last_fetch = 0.0
_last_fetch_ok = False
_rt_backoff_until: dict[str, float] = {}  # category -> epoch seconds to skip until


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
    try:
        resp = httpx.get(url, timeout=10, follow_redirects=True)
    except Exception:
        return False

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
        }
    return True


def ensure_fresh() -> None:
    global _last_fetch, _last_fetch_ok
    _load_static()
    now = time.time()
    if now - _last_fetch < LIVE_STALE_SECONDS:
        return
    _last_fetch = now

    any_ok = False
    for category in CATEGORIES:
        if now < _rt_backoff_until.get(category, 0):
            continue
        if _fetch_vehicles(category):
            any_ok = True
    _last_fetch_ok = any_ok

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
