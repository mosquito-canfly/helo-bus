"""Regression tests for plan_trip's fare estimate (app/gtfs.py).

Pure logic only — _leg_fare/_fare_total/_fare_phrase never touch the
network, so these don't have the live-feed flakiness next_arrivals/plan_trip
themselves have.

The one real number here (MRTFEEDER_FARE_RM = RM1.00) is cited in
app/gtfs.py against Prasarana's own MRT Feeder Bus page — everything else
(rapid-bus-kl, rail) has no official flat rate to cite, so those legs must
report fare=None, never a guessed number.

Run: python tests/test_fare.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import gtfs  # noqa: E402


def _bus_leg(category: str, fare) -> dict:
    return {"mode": "bus", "route": "T815", "category": category, "board_at": "A", "alight_at": "B", "eta_human": "5 minutes", "fare": fare}


def _rail_leg() -> dict:
    return {"mode": "rail", "line": "Kajang Line", "direction": "Kajang", "from_station": "B", "to_station": "C", "stops": 3, "fare": None}


class FareLegTests(unittest.TestCase):
    def test_mrtfeeder_leg_has_the_official_flat_fare(self):
        self.assertEqual(gtfs._leg_fare("rapid-bus-mrtfeeder"), gtfs.MRTFEEDER_FARE_RM)

    def test_rapid_bus_kl_leg_fare_is_unknown_not_guessed(self):
        self.assertIsNone(gtfs._leg_fare("rapid-bus-kl"))

    def test_unrecognised_category_is_unknown(self):
        self.assertIsNone(gtfs._leg_fare(None))


class FareTripTests(unittest.TestCase):
    def test_trip_with_a_fully_known_fare(self):
        # Both legs mrtfeeder-only is contrived, but exercises the
        # all_known=True path cleanly.
        steps = [_bus_leg("rapid-bus-mrtfeeder", 1.0), _bus_leg("rapid-bus-mrtfeeder", 1.0)]
        total = gtfs._fare_total(steps)
        self.assertEqual(total, {"amount": 2.0, "all_known": True})
        phrase = gtfs._fare_phrase(steps)
        self.assertIn("RM 2.00 in total", phrase)
        self.assertNotIn("isn't in my data", phrase)

    def test_trip_with_one_unknown_leg_states_what_is_known(self):
        # The real Fakulti Sains Komputer -> Pasar Seni shape: a known
        # mrtfeeder bus leg, then a rail leg with no fare data.
        steps = [_bus_leg("rapid-bus-mrtfeeder", 1.0), _rail_leg()]
        total = gtfs._fare_total(steps)
        self.assertEqual(total, {"amount": 1.0, "all_known": False})
        phrase = gtfs._fare_phrase(steps)
        self.assertIn("RM 1.00", phrase)
        self.assertIn("isn't in my data", phrase)

    def test_trip_with_no_known_fare_says_nothing(self):
        # A rapid-bus-kl-only trip: never invent a number, and don't
        # clutter the message when there's nothing to report either.
        steps = [_bus_leg("rapid-bus-kl", None)]
        total = gtfs._fare_total(steps)
        self.assertEqual(total, {"amount": None, "all_known": False})
        self.assertEqual(gtfs._fare_phrase(steps), "")


if __name__ == "__main__":
    unittest.main()
