"""Seed app/insights.py's SQLite table with realistic sample data, so
/insights has something to show before any real calls happen.

Only inserts when the events table is currently empty — this is a demo
convenience for a fresh checkout, not meant to mix fake and real rows into
one dataset. Rows are marked is_sample=1 so /insights can label them.

Run: python scripts/seed_insights.py
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import insights  # noqa: E402
from app.gtfs import KL_TZ, STALE_CAVEAT  # noqa: E402

random.seed(7)  # reproducible sample data

# Real stop/route names seen in this app's own data this session — not
# invented, just illustrative of the shape of real traffic.
POPULAR_STOPS = [
    "KL Sentral", "Pasar Seni (Platform B5)", "Mid Valley (Selatan)", "Perpustakaan Um",
    "Fakulti Sains Komputer", "Bandar Kajang", "Klcc", "Bangsar Utama", "Sogo KL", "Hub Lebuh Pudu",
]
ROUTES = ["T789", "300", "770", "T815", "U8220", "200", "780", "220", "450", "T406"]

# Realistic mishearings/garbled STT output for stops that don't resolve —
# these are what motivated the find_nearby_stops fallback, not real transcripts.
UNRESOLVED_QUERIES = [
    "kolej kediaman", "sains komputer fakulti", "damansara utama one",
    "pasar malam jalan", "taman connaught mrt", "sri petaling wan",
    "bukit bintang plaza low yat", "usj taipan",
]

TRIP_PAIRS_NO_ROUTE = [
    ("Bandar Kajang", "Taman Botani Putrajaya"),
    ("Klcc", "Usj 7"),
    ("Bangsar Utama", "Bandar Kajang"),
]


def _find_stop_event(hour_ts: float) -> tuple[float, str, dict, dict]:
    if random.random() < 0.82:
        stop = random.choice(POPULAR_STOPS)
        return hour_ts, "find_stop", {"query": stop}, {"ok": True, "stop": stop, "message": f"Found {stop}."}
    query = random.choice(UNRESOLVED_QUERIES)
    return (
        hour_ts,
        "find_stop",
        {"query": query},
        {"ok": False, "reason": "not_found", "message": "I couldn't find a stop by that name. Want me to check what's nearby instead?"},
    )


def _next_arrivals_event(hour_ts: float) -> tuple[float, str, dict, dict]:
    stop = random.choice(POPULAR_STOPS)
    route = random.choice(ROUTES) if random.random() < 0.3 else None
    args = {"stop": stop, "route": route}
    roll = random.random()
    if roll < 0.75:
        result = {"ok": True, "stop": stop, "arrivals": [{"route": route or random.choice(ROUTES), "eta_human": "8 minutes"}], "message": f"At {stop}: Route {route or random.choice(ROUTES)} in 8 minutes."}
    elif roll < 0.85:
        result = {"ok": True, "stop": stop, "arrivals": [{"route": route or random.choice(ROUTES), "eta_human": "12 minutes"}], "message": f"At {stop}: Route {route or random.choice(ROUTES)} in 12 minutes.{STALE_CAVEAT}"}
    elif roll < 0.93:
        result = {"ok": False, "reason": "no_buses_nearby", "message": f"No buses are currently close enough to {stop} to give an ETA."}
    else:
        result = {"ok": False, "reason": "feed_unavailable", "message": "I can't reach live bus positions right now. Try again in a moment."}
    return hour_ts, "next_arrivals", args, result


def _plan_trip_event(hour_ts: float) -> tuple[float, str, dict, dict]:
    if random.random() < 0.25:
        from_stop, to_stop = random.choice(TRIP_PAIRS_NO_ROUTE)
        return (
            hour_ts, "plan_trip", {"from_stop": from_stop, "to_stop": to_stop},
            {"ok": False, "reason": "no_direct_route", "message": f"I don't see a direct bus or train from {from_stop} to {to_stop}. Try checking arrivals at a bigger hub nearby instead."},
        )
    from_stop, to_stop = random.sample(POPULAR_STOPS, 2)
    return (
        hour_ts, "plan_trip", {"from_stop": from_stop, "to_stop": to_stop},
        {"ok": True, "from_stop": from_stop, "to_stop": to_stop, "options": [{"steps": [{"mode": "bus", "route": random.choice(ROUTES), "eta_human": "6 minutes"}]}], "message": f"Take Route {random.choice(ROUTES)} from {from_stop} to {to_stop}, next one in 6 minutes."},
    )


# More calls during commute hours, a trickle overnight — shapes the "by hour
# of day" chart into something worth showing, not a flat line.
HOUR_WEIGHTS = [1, 1, 1, 1, 1, 2, 5, 12, 16, 8, 5, 6, 7, 6, 5, 6, 9, 15, 13, 7, 4, 3, 2, 1]


def generate(days: int = 7) -> list[tuple[float, str, dict, dict]]:
    today = datetime.now(KL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    events = []
    for day in range(days):
        day_start = today - timedelta(days=days - day)
        for hour, weight in enumerate(HOUR_WEIGHTS):
            count = random.randint(max(0, weight - 2), weight + 2)
            for _ in range(count):
                ts = (day_start + timedelta(hours=hour, seconds=random.randint(0, 3599))).timestamp()
                builder = random.choices(
                    [_find_stop_event, _next_arrivals_event, _plan_trip_event],
                    weights=[0.4, 0.35, 0.25],
                )[0]
                events.append(builder(ts))
    return events


def main() -> None:
    if insights._rows():
        print("insights.db already has events — not seeding (delete data/insights.db first if you want fresh sample data)")
        return
    events = generate()
    insights.seed(events)
    print(f"seeded {len(events)} sample events into {insights.DB_FILE}")


if __name__ == "__main__":
    main()
