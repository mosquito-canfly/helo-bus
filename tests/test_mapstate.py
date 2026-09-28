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
        # A synthetic result shaped exactly like a real plan_trip answer for
        # this stop pair (structurally verified elsewhere this session) —
        # built directly rather than calling gtfs.plan_trip(), which needs a
        # live bus ETA to return ok=True and would make this test flaky
        # depending on which buses happen to be tracked right now (same
        # reason next_arrivals/plan_trip themselves have no live test).
        result = {
            "ok": True,
            "from_stop": "Fakulti Sains Komputer",
            "to_stop": "Pasar Seni (Platform B5)",
            "options": [{
                "steps": [
                    {
                        "mode": "bus", "route": "T815", "category": "rapid-bus-mrtfeeder",
                        "board_at": "Fakulti Sains Komputer", "alight_at": "MRT Phileo Damansara Pintu A",
                        "eta_seconds": 480, "eta_human": "8 minutes",
                    },
                    {
                        "mode": "rail", "line": "Kajang Line", "direction": "Kajang",
                        "from_station": "Phileo Damansara", "to_station": "Pasar Seni",
                        "stops": 4,
                    },
                ],
            }],
        }
        mapstate.set_from_plan_trip(result)
        state = mapstate.get()

        self.assertEqual(state["kind"], "trip")
        option = state["options"][0]
        self.assertGreaterEqual(len(option["stops"]), 2)
        self.assertEqual({leg["mode"] for leg in option["legs"]}, {"bus", "rail"})
        rail_leg = next(leg for leg in option["legs"] if leg["mode"] == "rail")
        self.assertEqual(rail_leg["color"], "#047940")  # Kajang Line's official GTFS route_color
        for leg in option["legs"]:
            self.assertGreaterEqual(len(leg["points"]), 2, leg)
            for lat, lon in leg["points"]:
                self.assertTrue(1.0 < lat < 4.0, lat)  # sanity: within the Klang Valley
                self.assertTrue(100.0 < lon < 102.5, lon)

    def test_next_arrivals_places_the_stop(self):
        result = gtfs.next_arrivals("KL Sentral")
        mapstate.set_from_next_arrivals(result)
        state = mapstate.get()

        self.assertEqual(state["kind"], "stop")
        self.assertEqual(len(state["options"]), 1)
        self.assertEqual(state["options"][0]["stops"][0]["name"], "KL Sentral")

    def test_failed_result_does_not_touch_existing_state(self):
        mapstate.set_from_next_arrivals(gtfs.next_arrivals("KL Sentral"))
        before = mapstate.get()
        mapstate.set_from_plan_trip({"ok": False, "reason": "no_direct_route"})
        self.assertEqual(mapstate.get(), before)

    def test_no_live_eta_still_draws_the_structural_route(self):
        # The exact failure shape plan_trip returns when a route genuinely
        # exists but no bus is currently tracked close enough for an ETA —
        # the map should still draw the route, just with no live vehicles
        # on it, instead of showing nothing at all.
        result = {
            "ok": False,
            "reason": "no_buses_nearby",
            "message": "There's a route, but no buses are close enough to Fakulti Sains Komputer right now for an ETA.",
        }
        args = {"from_stop": "Fakulti Sains Komputer", "to_stop": "Pasar Seni (Platform B5)"}
        mapstate.set_from_plan_trip(result, args)
        state = mapstate.get()

        self.assertEqual(state["kind"], "trip")
        self.assertTrue(state["options"], state)
        self.assertTrue(any(leg["points"] for leg in state["options"][0]["legs"]))


if __name__ == "__main__":
    unittest.main()
