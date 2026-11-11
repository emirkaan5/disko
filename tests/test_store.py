"""Analysis for tracks without a file (SPOTIFY_INTEGRATION_PLAN.md, Milestone 3): the session temp
folder, the per-provider analysis store, and both passes on the synthetic source, with stub helpers
standing in for Essentia and allin1 (tests/stub_workers). Checks that decoded audio is on disk only
while a helper reads it, and gone after.
"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from live import indexer
from live.indexer import DeepAnalyzer, FullPass, SourceIndexer
from live.sources import Operation, Policy, PolicyDenied
from live.sources.fake import FakeSource, fake_policy
from live.sources.store import SessionTemp, SourceStore

STUBS = Path(__file__).resolve().parent / "stub_workers"


def seen(store: SourceStore) -> list:
    path = store.analysis / "seen.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


class Stubbed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.temp = SessionTemp(parent=self.tmp.name)
        self.addCleanup(self.temp.close)
        for name, value in (("FAST_PYTHON", Path(sys.executable)), ("DEEP_PYTHON", Path(sys.executable)),
                            ("WORKERS", STUBS)):
            patcher = mock.patch.object(indexer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def audio_left(self) -> list:
        folder = self.temp.dir / "audio"
        return sorted(folder.iterdir()) if folder.exists() else []


class PolicyTest(unittest.TestCase):
    def test_temp_audio_needs_its_own_flag_or_permanent_retention(self):
        base = dict(provider="x", allowed=frozenset(Operation), agreement={"reference": "test"})
        self.assertFalse(Policy(**base).allows(Operation.TEMP_AUDIO))
        self.assertTrue(Policy(**base, audio_in_session_temp=True).allows(Operation.TEMP_AUDIO))
        self.assertFalse(Policy(**base, audio_in_session_temp=True).allows(Operation.PERSIST_AUDIO))
        self.assertTrue(Policy(**base, audio_on_disk=True).allows(Operation.TEMP_AUDIO))
        self.assertTrue(Policy.local().allows(Operation.TEMP_AUDIO))

    def test_matrix_field(self):
        matrix = {"provider": "x", "agreement": {"reference": "test"}, "allow": ["DECODE", "TEMP_AUDIO"],
                  "retention": {"audio_in_session_temp": True}}
        self.assertTrue(Policy.from_matrix(matrix).allows(Operation.TEMP_AUDIO))
        matrix["retention"] = {}
        self.assertFalse(Policy.from_matrix(matrix).allows(Operation.TEMP_AUDIO))


class SessionTempTest(Stubbed):
    def test_files_go_when_closed_when_the_source_disconnects_and_on_exit(self):
        src = FakeSource(n=3, seconds=12.0)
        a, b = src.library_tracks()[:2]
        first = self.temp.decode(a)
        self.assertTrue(first.path.exists())
        first.close()
        self.assertFalse(first.path.exists())
        second = self.temp.decode(b)
        src.close()  # disconnect: whatever it still had here is deleted
        self.assertFalse(second.path.exists())
        self.assertEqual(self.temp.files(), [])
        folder = self.temp.dir
        self.temp.close()
        self.assertFalse(folder.exists())

    def test_refused_without_temp_permission_before_anything_is_read(self):
        src = FakeSource(n=2, policy=fake_policy(audio_in_session_temp=False))
        with self.assertRaises(PolicyDenied):
            self.temp.decode(src.library_tracks()[0])
        self.assertEqual(src.reads, [])


class StoreTest(Stubbed):
    def test_store_lives_in_temp_unless_analysis_may_be_kept(self):
        kept = SourceStore.for_source(FakeSource(n=1, policy=fake_policy(analysis_on_disk=True)), self.temp,
                                      root=Path(self.tmp.name) / "stores")
        self.assertTrue(kept.persistent)
        self.assertEqual(kept.dir, Path(self.tmp.name) / "stores" / "fake")
        passing = SourceStore.for_source(FakeSource(n=1), self.temp)
        self.assertFalse(passing.persistent)
        self.assertTrue(passing.dir.is_relative_to(self.temp.dir))

    def test_unsafe_keys_get_file_safe_names(self):
        store = SourceStore("spotify", Path(self.tmp.name), False)
        t = FakeSource(n=1).library_tracks()[0]
        self.assertEqual(store.key(t), "t00")
        from live.library import Track
        local_file = Track(uid="spotify:spotify:local:a:b:c:1", provider="spotify")
        self.assertRegex(store.key(local_file), r"^[0-9a-f]{20}$")


class FastPassTest(Stubbed):
    def test_every_track_analyzed_through_temp_and_nothing_left(self):
        src = FakeSource(n=6, seconds=12.0, missing={"fake:t01": ("energy", "mood")})
        tracks = src.library_tracks()
        store = SourceStore.for_source(src, self.temp)
        before = {t.uid: t.bpm for t in tracks}
        counts = SourceIndexer(store, tracks, self.temp, workers=2).run()
        self.assertEqual(counts, {"done": 6, "failed": 0})
        self.assertTrue(all(r["there"] for r in seen(store)))           # the helper found each file...
        self.assertEqual(self.audio_left(), [])                         # ...and none is left
        self.assertTrue(all(len(t.rough_beats) == 300 for t in tracks))  # the grid comes from the audio
        self.assertEqual({t.uid: t.bpm for t in tracks}, before)        # the source's own tempo stays
        t1 = next(t for t in tracks if t.uid == "fake:t01")
        self.assertEqual(t1.energy, 6.0)                                # an unknown is filled in
        self.assertIn("fast pass", t1.provenance["energy"])
        with open(store.csv) as f:
            self.assertEqual(len(list(csv.DictReader(f))), 6)

    def test_analyzed_once(self):
        src = FakeSource(n=3, seconds=12.0, policy=fake_policy(analysis_on_disk=True))
        store = SourceStore.for_source(src, self.temp, root=Path(self.tmp.name) / "stores")
        SourceIndexer(store, src.library_tracks(), self.temp).run()
        reads = len(src.reads)
        again = FakeSource(n=3, seconds=12.0, policy=fake_policy(analysis_on_disk=True))
        tracks = again.library_tracks()
        indexer2 = SourceIndexer(SourceStore.for_source(again, self.temp, root=Path(self.tmp.name) / "stores"),
                                 tracks, self.temp)
        self.assertEqual(indexer2.todo, [])
        self.assertEqual(again.reads, [])
        self.assertTrue(all(t.rough_beats for t in tracks))
        self.assertEqual(reads, 3)

    def test_failures_are_reported_remembered_and_cleaned_up(self):
        src = FakeSource(n=4, seconds=12.0, failures={"fake:t02": "decode", "fake:t03": "region"})
        tracks = [t for t in src.library_tracks() if t.availability.playable]
        store = SourceStore.for_source(src, self.temp)
        failed = []
        counts = SourceIndexer(store, tracks, self.temp).run(lambda k, i: k == "failed" and failed.append(i))
        self.assertEqual(counts, {"done": 2, "failed": 1})
        self.assertIn("couldn't decode", failed[0]["error"])
        self.assertIn("fake:t02", store.skipped())
        self.assertEqual(store.pending(tracks), [])
        self.assertEqual(self.audio_left(), [])

    def test_denied_tracks_are_never_decoded(self):
        src = FakeSource(n=3, seconds=12.0, policy=fake_policy(audio_in_session_temp=False))
        store = SourceStore.for_source(src, self.temp)
        idx = SourceIndexer(store, src.library_tracks(), self.temp)
        self.assertEqual(idx.todo, [])
        self.assertEqual(idx.run(), {"done": 0, "failed": 0})
        self.assertEqual(src.reads, [])


class DeepPassTest(Stubbed):
    def test_a_streamed_track_is_mapped_through_temp(self):
        src = FakeSource(n=3, seconds=12.0)
        tracks = src.library_tracks()
        store = SourceStore.for_source(src, self.temp)
        SourceIndexer(store, tracks, self.temp).run()
        analyzer = DeepAnalyzer(store, self.temp)
        self.addCleanup(analyzer.close)
        t = tracks[0]
        analyzer.analyze(t).result(timeout=20)
        self.assertTrue(t.deep)
        self.assertEqual([s.label for s in t.sections], ["intro", "chorus", "outro"])
        self.assertEqual(t.first_drop_bar, 17)
        self.assertEqual(t.n_bars, 64)
        request = seen(store)[-1]
        self.assertTrue(request["there"])
        self.assertTrue(Path(request["tmp"]).is_relative_to(self.temp.dir))  # stems stay in the session folder
        self.assertEqual(self.audio_left(), [])
        self.assertEqual(analyzer.analyze(t).result(timeout=1), 0.0)       # mapped: not decoded again

    def test_refused_without_a_temp_folder_or_permission(self):
        src = FakeSource(n=2, seconds=12.0)
        store = SourceStore.for_source(src, self.temp)
        no_temp = DeepAnalyzer(store)
        self.addCleanup(no_temp.close)
        with self.assertRaises(PolicyDenied):
            no_temp.analyze(src.library_tracks()[0]).result(timeout=1)
        strict = FakeSource(n=2, seconds=12.0, policy=fake_policy(audio_in_session_temp=False))
        analyzer = DeepAnalyzer(store, self.temp)
        self.addCleanup(analyzer.close)
        with self.assertRaises(PolicyDenied):
            analyzer.analyze(strict.library_tracks()[0]).result(timeout=1)
        self.assertIsNone(analyzer.proc)
        self.assertEqual(src.reads + strict.reads, [])

    def test_unreadable_track_fails_and_leaves_nothing(self):
        src = FakeSource(n=2, seconds=12.0, failures={"fake:t01": "decode"})
        store = SourceStore.for_source(src, self.temp)
        analyzer = DeepAnalyzer(store, self.temp)
        self.addCleanup(analyzer.close)
        with self.assertRaises(RuntimeError):
            analyzer.analyze(src.library_tracks()[1]).result(timeout=10)
        self.assertEqual(self.audio_left(), [])


class PreviewAndFullPassTest(Stubbed):
    """Milestones 4 and 5, steps 1 to 3: a clip's features are estimates, the track's own replace
    them as soon as it is picked, and the grid only ever comes from the whole track."""

    RESULTS = {"version": 1, "path": "fake:t00", "bytes": 10, "tags": {}, "duration": 30.0, "bpm": 128.0,
               "beats": [i * 0.5 for i in range(30)], "key": "A", "scale": "minor", "key_strength": 0.7,
               "loudness_short_term_mean": -8.0, "onset_rate": 3.0, "spectral_centroid_mean": 2100.0,
               "danceability": 1.2, "voice": 0.3, "valence": 5.5, "arousal": 6.5,
               "styles": {"Electronic---House": 0.8}}

    def setUp(self):
        super().setUp()
        self.src = FakeSource(n=1, seconds=12.0, missing={"fake:t00": ("bpm", "camelot", "energy")})
        self.track = self.src.library_tracks()[0]
        self.store = SourceStore.for_source(self.src, self.temp)

    def test_a_clip_fills_in_only_what_the_source_left_unknown_and_never_the_grid(self):
        mood = self.track.mood
        self.store.apply_preview(self.track, self.RESULTS, "itunes")
        self.assertEqual(self.track.bpm, 128.0)
        self.assertEqual(self.track.camelot, "8A")          # A minor, from the clip's key
        self.assertEqual(self.track.energy, 6.5)
        self.assertEqual(self.track.provenance["bpm"], "30 s preview (itunes)")
        self.assertIn("nothing from the whole track yet", self.track.provenance["features"])
        self.assertFalse(self.track.rough_beats)            # a clip says nothing about the grid
        self.assertEqual(self.track.mood, mood)             # what the source knew is not touched
        self.assertEqual(self.track.provenance["mood"], "fake source")
        self.assertEqual(self.track.duration, 12.0)         # the source's own length is not overwritten
        self.assertIsNotNone(self.store.preview(self.track))
        self.assertEqual(self.store.pending([self.track]), [])   # the full pass waits for the pick
        self.assertTrue(self.store.preview_only(self.track))

    def test_a_saved_clip_is_applied_again_without_being_analyzed_again(self):
        self.store.apply_preview(self.track, self.RESULTS, "deezer")
        fresh = self.src.library_tracks()[0]
        again = SourceStore.for_source(self.src, self.temp)
        again.apply_preview(fresh, again.preview(fresh), "deezer")
        self.assertEqual(fresh.bpm, 128.0)
        self.assertEqual(fresh.provenance["bpm"], "30 s preview (deezer)")

    def test_the_whole_track_replaces_the_estimates_and_brings_the_grid_with_it(self):
        self.store.apply_preview(self.track, self.RESULTS, "itunes")
        full = FullPass(self.store)
        self.addCleanup(full.close)
        with self.temp.decode(self.track) as audio:
            self.assertTrue(full.run(self.track, audio.path))
        self.assertAlmostEqual(self.track.bpm, 123.0 + len(self.store.key(self.track)) % 5)  # the stub's pass
        self.assertNotAlmostEqual(self.track.bpm, 128.0)     # the clip's estimate is gone
        self.assertEqual(self.track.provenance["bpm"], "fast pass v1 on the full track")
        self.assertEqual(len(self.track.rough_beats), 300)  # the grid now covers the whole track
        self.assertFalse(self.store.preview_only(self.track))
        with self.temp.decode(self.track) as audio:
            self.assertFalse(full.run(self.track, audio.path))  # nothing left to replace
        self.assertEqual(self.audio_left(), [])

    def test_a_full_pass_without_a_clip_first_changes_nothing(self):
        full = FullPass(self.store)
        self.addCleanup(full.close)
        with self.temp.decode(self.track) as audio:
            self.assertFalse(full.run(self.track, audio.path))  # the source's own values stand
        self.assertFalse(self.track.rough_beats)
        self.assertIsNone(self.track.bpm)
        self.assertIsNone(self.track.provenance.get("bpm"))

    def test_a_pick_decodes_once_and_runs_both_passes_on_it(self):
        self.store.apply_preview(self.track, self.RESULTS, "itunes")
        analyzer = DeepAnalyzer(self.store, self.temp)
        self.addCleanup(analyzer.close)
        analyzer.analyze(self.track).result(timeout=30)
        self.assertTrue(self.track.deep)                    # the deep pass ran...
        self.assertEqual(self.track.provenance["bpm"], "fast pass v1 on the full track")  # ...after the full one
        self.assertEqual(len(self.track.rough_beats), 300)
        self.assertEqual(self.src.reads, [self.track.id])   # one decode, for both passes
        self.assertEqual(self.audio_left(), [])


if __name__ == "__main__":
    unittest.main()
