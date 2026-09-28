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

    def test_kolej_kediaman_kesepuluh_to_kl_sentral_via_rail_interchange(self):
        # Real route from a live call: T815 -> Phileo Damansara MRT -> ride
        # the Kajang Line -> Muzium Negara -> walk the ~350-500m walkway to
        # KL Sentral. This needs a rail LINE CHANGE (Kajang Line only
        # reaches Muzium Negara, not KL Sentral directly) plus a walking
        # leg _rail_journeys/_walk_extended model — genuinely unreachable
        # before that support existed (see git history for the prior
        # "genuinely unreachable" version of this test).
        # Structural check via debug_trip (see _debug_step_summary), which
        # doesn't depend on a live bus ETA existing right now — same
        # reasoning as RailInclusiveTests above.
        debug = gtfs.debug_trip("Kolej Kediaman Kesepuluh", "KL Sentral")
        self.assertGreater(debug["rail_inclusive_options_count"], 0, debug)
        modes = [s["mode"] for s in debug["rail_inclusive_options"][0]]
        self.assertEqual(modes, ["bus", "rail", "walk"], debug["rail_inclusive_options"][0])
        walk_step = debug["rail_inclusive_options"][0][2]
        self.assertEqual(walk_step["to"], "KL Sentral")
        self.assertLessEqual(walk_step["meters"], 500)

        result = gtfs.plan_trip("Kolej Kediaman Kesepuluh", "KL Sentral")
        self.assertNotEqual(result.get("reason"), "no_direct_route", result)


class RailInterchangeTests(unittest.TestCase):
    """Regression for the one-line-change rail graph (_rail_journeys /
    _walk_links / _interchange_groups) added after real calls hit trips
    that need a genuine line change or a short interchange walk."""

    def test_kl_sentral_to_pasar_seni_prefers_the_direct_line_no_detour(self):
        # KL Sentral is directly on the Kelana Jaya Line, one stop from
        # Pasar Seni — a real regression had this drown in worse rail-
        # inclusive candidates (e.g. via LRT Abdullah Hukum) instead of
        # surfacing the obvious direct hop. Only the direct option should
        # survive: the dominance prune must drop a strictly-worse 2nd
        # option (more transfers, more or equal stops).
        result = gtfs.plan_trip("KL Sentral", "Pasar Seni")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["options"]), 1)
        steps = result["options"][0]["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["mode"], "rail")
        self.assertEqual(steps[0]["line"], "Kelana Jaya Line")
        for opt in result["options"]:
            self.assertNotIn("Abdullah Hukum", " ".join(s.get("from_station", "") + s.get("to_station", "") for s in opt["steps"]))

    def test_one_line_change_via_same_name_interchange(self):
        # Masjid Jamek is one RailStationGroup spanning 3 lines (Ampang,
        # Kelana Jaya, Sungai Buloh-Kajang) — a free interchange, no walk.
        masjid_jamek = gtfs._rail_group_by_display_name["Masjid Jamek"]
        self.assertGreaterEqual(len({l for sid in masjid_jamek.station_ids for l in gtfs._station_lines.get(sid, set())}), 2)


if __name__ == "__main__":
    unittest.main()
