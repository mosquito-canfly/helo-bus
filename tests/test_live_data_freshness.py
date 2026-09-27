"""Regression test for a real bug: next_arrivals answered "no buses nearby"
for Fakulti Sains Komputer, then 35s later correctly found the bus. Root
cause — one category's live fetch succeeding set a single global "ok" flag
that masked the OTHER category (the one the query actually needed) having
failed/never fetched, so an unusable answer read as a confident "nothing's
close" instead of "I can't tell yet".

No network here — these exercise the pure decision logic in
app/gtfs.py directly, with the module's live-polling state stubbed.

Run: python tests/test_live_data_freshness.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import gtfs  # noqa: E402

gtfs._load_static()


class LiveDataFreshnessTests(unittest.TestCase):
    def setUp(self):
        self._vehicles_backup = dict(gtfs._vehicles)
        self._category_ok_backup = dict(gtfs._category_ok)
        gtfs._vehicles.clear()
        gtfs._category_ok.clear()

    def tearDown(self):
        gtfs._vehicles.clear()
        gtfs._vehicles.update(self._vehicles_backup)
        gtfs._category_ok.clear()
        gtfs._category_ok.update(self._category_ok_backup)

    def test_one_category_ok_does_not_mask_the_needed_one_failing(self):
        # rapid-bus-kl fetched fine this round; rapid-bus-mrtfeeder (what
        # the query actually needs) didn't — this must read as unavailable,
        # not as "confirmed, nothing nearby".
        gtfs._category_ok["rapid-bus-kl"] = True
        gtfs._category_ok["rapid-bus-mrtfeeder"] = False
        error = gtfs._live_data_error({"rapid-bus-mrtfeeder"}, True, "6:00 am")
        self.assertIsNotNone(error)
        self.assertEqual(error["reason"], "feed_unavailable")

    def test_needed_category_ok_returns_no_error(self):
        gtfs._category_ok["rapid-bus-kl"] = True
        gtfs._category_ok["rapid-bus-mrtfeeder"] = False
        self.assertIsNone(gtfs._live_data_error({"rapid-bus-kl"}, True, "6:00 am"))

    def test_stale_fetch_with_still_cached_vehicle_counts_as_data(self):
        # This round's attempt failed, but a vehicle from an earlier
        # success is still cached (not yet evicted) — that's usable, not
        # a reason to claim the feed is down.
        gtfs._category_ok["rapid-bus-kl"] = False
        route_id = next(iter(gtfs._routes))
        category = gtfs._routes[route_id]["category"]
        gtfs._vehicles["veh1"] = {
            "route_id": route_id,
            "direction_id": 0,
            "lat": 0.0,
            "lon": 0.0,
            "speed_kmh": None,
            "seen_at": 0.0,
        }
        self.assertIsNone(gtfs._live_data_error({category}, True, "6:00 am"))

    def test_never_fetched_and_nothing_cached_is_unavailable(self):
        error = gtfs._live_data_error({"rapid-bus-kl"}, True, "6:00 am")
        self.assertIsNotNone(error)
        self.assertEqual(error["reason"], "feed_unavailable")


if __name__ == "__main__":
    unittest.main()
