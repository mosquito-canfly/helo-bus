"""Regression test for per-call scoping of map-state/events/location.

The bug: map state and the tool-call event log used to be one global slot
any visitor's poll would read — a fresh page load, or another caller's
stale browser tab, could see someone else's trip. See app/store.py's
module docstring for the scoping approach this fixes it with, and its one
real limit (only one call is ever "active" at a time).

Uses store.log_event directly rather than a real /tools/* call through
TestClient, so this doesn't also write a row into the persistent
data/insights.db (main.py's middleware calls insights.log_event too, which
this test has no business polluting).

Run: python tests/test_call_scoping.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import gtfs, mapstate, store  # noqa: E402
from app.main import app  # noqa: E402

gtfs._load_static()
client = TestClient(app)

_ARRIVAL_RESULT = {
    "ok": True,
    "stop": "KL Sentral",
    "arrivals": [{"route": "T789", "eta_seconds": 120, "eta_human": "2 minutes", "category": "rapid-bus-kl"}],
}


class CallScopingTests(unittest.TestCase):
    def test_call_start_mints_a_fresh_id_each_time(self):
        a = client.post("/api/call/start").json()["call_id"]
        b = client.post("/api/call/start").json()["call_id"]
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)

    def test_map_state_only_answers_to_its_own_call_id(self):
        call_id = client.post("/api/call/start").json()["call_id"]
        mapstate.set_from_next_arrivals(_ARRIVAL_RESULT)

        mine = client.get(f"/api/map-state?call_id={call_id}").json()
        self.assertEqual(mine["kind"], "stop")

        other = client.get("/api/map-state?call_id=someone-elses-id").json()
        self.assertIsNone(other["kind"])
        nobody = client.get("/api/map-state").json()
        self.assertIsNone(nobody["kind"])

    def test_a_new_call_start_resets_map_state_and_location(self):
        first = client.post("/api/call/start").json()["call_id"]
        mapstate.set_from_next_arrivals(_ARRIVAL_RESULT)
        client.post("/api/location", json={"lat": 3.14, "lon": 101.68, "call_id": first})
        self.assertEqual(client.get(f"/api/map-state?call_id={first}").json()["location"], {"lat": 3.14, "lon": 101.68})

        second = client.post("/api/call/start").json()["call_id"]
        self.assertNotEqual(first, second)
        fresh = client.get(f"/api/map-state?call_id={second}").json()
        self.assertIsNone(fresh["kind"])
        self.assertIsNone(fresh["location"])
        # And the first call's own id no longer sees its data either, once
        # a second call has started — matches "keep the panel ... until
        # ... a new call starts (which resets it)".
        stale = client.get(f"/api/map-state?call_id={first}").json()
        self.assertIsNone(stale["kind"])
        self.assertIsNone(stale["location"])

    def test_events_are_scoped_to_the_call_that_produced_them(self):
        first = client.post("/api/call/start").json()["call_id"]
        store.log_event("/tools/get_now", b"{}", b'{"ok": true}')  # a tool call, logged against whichever call is active
        events_for_first = client.get(f"/api/events?since=0&call_id={first}").json()["events"]
        self.assertTrue(events_for_first)

        second = client.post("/api/call/start").json()["call_id"]
        events_for_second = client.get(f"/api/events?since=0&call_id={second}").json()["events"]
        self.assertEqual(events_for_second, [])

        stale_read_of_first = client.get(f"/api/events?since=0&call_id={first}").json()["events"]
        self.assertEqual(stale_read_of_first, [])  # start_call() clears the log


if __name__ == "__main__":
    unittest.main()
