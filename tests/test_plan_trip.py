"""Regression tests for trip planning (app/gtfs.py plan_trip), direct bus
and rail-inclusive (LRT/MRT/Monorail/BRT, one transfer max).

DirectRouteOptionsTests exercises the pure route-matching logic
(_direct_route_options) — no network. RailInclusiveTests calls the real
plan_trip() for named real-world routes and only asserts that a STRUCTURAL
route was found (reason != "no_direct_route"), not that a live bus ETA was
available for it — the same reason next_arrivals itself has no test here;
which specific buses are tracked live varies minute to minute.

Run: python tests/test_plan_trip.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import gtfs  # noqa: E402

gtfs._load_static()


def _group_for_stop(stop_id: str):
    for group in gtfs._groups_by_name.values():
        if stop_id in group.stop_ids:
            return group
    return None


class DirectRouteOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Any real route's first and last stop are a guaranteed direct pair.
        for path in gtfs._route_paths.values():
            if len(path) < 2:
                continue
            start, end = _group_for_stop(path[0]), _group_for_stop(path[-1])
            if start and end and start.name != end.name:
                cls.from_group, cls.to_group = start, end
                break
        else:
            raise RuntimeError("no route with 2+ distinct stops in static data")

    def test_route_endpoints_are_a_direct_option(self):
        options = gtfs._direct_route_options(self.from_group, self.to_group)
        self.assertTrue(options, "expected the route's own endpoints to match directly")

    def test_plan_trip_rejects_same_stop(self):
        result = gtfs.plan_trip(self.from_group.name, self.from_group.name)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "same_stop")


class RailInclusiveTests(unittest.TestCase):
    def test_fakulti_sains_komputer_to_pasar_seni_finds_a_route(self):
        # No direct bus between these two; only findable via the bus->rail
        # fallback tier (a feeder to an MRT station, then the Kajang Line).
        result = gtfs.plan_trip("Fakulti Sains Komputer", "Pasar Seni (Platform B5)")
        self.assertNotEqual(result.get("reason"), "no_direct_route", result)

    def test_kl_sentral_to_mid_valley_finds_a_route(self):
        result = gtfs.plan_trip("KL Sentral", "Mid Valley (Selatan)")
        self.assertNotEqual(result.get("reason"), "no_direct_route", result)

    def test_unconnected_stops_report_no_direct_route(self):
        result = gtfs.plan_trip("Bandar Kajang", "Taman Botani Putrajaya")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "no_direct_route")


if __name__ == "__main__":
    unittest.main()
