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

from app import gtfs, mapstate, store  # noqa: E402

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
        state = mapstate.get(store.active_call_id())

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
        # Synthetic result, not a live gtfs.next_arrivals() call — this
        # tests set_from_next_arrivals's own placement logic, not whether a
        # bus happens to be tracked near KL Sentral right now (that used to
        # make this test flaky outside service hours or on a live-feed gap).
        result = {"ok": True, "stop": "KL Sentral", "arrivals": [{"route": "T789", "eta_seconds": 180, "eta_human": "3 minutes", "category": "rapid-bus-kl"}]}
        mapstate.set_from_next_arrivals(result)
        state = mapstate.get(store.active_call_id())

        self.assertEqual(state["kind"], "stop")
        self.assertEqual(len(state["options"]), 1)
        self.assertEqual(state["options"][0]["stops"][0]["name"], "KL Sentral")

    def test_failed_result_does_not_touch_existing_state(self):
        mapstate.set_from_next_arrivals({"ok": True, "stop": "KL Sentral", "arrivals": [{"route": "T789", "eta_seconds": 180, "eta_human": "3 minutes", "category": "rapid-bus-kl"}]})
        before = mapstate.get(store.active_call_id())
        mapstate.set_from_plan_trip({"ok": False, "reason": "no_direct_route"})
        self.assertEqual(mapstate.get(store.active_call_id()), before)

    def test_a_different_or_stale_call_id_never_sees_this_state(self):
        # The actual bug this session fixes: map state (and events, and
        # location) used to be one global slot any visitor's poll would
        # read — a fresh page load, or another caller's stale tab, could
        # see someone else's trip. See store.py's module docstring. Uses a
        # synthetic result (not a live next_arrivals call) for the same
        # reason test_bus_plus_rail_trip_has_real_polylines does — this
        # isn't testing live-ETA behaviour, just the call_id gate.
        call_id = store.start_call()
        mapstate.set_from_next_arrivals({"ok": True, "stop": "KL Sentral", "arrivals": [{"route": "T789", "eta_seconds": 180, "eta_human": "3 minutes", "category": "rapid-bus-kl"}]})

        self.assertEqual(mapstate.get(call_id)["kind"], "stop")
        self.assertEqual(mapstate.get("some-other-call-id"), {"kind": None, "options": []})
        self.assertEqual(mapstate.get(None), {"kind": None, "options": []})

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
        state = mapstate.get(store.active_call_id())

        self.assertEqual(state["kind"], "trip")
        self.assertTrue(state["options"], state)
        self.assertTrue(any(leg["points"] for leg in state["options"][0]["legs"]))

    def test_find_stop_places_the_resolved_stop(self):
        mapstate.set_from_find_stop({"ok": True, "stop": "KL Sentral", "is_station": False, "message": "Found KL Sentral."})
        state = mapstate.get(store.active_call_id())
        self.assertEqual(state["kind"], "stop")
        self.assertEqual(state["options"][0]["stops"][0]["name"], "KL Sentral")

    def test_find_stop_ambiguous_result_has_nothing_to_place(self):
        # No single "stop" key — a list of candidates isn't a map point, so
        # this must leave whatever state already existed untouched (same
        # reasoning as test_failed_result_does_not_touch_existing_state).
        mapstate.set_from_find_stop({"ok": True, "stop": "KL Sentral", "is_station": False, "message": "Found KL Sentral."})
        before = mapstate.get(store.active_call_id())
        mapstate.set_from_find_stop({"ok": True, "ambiguous": True, "candidates": ["A", "B"], "message": "Which one?"})
        self.assertEqual(mapstate.get(store.active_call_id()), before)

    def test_find_nearby_stops_places_every_stop_found(self):
        result = {"ok": True, "nearby": [
            {"stop": "KL Sentral", "distance_meters": 50, "walk_minutes": 1},
            {"stop": "Pasar Seni (Platform B5)", "distance_meters": 300, "walk_minutes": 4},
        ], "message": "Nearest stops: KL Sentral, Pasar Seni."}
        mapstate.set_from_find_nearby_stops(result)
        state = mapstate.get(store.active_call_id())
        self.assertEqual(state["kind"], "stop")
        self.assertEqual({s["name"] for s in state["options"][0]["stops"]}, {"KL Sentral", "Pasar Seni (Platform B5)"})


if __name__ == "__main__":
    unittest.main()
