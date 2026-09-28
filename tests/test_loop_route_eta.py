"""Regression test for a real bug found via /api/debug/stop against
Perpustakaan Um: T815's own route path revisits that stop twice (a loop —
index 8 and index 17 of its 28-stop path). The old code used only the FIRST
occurrence (path.index()), which caused two real problems:

  1. _stop_to_routes got a duplicate (route_id, direction_id) entry for that
     stop, so next_arrivals asked _arrivals_for the identical question twice
     and announced the same bus arriving twice ("Route T815 in 3 minutes,
     Route T815 in 3 minutes").
  2. A vehicle positioned between the two occurrences read as having
     "already passed" the stop (compared only against the earlier index),
     even though it was genuinely approaching the second, later visit.

Uses a small synthetic route (gtfs.py's module state stubbed directly and
restored after) rather than live vehicles, so this is deterministic — no
network, no dependence on which buses happen to be tracked right now.

Run: python tests/test_loop_route_eta.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import gtfs  # noqa: E402

gtfs._load_static()

ROUTE_ID = "R"
DIRECTION_ID = 0
PATH_KEY = f"{ROUTE_ID}_{DIRECTION_ID}"  # must match what _arrivals_for itself constructs
STOP_ID = "TEST_STOP"


class LoopRouteEtaTests(unittest.TestCase):
    def setUp(self):
        # A tiny 5-stop loop where STOP_ID appears at index 1 and index 4,
        # 1km apart along the path each time.
        self._route_paths_backup = dict(gtfs._route_paths)
        self._stop_cum_dist_backup = dict(gtfs._stop_cum_dist)
        self._stops_backup = dict(gtfs._stops)
        self._vehicles_backup = dict(gtfs._vehicles)
        self._eta_history_backup = dict(gtfs._eta_history)

        gtfs._route_paths[PATH_KEY] = ["A", STOP_ID, "B", "C", STOP_ID, "D"]
        gtfs._stop_cum_dist[PATH_KEY] = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
        for i, sid in enumerate(["A", STOP_ID, "B", "C", "D"]):
            gtfs._stops[sid] = {"name": sid, "lat": 3.100 + i * 0.01, "lon": 101.600}
        gtfs._vehicles.clear()
        gtfs._eta_history.clear()

    def tearDown(self):
        gtfs._route_paths.clear()
        gtfs._route_paths.update(self._route_paths_backup)
        gtfs._stop_cum_dist.clear()
        gtfs._stop_cum_dist.update(self._stop_cum_dist_backup)
        gtfs._stops.clear()
        gtfs._stops.update(self._stops_backup)
        gtfs._vehicles.clear()
        gtfs._vehicles.update(self._vehicles_backup)
        gtfs._eta_history.clear()
        gtfs._eta_history.update(self._eta_history_backup)

    def test_stop_positions_returns_every_occurrence(self):
        self.assertEqual(gtfs._stop_positions(PATH_KEY, STOP_ID), [(1.0, 1), (4.0, 4)])

    def test_vehicle_between_two_occurrences_is_not_wrongly_passed(self):
        # Snapped at "B" (path index 2, distance 2.0km) — between the stop's
        # two visits at 1.0km and 4.0km. The old code compared only against
        # the first visit (1.0km) and wrongly called this bus "already
        # passed"; it should instead be found via the second, upcoming visit.
        gtfs._vehicles["bus1"] = {
            "route_id": "R", "direction_id": 0,
            "lat": gtfs._stops["B"]["lat"], "lon": gtfs._stops["B"]["lon"],
            "speed_kmh": None, "seen_at": 0.0,
        }
        arrivals = gtfs._arrivals_for("R", 0, STOP_ID, "TEST")
        self.assertEqual(len(arrivals), 1, arrivals)

    def test_vehicle_past_every_occurrence_is_excluded(self):
        # Snapped at "D" (index 5, 5.0km) — past both visits of STOP_ID
        # (1.0km and 4.0km). Genuinely nothing left to arrive for.
        gtfs._vehicles["bus2"] = {
            "route_id": "R", "direction_id": 0,
            "lat": gtfs._stops["D"]["lat"], "lon": gtfs._stops["D"]["lon"],
            "speed_kmh": None, "seen_at": 0.0,
        }
        arrivals = gtfs._arrivals_for("R", 0, STOP_ID, "TEST")
        self.assertEqual(arrivals, [])

    def test_no_stop_has_duplicate_route_direction_entries(self):
        # Broad check against the REAL static data, not just the synthetic
        # loop above — catches this class of bug for any stop, not only
        # Perpustakaan Um.
        for stop_id, pairs in gtfs._stop_to_routes.items():
            self.assertEqual(len(pairs), len(set(pairs)), f"{stop_id} has duplicate entries: {pairs}")


if __name__ == "__main__":
    unittest.main()
