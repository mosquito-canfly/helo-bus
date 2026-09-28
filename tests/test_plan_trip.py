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


class RailStationNameFallbackTests(unittest.TestCase):
    """Regression for a real bug from a live call: "Pasar Seni" (the bare
    interchange name a caller actually says) has no single matching bus
    platform — it's ambiguous among 9+ "Pasar Seni Platform X" stops — so
    plan_trip used to error out with ambiguous_stop before ever checking
    that "Pasar Seni" names an unambiguous RAIL station (_resolve_rail_group)
    that the bus->rail fallback tier can anchor on directly."""

    def test_fakulti_sains_komputer_to_bare_pasar_seni_finds_a_route(self):
        result = gtfs.plan_trip("Fakulti Sains Komputer", "Pasar Seni")
        self.assertNotEqual(result.get("reason"), "ambiguous_stop", result)
        self.assertNotEqual(result.get("reason"), "no_direct_route", result)

    def test_perpustakaan_um_to_kl_sentral_finds_a_structural_route(self):
        # T789 (a route Perpustakaan Um also sits on) reaches a stop linked
        # to the Kelana Jaya Line, which serves KL Sentral directly. Only
        # asserts a route was found, not a live ETA — same reasoning as
        # RailInclusiveTests above.
        result = gtfs.plan_trip("Perpustakaan UM", "KL Sentral")
        self.assertNotIn(result.get("reason"), ("no_direct_route", "ambiguous_stop"), result)

    def test_kolej_kediaman_kesepuluh_to_kl_sentral_is_genuinely_unreachable(self):
        # Not a bug: T815's only rail-linked stop is Phileo Damansara, which
        # is solely on the Kajang Line — and KL Sentral is only linked to
        # the Kelana Jaya Line and the Monorail, neither of which is the
        # Kajang Line. Reaching KL Sentral from here needs a rail-to-rail
        # transfer, outside this app's one-transfer design (see plan_trip's
        # docstring). This documents that the rejection is correct, not a
        # routing bug — the agent's job here is next_arrivals + nearest rail
        # station, not a route that doesn't exist within one transfer.
        result = gtfs.plan_trip("Kolej Kediaman Kesepuluh", "KL Sentral")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "no_direct_route")
        # Real-call regression: T815 does reach a rail station (Phileo
        # Damansara, its own loop terminus) even though that station's line
        # doesn't help reach KL Sentral specifically — nearby_rail_stations
        # used to only ever report a WALKING-distance station (none here),
        # leaving the caller with nothing but "try a bigger hub". It should
        # now name Phileo Damansara as worth checking from there.
        self.assertIn("Phileo Damansara", result["nearby_rail_stations"])


if __name__ == "__main__":
    unittest.main()
