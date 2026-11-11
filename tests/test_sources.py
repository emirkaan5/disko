"""Sources and policy: ids, fail-closed permissions, deny-before-use, failures and paging.

    .venv-live/bin/python -m unittest discover tests
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from live.audio import load_deck, native_audio
from live.indexer import DeepAnalyzer
from live.library import Library, Track
from live.playlist import Playlist
from live.sources import (AuthExpired, Availability, Operation, Policy, PolicyDenied, SourceError, SourceTimeout,
                          TrackId, TrackUnavailable, open_track_audio, policy_for)
from live.sources import beatport
from live.sources.fake import FakeSource, fake_policy
from live.sources.local import LocalSource


class TrackIds(unittest.TestCase):
    def test_parse_and_compare(self):
        a = TrackId.parse("fake:t01")
        self.assertEqual(a, TrackId("fake", "t01"))
        self.assertEqual(str(a), "fake:t01")
        self.assertNotEqual(TrackId("fake", "abc"), TrackId("local", "abc"))
        with self.assertRaises(ValueError):
            TrackId.parse("no-provider")
        with self.assertRaises(ValueError):
            TrackId("", "x")

    def test_local_and_fake_tracks_never_collide(self):
        local = Track(uid="t00", title="Same key, local", bpm=122.0, camelot="8A")
        fake = FakeSource(n=2).library_tracks()
        self.assertEqual(fake[0].id, TrackId("fake", "t00"))
        self.assertNotEqual(local.id, fake[0].id)
        self.assertNotEqual(local.uid, fake[0].uid)
        lib = Library([local] + fake, name="mixed")
        self.assertIs(lib.by_id[TrackId("local", "t00")], local)
        self.assertIs(lib.by_id[TrackId("fake", "t00")], fake[0])

    def test_uid_rules(self):
        with self.assertRaises(ValueError):
            Track(uid="local:abc")         # a local uid is the bare key
        with self.assertRaises(ValueError):
            Track(uid="t00", provider="fake")  # anyone else's is the full id

    def test_library_rejects_duplicate_ids(self):
        a = Track(uid="x1", bpm=120.0, camelot="8A")
        b = Track(uid="x1", bpm=124.0, camelot="9A")
        with self.assertRaises(ValueError):
            Library([a, b], name="dupes")


class Policies(unittest.TestCase):
    def test_beatport_default_is_browsing_only(self):
        p = beatport.default_policy()
        self.assertEqual({op for op in Operation if p.allows(op)},
                         {Operation.BROWSE, Operation.METADATA, Operation.SEND_TO_MODEL})
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(beatport.load_policy(Path(d)), p)  # no file: the built-in default
            (Path(d) / "beatport.json").write_text(json.dumps({"provider": "beatport", "agreement": None}))
            restricted = beatport.load_policy(Path(d))       # a file replaces it, and fails closed
        self.assertFalse(any(restricted.allows(op) for op in Operation))
        self.assertFalse(beatport.status(restricted).usable)
        with self.assertRaises(PolicyDenied):
            beatport.connect(restricted)

    def test_matrix_without_agreement_grants_nothing(self):
        p = Policy.from_matrix({"provider": "x", "allow": ["DECODE", "ANALYZE"]})
        self.assertFalse(p.allows(Operation.DECODE))
        with self.assertRaises(PolicyDenied) as err:
            p.require(Operation.DECODE, TrackId("x", "1"))
        self.assertIn("no written authorization", str(err.exception))

    def test_matrix_grants_only_what_it_lists(self):
        p = Policy.from_matrix({"provider": "x", "agreement": {"reference": "letter 2027-01"},
                                "allow": ["BROWSE", "DECODE", "PERSIST_ANALYSIS"],
                                "retention": {"analysis_on_disk": False, "handle_ttl_s": 60}})
        self.assertTrue(p.allows(Operation.BROWSE))
        self.assertTrue(p.allows(Operation.DECODE))
        self.assertFalse(p.allows(Operation.MIX))
        self.assertFalse(p.allows(Operation.PERSIST_ANALYSIS))  # listed, but retention says not on disk
        self.assertEqual(p.handle_ttl, 60.0)

    def test_bad_matrices_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(Policy.load("nobody", Path(d)).allows(Operation.BROWSE))
            (Path(d) / "x.json").write_text("{not json")
            self.assertFalse(Policy.load("x", Path(d)).allows(Operation.BROWSE))
            (Path(d) / "y.json").write_text(json.dumps({"provider": "y", "agreement": {"reference": "r"},
                                                        "allow": ["BROWSE", "TELEPORT"]}))
            p = Policy.load("y", Path(d))
            self.assertFalse(p.allows(Operation.BROWSE))
            self.assertIn("TELEPORT", p.reason)
            (Path(d) / "z.json").write_text(json.dumps({"provider": "other", "agreement": {"reference": "r"},
                                                        "allow": ["BROWSE"]}))
            self.assertFalse(Policy.load("z", Path(d)).allows(Operation.BROWSE))

    def test_trackless_provider_is_denied(self):
        t = Track(uid="ghost:1", provider="ghost", bpm=120.0, camelot="8A")
        self.assertFalse(policy_for(t).allows(Operation.DECODE))
        with self.assertRaises(PolicyDenied):
            open_track_audio(t)


class DenyBeforeUse(unittest.TestCase):
    """A denied operation must fail before any audio is generated, decoded or written."""

    def test_decode_denied(self):
        src = FakeSource(n=3, policy=fake_policy(allow={Operation.BROWSE, Operation.METADATA}))
        t = src.library_tracks()[0]
        with self.assertRaises(PolicyDenied):
            src.open_audio(t.id)
        with self.assertRaises(PolicyDenied):
            native_audio(t)
        with self.assertRaises(PolicyDenied):
            load_deck(t)
        self.assertEqual(src.reads, [])

    def test_mixing_denied_even_when_decoding_is_allowed(self):
        src = FakeSource(n=2, policy=fake_policy(allow={Operation.DECODE, Operation.ANALYZE}))
        t = src.library_tracks()[0]
        with self.assertRaises(PolicyDenied):
            load_deck(t)
        self.assertEqual(src.reads, [])

    def test_allin1_never_sees_a_streamed_track_without_a_session_temp_folder(self):
        src = FakeSource(n=2)  # everything but disk retention
        t = src.library_tracks()[0]

        class NoPlaylist:  # DeepAnalyzer must refuse before it touches the playlist or spawns a helper
            def __getattr__(self, name):
                raise AssertionError(f"touched the playlist ({name})")

        analyzer = DeepAnalyzer(NoPlaylist())
        try:
            future = analyzer.analyze(t)
            with self.assertRaises(PolicyDenied):
                future.result(timeout=1)
            self.assertIsNone(analyzer.proc)
        finally:
            analyzer._closed = True
        self.assertEqual(src.reads, [])

    def test_browse_denied(self):
        src = FakeSource(n=2, policy=Policy.deny_all("fake", "test"))
        with self.assertRaises(PolicyDenied):
            src.playlists()
        with self.assertRaises(PolicyDenied):
            src.tracks("all")
        with self.assertRaises(PolicyDenied):
            src.info(TrackId("fake", "t00"))

    def test_foreign_ids_refused(self):
        src = FakeSource(n=2)
        with self.assertRaises(SourceError):
            src.open_audio(TrackId("local", "t00"))


class Failures(unittest.TestCase):
    def setUp(self):
        self.src = FakeSource(n=8, failures={"fake:t01": "region", "fake:t02": "timeout", "fake:t03": "expired",
                                             "fake:t04": "decode", "fake:t05": "entitlement"})
        self.tracks = {t.uid: t for t in self.src.library_tracks()}

    def test_availability_reported(self):
        self.assertIs(self.tracks["fake:t01"].availability, Availability.REGION_RESTRICTED)
        self.assertIs(self.tracks["fake:t05"].availability, Availability.ENTITLEMENT_REQUIRED)
        self.assertTrue(self.tracks["fake:t00"].mixable)
        self.assertFalse(self.tracks["fake:t01"].mixable)

    def test_errors_surface_as_source_errors(self):
        with self.assertRaises(TrackUnavailable):
            self.src.read_audio(TrackId("fake", "t01"))
        with self.assertRaises(SourceTimeout):
            self.src.read_audio(TrackId("fake", "t02"))
        with self.assertRaises(AuthExpired):
            self.src.read_audio(TrackId("fake", "t03"))
        with self.assertRaises(SourceError):
            self.src.read_audio(TrackId("fake", "t04"))
        self.assertEqual(self.src.reads, [])

    def test_unavailable_tracks_are_skipped_not_retried(self):
        lib = Library(list(self.tracks.values()), name="fake")
        ranked = lib.rank(self.tracks["fake:t00"], 122.0, set())
        uids = {c.track.uid for c in ranked}
        self.assertNotIn("fake:t01", uids)
        self.assertNotIn("fake:t05", uids)
        self.assertIn("fake:t02", uids)  # a timeout is transient: still a candidate
        reasons = dict((t.uid, why) for t, why in lib.unrankable)
        self.assertEqual(reasons["fake:t01"], "region restricted")

    def test_handles_expire_and_close_on_disconnect(self):
        src = FakeSource(n=2, policy=fake_policy(handle_ttl=0.05))
        h = src.open_audio(TrackId("fake", "t00"))
        time.sleep(0.08)
        with self.assertRaises(AuthExpired):
            h.read()
        h2 = src.open_audio(TrackId("fake", "t01"))
        src.close()
        with self.assertRaises(SourceError):
            h2.read()
        self.assertEqual(src.reads, [])


class Paging(unittest.TestCase):
    def test_pages_cover_the_playlist_once(self):
        src = FakeSource(n=23)
        seen, offset = [], 0
        while offset is not None:
            page = src.tracks("all", offset, 10)
            seen += [i.id for i in page.items]
            offset = page.next_offset
        self.assertEqual(seen, src.ids)
        with self.assertRaises(ValueError):
            src.tracks("all", 0, 0)
        with self.assertRaises(ValueError):
            src.tracks("all", 0, 10_000)


class LocalFolder(unittest.TestCase):
    def test_folder_as_source(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "set"
            folder.mkdir()
            tone = 0.1 * np.sin(2 * np.pi * 220 * np.arange(44100) / 44100).astype(np.float32)
            sf.write(folder / "01 - Some Artist - Some Title.wav", tone, 44100)
            pl = Playlist(folder)
            src = pl.source
            self.assertIsInstance(src, LocalSource)
            (ref,) = src.playlists()
            page = src.tracks(ref.id)
            self.assertEqual(page.total, 1)
            info = page.items[0]
            self.assertEqual((info.title, info.artist), ("Some Title", "Some Artist"))
            self.assertEqual(info.features, {})  # nothing analyzed yet, so nothing is claimed
            x, sr = src.read_audio(info.id)
            self.assertEqual((sr, len(x)), (44100, 44100))
            self.assertFalse((folder / "disko").exists())  # browsing and reading write nothing


if __name__ == "__main__":
    unittest.main()
