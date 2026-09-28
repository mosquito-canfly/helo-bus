"""Regression test for app/mapstate.py's geometry re-derivation.

mapstate.py never touches gtfs.py's matching/ETA logic — it re-resolves a
plan_trip/next_arrivals result's stop names back to coordinates using gtfs's
existing public lookups. This is non-trivial logic (name -> group -> route
path -> slice) with a real way to get it silently wrong (e.g. an empty
points list), so it gets one check like everything else non-trivial in this
codebase.

Run: python tests/test_mapstate.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import gtfs, mapstate  # noqa: E402

gtfs._load_static()


class MapStateTests(unittest.TestCase):
    def test_bus_plus_rail_trip_has_real_polylines(self):
        result = gtfs.plan_trip("Fakulti Sains Komputer", "Pasar Seni (Platform B5)")
        self.assertTrue(result["ok"], result)
        mapstate.set_from_plan_trip(result)
        state = mapstate.get()

        self.assertEqual(state["kind"], "trip")
        self.assertGreaterEqual(len(state["stops"]), 2)
        self.assertEqual({leg["mode"] for leg in state["legs"]}, {"bus", "rail"})
        for leg in state["legs"]:
            self.assertGreaterEqual(len(leg["points"]), 2, leg)
            for lat, lon in leg["points"]:
                self.assertTrue(1.0 < lat < 4.0, lat)  # sanity: within the Klang Valley
                self.assertTrue(100.0 < lon < 102.5, lon)

    def test_next_arrivals_places_the_stop(self):
        result = gtfs.next_arrivals("KL Sentral")
        mapstate.set_from_next_arrivals(result)
        state = mapstate.get()

        self.assertEqual(state["kind"], "stop")
        self.assertEqual(len(state["stops"]), 1)
        self.assertEqual(state["stops"][0]["name"], "KL Sentral")

    def test_failed_result_does_not_touch_existing_state(self):
        gtfs.next_arrivals("KL Sentral")
        mapstate.set_from_next_arrivals(gtfs.next_arrivals("KL Sentral"))
        before = mapstate.get()
        mapstate.set_from_plan_trip({"ok": False, "reason": "no_direct_route"})
        self.assertEqual(mapstate.get(), before)


if __name__ == "__main__":
    unittest.main()
