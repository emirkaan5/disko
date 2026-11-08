"""Ranking and Kev's cards: local parity with the old ranking, and unknowns staying unknown."""
import json
import math
import unittest
from pathlib import Path

from live.dj import Cards
from live.library import WEIGHTS, Library, Track
from live.sources.fake import FakeSource

FIXTURE = Path(__file__).parent / "fixtures" / "rank_house.json"


class LocalParity(unittest.TestCase):
    """The house set ranks exactly as it did before sources existed (fixture from commit 7f2106d)."""

    @classmethod
    def setUpClass(cls):
        cls.fx = json.loads(FIXTURE.read_text())
        cls.lib = Library()

    def test_rankings_unchanged(self):
        for case in self.fx["cases"]:
            ranked = self.lib.rank(self.lib.by_uid[case["current"]], case["master_bpm"], set(case["played"]),
                                   case["lift"])
            self.assertEqual(len(ranked), case["count"])
            got = [[c.track.uid, round(c.score, 10), round(c.stretch, 10), c.key_label] for c in ranked[:25]]
            self.assertEqual(got, case["top"], f"{case['current']} lift {case['lift']}")
            self.assertTrue(all(c.unknown == () for c in ranked))

    def test_percentiles_unchanged(self):
        for feature, values in self.fx["percentiles"].items():
            got = [round(self.lib.percentile(self.lib.tracks[i], feature), 10) for i in (0, 100, 300)]
            self.assertEqual(got, values, feature)
        self.assertEqual(round(self.lib.spread("energy"), 10), self.fx["spread_energy"])


class Unknowns(unittest.TestCase):
    def setUp(self):
        src = FakeSource(n=10, missing={"fake:t02": ("energy", "mood", "busyness", "brightness", "vocals",
                                                     "danceability"),
                                        "fake:t03": ("key_strength",),
                                        "fake:t04": ("bpm",),
                                        "fake:t05": ("camelot",)})
        self.tracks = {t.uid: t for t in src.library_tracks()}
        self.lib = Library(list(self.tracks.values()), name="fake")

    def test_missing_values_are_none_not_defaults(self):
        t = self.tracks["fake:t02"]
        for f in ("energy", "mood", "vocals"):
            self.assertIsNone(getattr(t, f))
        self.assertIsNone(self.lib.percentile(t, "energy"))
        self.assertNotIn("energy", t.provenance)

    def test_below_the_minimum_isnt_ranked(self):
        reasons = dict((t.uid, why) for t, why in self.lib.unrankable)
        self.assertEqual(reasons, {"fake:t04": "unknown bpm", "fake:t05": "unknown camelot"})
        uids = {c.track.uid for c in self.lib.rank(self.tracks["fake:t00"], 122.0, set())}
        self.assertNotIn("fake:t04", uids)
        self.assertNotIn("fake:t05", uids)
        self.assertIn("fake:t02", uids)

    def test_unknown_signals_are_left_out_and_weights_renormalized(self):
        ranked = {c.track.uid: c for c in self.lib.rank(self.tracks["fake:t00"], 122.0, set())}
        blind = ranked["fake:t02"]
        self.assertIsNone(blind.energy_delta)
        self.assertIn("energy", blind.unknown)
        self.assertIn("vibe", blind.unknown)
        self.assertIn("runway", blind.unknown)  # nothing here has been through allin1
        for c in ranked.values():
            self.assertTrue(0.0 <= c.score <= 1.0)
            self.assertFalse(math.isnan(c.score))
        # a pair scored on tempo, key and genre alone: the weighted mean of just those
        known = [w for name, w in WEIGHTS.items() if name not in blind.unknown]
        self.assertAlmostEqual(sum(known), WEIGHTS["tempo"] + WEIGHTS["key"] + WEIGHTS["genre"])

    def test_unknown_key_strength_trusts_the_key(self):
        c = next(c for c in self.lib.rank(self.tracks["fake:t03"], 122.0, set()))
        self.assertIsNotNone(c.key_score)

    def test_cards_say_unknown(self):
        cards = Cards(self.lib)
        a, b = self.tracks["fake:t00"], self.tracks["fake:t02"]
        vibe = cards.vibe(b)
        for word in ("energy unknown", "mood unknown", "vocals unknown", "groove unknown", "sound unknown"):
            self.assertIn(word, vibe)
        self.assertIn("energy change unknown", cards.fit(122.0, a, b))
        self.assertNotIn("unknown", cards.vibe(a))

    def test_everything_unknown_library(self):
        tracks = [Track(uid=f"bare:{i}", provider="bare", bpm=120.0 + i, camelot="8A",
                        availability=FakeSource(n=1).library_tracks()[0].availability) for i in range(3)]
        lib = Library(tracks, name="bare")
        ranked = lib.rank(tracks[0], 120.0, set())
        self.assertEqual(len(ranked), 2)
        self.assertEqual(set(ranked[0].unknown), {"energy", "vibe", "runway"})
        self.assertIsNone(lib.spread("energy"))

    def test_undersized_library_ends_instead_of_repeating(self):
        lib = Library([self.tracks["fake:t00"], self.tracks["fake:t01"]], name="tiny")
        self.assertEqual(lib.rank(self.tracks["fake:t00"], 122.0, {"fake:t01"}), [])


if __name__ == "__main__":
    unittest.main()
