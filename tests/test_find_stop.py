"""Regression tests for stop-name matching (app/gtfs.py).

Real GTFS data, no mocking — needs .gtfs_cache/ populated (the app's own
first run does this; downloads it if missing).

Run: python tests/test_find_stop.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import gtfs  # noqa: E402


class FindStopTests(unittest.TestCase):
    def test_university_of_malaya(self):
        result = gtfs.find_stop("University of Malaya")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stop"], "Perpustakaan Um")

    def test_um_abbreviation(self):
        result = gtfs.find_stop("UM")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stop"], "Perpustakaan Um")

    def test_universiti_malaya(self):
        result = gtfs.find_stop("Universiti Malaya")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stop"], "Perpustakaan Um")

    def test_kl_central_english_malay_translation(self):
        result = gtfs.find_stop("KL Central")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stop"], "KL Sentral")

    def test_damansara_single_word_is_ambiguous_not_wrong(self):
        result = gtfs.find_stop("Damansara")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result.get("ambiguous"), result)
        self.assertGreaterEqual(len(result["candidates"]), 2)
        self.assertLessEqual(len(result["candidates"]), 5)

    def test_pasar_seni_resolves_to_the_station_not_an_ambiguous_platform_list(self):
        # Bare "Pasar Seni" exactly names the rail station but only ever
        # CONTAINS-matched 9+ numbered bus platforms ("Pasar Seni (Platform
        # B5)", etc) — an exact rail match now wins over that weaker bus
        # tier, so a caller gets one clean, useful answer instead of a list
        # of platform codes nobody says out loud (see _best_match).
        result = gtfs.find_stop("Pasar Seni")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result.get("is_station"), result)
        self.assertEqual(result["stop"], "Pasar Seni")

    def test_garbled_query_never_returns_a_confident_false_match(self):
        # This exact input previously Soundexed "Malaya"-style noise into a
        # false "Found X" — a wrong confident answer is worse than a guess.
        result = gtfs.find_stop("Fidudaman SARA")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "not_found")
        if "candidates" in result:
            self.assertLessEqual(len(result["candidates"]), 3)

    def test_pure_noise_gets_no_guess_at_all(self):
        result = gtfs.find_stop("xyzzy quibble nonsense")
        self.assertFalse(result["ok"], result)
        self.assertNotIn("candidates", result)

    def test_muzium_negara_finds_the_rail_station_not_a_word_intersection_false_positive(self):
        # Regression: no bus stop is literally named "Muzium Negara" — only
        # "Muzium Tekstil Negara", which used to win via the bus word-tier
        # (both "muzium" and "negara" are real bus words) before find_stop
        # ever checked whether the RAIL namespace had an exact match.
        result = gtfs.find_stop("Muzium Negara")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result.get("is_station"), result)
        self.assertEqual(result["stop"], "Muzium Negara")

    def test_muzium_negara_mishearings_favour_the_station_over_the_wrong_bus_stop(self):
        # Real STT mishearings from a live call. "Moudium"/"Mudium" clear the
        # rail close-match tier cleanly; "Miojim Nagara" is genuinely
        # ambiguous with "Masjid Negara" at the string level, so it's only
        # asserted to surface Muzium Negara as a candidate, not as the sole
        # confident answer.
        for query in ("Moudium Nagara", "Mudium Negara"):
            result = gtfs.find_stop(query)
            self.assertTrue(result["ok"], (query, result))
            self.assertEqual(result["stop"], "Muzium Negara", (query, result))

        result = gtfs.find_stop("Miojim Nagara")
        candidates = result.get("candidates") or ([result["stop"]] if result.get("ok") else [])
        self.assertTrue(any("Muzium Negara" in c for c in candidates), result)

    def test_national_museum_alias_resolves_to_the_station(self):
        # "national"->"negara" and "museum"->"muzium" translate in word
        # order, producing "negara muzium" — which doesn't exact/contains
        # match the rail station's "muzium negara" key, so this relies on
        # the explicit alias (data/aliases.json), not just translation.
        result = gtfs.find_stop("National Museum")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result.get("is_station"), result)
        self.assertEqual(result["stop"], "Muzium Negara")

    def test_alias_lookup_is_not_broken_by_abbreviation_translation(self):
        # Regression: ALIASES used to be checked only AFTER ABBREVIATIONS
        # translation ("street"->"jalan", "gardens"->"taman"), so any alias
        # key containing one of those words could never match — "Petaling
        # Street" normalized to "petaling jalan", which isn't a key in
        # aliases.json, and silently fell through to unrelated JLN stops.
        result = gtfs.find_stop("Petaling Street")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stop"], "Pasar Seni")

        result = gtfs.find_stop("The Gardens")
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("Mid Valley" in c for c in result.get("candidates", [result.get("stop", "")])))

    def test_faculty_of_computer_science_translation(self):
        # Regression: "of" is a real word (part of "Commission OF India"),
        # which used to block the word-intersection down to nothing and
        # fall back to a noisy union across every meaning of every word.
        result = gtfs.find_stop("Faculty of Computer Science")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result.get("ambiguous"), result)
        self.assertTrue(all("Komputer" in c for c in result["candidates"]), result)


if __name__ == "__main__":
    unittest.main()
