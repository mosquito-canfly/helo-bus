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

    def test_pasar_seni_is_ambiguous_across_platforms(self):
        result = gtfs.find_stop("Pasar Seni")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result.get("ambiguous"), result)
        self.assertTrue(any("Pasar Seni" in c for c in result["candidates"]))

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
