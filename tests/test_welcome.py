"""The welcome screen's Spotify page (SPOTIFY_INTEGRATION_PLAN.md, Milestone 6), offscreen and with no
network: which state shows what, that the opt-in goes through the same call the terminal uses, and that
a pool build drives the progress page to a set the way a folder index does. Nothing here asks Spotify
anything: the playlists and the sign-in state are handed to the page as they would be after a read.
"""
import os
import queue
import sys
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # no display in a test run

from PySide6.QtCore import Qt                # noqa: E402  (after the platform is chosen)
from PySide6.QtWidgets import QApplication

from live.library import Library
from live.sources import spotify
from live.sources.fake import FakeSource
from live.sources.pool import Pool, track_from_info
from live.sources.spotify import GateStatus
from live.ui.welcome import WelcomeWindow
from unittest import mock

PLAYLISTS = [("liked", "Liked Songs", 812), ("37i9dQZF1DXcBWIGoYBM5M", "Today's Top Hits", 50),
             ("2v3iNvBX8Ay1Gt2uXtUKUT", "late night drive", None)]
SIGNED_IN = GateStatus(True, True, True, ("BROWSE", "METADATA"), "ready", audio=False)
AUDIO_ON = GateStatus(True, True, True, ("BROWSE", "METADATA", "MIX"), "ready", audio=True)
SIGNED_OUT = GateStatus(True, True, False, ("BROWSE", "METADATA"), "not signed in: run `login`", audio=False)
NO_CLIENT = GateStatus(True, False, False, ("BROWSE", "METADATA"), "no client ID is set", audio=False)


class StubPoolBuild:
    """The PoolBuild protocol by hand: a queue of events, and what the set needs when it finishes."""

    def __init__(self, result, pool, total, name="Fake list", error=None):
        self.events, self.result, self.pool, self.error = queue.Queue(), result, pool, error
        self.todo, self.name, self.cancelled = total, name, False

    @property
    def total(self) -> int:
        return self.todo

    def cancel(self):
        self.cancelled = True


class WindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def setUp(self):
        self.win = WelcomeWindow(None)
        self.win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)   # isVisible() works, nothing flashes
        self.win.show()
        self.addCleanup(self.win.close)
        self.ready = []
        self.win.ready_set.connect(self.ready.append)

    def page(self, state, playlists=PLAYLISTS):
        self.win.show_spotify(playlists, state=state)
        return self.win

    # ------------------------------------------------------------ the page

    def test_the_home_page_offers_spotify_and_back_returns_to_it(self):
        home = self.win.home
        self.win.show_spotify(PLAYLISTS, state=SIGNED_IN)
        self.assertIs(self.win.pages.currentWidget(), self.win.spot_page)
        self.win.show_home([])            # the page's Back button does this
        self.assertIs(self.win.pages.currentWidget(), home)

    def test_signed_in_with_audio_off_shows_the_warning_and_the_playlists(self):
        page = self.page(SIGNED_IN)
        self.assertTrue(page.spot_warning.isVisible())     # what it means, before any audio is read
        self.assertFalse(page.spot_signin.isVisible())
        self.assertFalse(page.spot_off.isVisible())
        self.assertEqual(page.spot_list.count(), len(PLAYLISTS))
        self.assertIn("audio is off", page.spot_state.text())

    def test_audio_on_hides_the_warning_and_offers_going_back_to_browsing(self):
        page = self.page(AUDIO_ON)
        self.assertFalse(page.spot_warning.isVisible())
        self.assertTrue(page.spot_off.isVisible())
        self.assertIn("fetched only as it comes up", page.spot_state.text())

    def test_signed_out_offers_the_sign_in_and_no_playlists(self):
        page = self.page(SIGNED_OUT)
        self.assertTrue(page.spot_signin.isVisible())
        self.assertFalse(page.spot_warning.isVisible())
        self.assertEqual(page.spot_list.count(), 0)

    def test_no_client_id_says_where_one_comes_from(self):
        page = self.page(NO_CLIENT, playlists=[])
        self.assertIn("set-client", page.spot_state.text())
        self.assertFalse(page.spot_signin.isVisible())      # there is nothing to sign in with yet
        self.assertEqual(page.spot_list.count(), 0)

    def test_the_choice_the_page_records_is_the_listener_s_own(self):
        with mock.patch.object(spotify, "set_audio") as set_audio:
            self.win._spot_audio(True)
            self.win._spot_audio(False)
        self.assertEqual([call.kwargs for call in set_audio.call_args_list], [{"audio": True}, {"audio": False}])

    # ------------------------------------------------------------ a pool being built

    def tracks(self, n=3):
        source = FakeSource(n=n)
        return [track_from_info(info, source) for info in source.tracks("all", 0, n).items]

    def start(self, job):
        self.win.job = job
        self.win.pages.setCurrentWidget(self.win.index)
        self.win.timer.start(80)

    def test_the_page_counts_the_clips_and_shows_each_one_as_it_lands(self):
        tracks = self.tracks()
        self.start(StubPoolBuild((Library(tracks, name="Fake list"), None), Pool(tracks, [], False), len(tracks)))
        self.win.job.events.put(("start", {"event": "start", "tracks": len(tracks), "todo": len(tracks)}))
        for i, track in enumerate(tracks):
            self.win.job.events.put(("done", {"event": "done", "rel": f"{i}.mp3", "title": track.title,
                                              "artist": track.artist, "bpm": 124.0, "key": "C major",
                                              "arousal": 6.0}))
        self.win._poll()
        self.assertEqual((self.win._done, self.win._failed), (len(tracks), 0))
        self.assertEqual(self.win.count.text(), f"{len(tracks)} of {len(tracks)}")
        self.assertEqual(self.win.feed.count(), len(tracks))
        self.win.job.events.put(("finished", {}))
        self.win._poll()
        self.assertIsNone(self.win.job)                     # done: the page is polling nothing
        self.assertTrue(self.win.heading.text().startswith("“Fake list” is ready: 3 tracks"))
        deadline = time.time() + 5
        while not self.ready and time.time() < deadline:     # it starts on a timer, note readable first
            self.app.processEvents()
            time.sleep(0.02)
        self.assertEqual(len(self.ready), 1)
        self.assertEqual(self.ready[0][0].name, "Fake list")

    def test_a_playlist_that_could_not_be_read_says_so_instead_of_hanging(self):
        self.start(StubPoolBuild(None, None, 0, error="not signed in to Spotify: run `login`"))
        self.win.job.events.put(("finished", {}))
        self.win._poll()
        self.assertEqual(self.win.heading.text(), "The playlist couldn't be read")
        self.assertIn("not signed in", self.win.note.text())
        self.assertTrue(self.win.back.isVisible() and not self.win.cancel.isVisible())
        self.assertEqual(self.ready, [])

    def test_opening_a_folder_forgets_a_pool_that_was_built(self):
        """One page polls whichever of the two is set, so a folder must clear a pool and the reverse."""
        import tempfile
        from pathlib import Path

        from live.ui import welcome
        tracks = self.tracks()
        self.start(StubPoolBuild((Library(tracks, name="Fake list"), None), Pool(tracks, [], False), len(tracks)))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "list"
            folder.mkdir()
            for name in ("a.mp3", "b.mp3"):                  # only the names matter to the indexer
                (folder / name).write_bytes(b"not really audio")
            with mock.patch.object(welcome, "FastIndexer"):   # no analysis runs; the page is the subject
                self.win.open(str(folder))
        self.assertIsNone(self.win.job)
        self.assertIsNotNone(self.win.indexer)
        self.win.timer.stop()


if __name__ == "__main__":
    unittest.main()
