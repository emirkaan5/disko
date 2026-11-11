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
AUDIO_ON = GateStatus(True, True, True, ("BROWSE", "METADATA", "MIX"), "ready", audio=True, audio_ready=True)
AUDIO_UNSET = GateStatus(True, True, True, ("BROWSE", "METADATA", "MIX"), "ready", audio=True, audio_ready=False,
                         audio_missing="install the audio helper and sign in to Spotify for audio")
SIGNED_OUT = GateStatus(True, True, False, ("BROWSE", "METADATA"), "not signed in: run `login`", audio=False)
NO_CLIENT = GateStatus(True, False, False, ("BROWSE", "METADATA"), "no client ID is set", audio=False)


def setUpModule():
    from helpers import isolate_spotify_consent
    isolate_spotify_consent(unittest.addModuleCleanup)
    # The DJ-model page's choice and keys: a temp file and a dict, never ~/.disko or the Keychain.
    import tempfile
    from pathlib import Path
    from live import models
    from live.sources.secrets import MemoryStore
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    defaults = models.ModelStore.__init__.__defaults__
    models.ModelStore.__init__.__defaults__ = (Path(tmp.name) / "model.json", MemoryStore())
    unittest.addModuleCleanup(lambda: setattr(models.ModelStore.__init__, "__defaults__", defaults))

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
        self.assertIn("developer app", page.spot_state.text())
        self.assertTrue(page.spot_client.isVisible())          # pasted here, not in a terminal
        self.assertFalse(page.spot_signin.isVisible())      # there is nothing to sign in with yet
        self.assertEqual(page.spot_list.count(), 0)

    def test_search_narrows_the_playlists_by_every_word_typed(self):
        page = self.page(SIGNED_IN)
        shown = lambda: [c.name for c in page._spot_cards() if c.isVisible()]
        page.spot_search.setText("NIGHT late")
        self.assertEqual(shown(), ["late night drive"])
        page.spot_search.setText("nothing like this")
        self.assertEqual(shown(), [])
        self.assertTrue(page.spot_empty.isVisible())
        page.spot_search.clear()
        self.assertEqual(len(shown()), len(PLAYLISTS))
        self.assertFalse(page.spot_empty.isVisible())

    def test_a_cover_lands_on_its_card_and_a_failed_one_leaves_the_tile(self):
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QColor, QImage
        image = QImage(64, 64, QImage.Format.Format_RGB32)
        image.fill(QColor("#ff0000"))
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buf, "PNG")
        with mock.patch.object(WelcomeWindow, "_fetch_cover"):            # nothing leaves the machine
            page = self.page(SIGNED_IN, playlists=[("a", "With cover", 3, "https://i.scdn.co/a"),
                                                   ("b", "Broken cover", 4, "https://i.scdn.co/b"),
                                                   ("liked", "Liked Songs", 9)])
        self.assertTrue(page.cover_timer.isActive())
        page.cover_q.put(("https://i.scdn.co/a", bytes(data)))
        page.cover_q.put(("https://i.scdn.co/b", None))
        page._cover_poll()
        cards = {c.name: c for c in page._spot_cards()}
        self.assertIsNotNone(cards["With cover"].cover.pixmap)
        self.assertIsNone(cards["Broken cover"].cover.pixmap)
        self.assertTrue(cards["Liked Songs"].cover.liked)
        self.assertFalse(page.cover_timer.isActive())                       # nothing left to wait for
        self.assertIn("https://i.scdn.co/a", page.covers)                  # drawn again without asking

    def test_a_client_id_pasted_on_the_page_goes_to_the_keychain(self):
        page = self.page(NO_CLIENT, playlists=[])
        page.spot_client_id.setText("  abc123  ")
        with mock.patch.object(spotify, "KeychainStore") as store, \
                mock.patch.object(spotify, "status", return_value=SIGNED_OUT):
            page._spot_save_client()
        store.return_value.set.assert_called_once_with("client_id", "abc123")
        self.assertFalse(page.spot_client.isVisible())
        self.assertTrue(page.spot_signin.isVisible())          # the next step

    def test_audio_on_but_not_set_up_offers_the_setup_and_blocks_a_pick(self):
        page = self.page(AUDIO_UNSET)
        self.assertTrue(page.spot_setup.isVisible())
        self.assertIn("install the audio helper", page.spot_setup_what.text())
        self.assertIn("isn't set up yet", page.spot_state.text())
        with mock.patch.object(spotify, "load_policy") as policy:
            policy.return_value.allows.return_value = True
            page._spot_pick("pl1", "late night drive")
        self.assertIsNone(page.job)                            # nothing is pooled that couldn't be played
        self.assertIn("Finish setting up", page.spot_note.text())
        self.assertFalse(self.page(AUDIO_ON).spot_setup.isVisible())

    def test_the_setup_runs_off_the_window_thread_and_redraws_when_done(self):
        page = self.page(AUDIO_UNSET)
        from live.sources.spotify import helper

        def prepare(say, cancelled):
            say("Installing librespot and ffmpeg…")
            say("https://accounts.spotify.com/authorize?client_id=x")
        with mock.patch.object(helper, "prepare", side_effect=prepare), \
                mock.patch.object(spotify, "status", return_value=AUDIO_ON):
            page._spot_setup()
            self.assertFalse(page.spot_setup_go.isEnabled())
            deadline = time.time() + 5
            while page._spot_work and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
        self.assertEqual(page._spot_work, set())
        self.assertIsNone(page.setup_stop)
        self.assertFalse(page.spot_setup.isVisible())
        self.assertIn("set up", page.spot_note.text())
        self.assertEqual(page.spot_list.count(), len(PLAYLISTS))   # redrawn from what was there

    def test_a_failed_setup_says_why_and_can_be_tried_again(self):
        page = self.page(AUDIO_UNSET)
        from live.sources.spotify import helper
        with mock.patch.object(helper, "prepare", side_effect=helper.SetupFailed("installing needs uv")), \
                mock.patch.object(spotify, "status", return_value=AUDIO_UNSET):
            page._spot_setup()
            deadline = time.time() + 5
            while page._spot_work and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
        self.assertIn("needs uv", page.spot_setup_log.text())
        self.assertTrue(page.spot_setup_go.isEnabled())
        self.assertEqual(page.spot_setup_go.text(), "Try again")

    # ------------------------------------------------------------ the DJ's model

    def model_page(self, kev_ready=True):
        from live import models
        state = models.KevState(True, True, kev_ready)
        patch = mock.patch.object(models, "kev_state", return_value=state)
        patch.start()
        self.addCleanup(patch.stop)
        page = self.win.model_page
        page.store.path.unlink(missing_ok=True)
        page.store.keys.items.clear()
        self.win.show_model()
        return page, models

    def wait(self, page):
        deadline = time.time() + 5
        while page.work and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def test_the_home_page_says_which_model_djs(self):
        page, models = self.model_page(kev_ready=False)
        self.win.show_home()
        self.assertIn("isn't installed yet", self.win.dj_state.text())
        self.assertIn("Kev DJs", self.win.tag.text())
        page.store.write(models.ModelChoice("jev"))
        self.win.show_home()
        self.assertIn("no API key", self.win.dj_state.text())
        self.assertIn("Jev DJs", self.win.tag.text())

    def test_kev_not_installed_offers_the_install(self):
        page, _ = self.model_page(kev_ready=False)
        self.assertIs(self.win.pages.currentWidget(), page)
        self.assertTrue(page.kev_install.isVisible())
        self.assertIn("weights", page.kev.status.text())
        self.assertIn("isn't installed yet", page.state.text())
        self.assertEqual(page.kev.badge.text(), "IN USE")

    def test_installing_kev_runs_off_the_window_thread(self):
        page, models = self.model_page(kev_ready=False)

        def install(say, cancelled):
            say("Building Kev's environment…")
            return models.KevState(True, True, True)
        with mock.patch.object(models, "install_kev", side_effect=install):
            page.install_kev()
            self.assertFalse(page.kev_install.isEnabled())
            self.wait(page)
        self.assertIsNone(page.install_stop)
        self.assertFalse(page.kev_install.isVisible())
        self.assertIn("Installed", page.kev.status.text())

    def test_jev_needs_a_key_and_is_used_only_once_it_answers(self):
        page, models = self.model_page()
        page.use_jev()
        self.assertIn("API key first", page.jev.status.text())
        page.jev_key.setText("sk-bad")
        with mock.patch.object(models, "check", return_value="the API key was refused"):
            page.use_jev()
            self.wait(page)
        self.assertEqual(page.store.read().kind, "kev")              # not switched
        self.assertIn("refused", page.jev.status.text())
        self.assertEqual(page.store.key("jev"), "sk-bad")            # kept, so it can be fixed and checked again
        page.jev_key.setText("sk-good")
        with mock.patch.object(models, "check", return_value=""):
            page.use_jev()
            self.wait(page)
        self.assertEqual(page.store.read().kind, "jev")
        self.assertEqual(page.jev.badge.text(), "IN USE")
        self.assertEqual(page.jev_key.text(), "")                    # the key isn't left on screen
        self.assertTrue(page.kev_use.isVisible())                    # and Kev can be chosen again

    def test_a_custom_server_needs_an_address(self):
        page, models = self.model_page()
        page.custom_url.setText("box:8010")
        page.use_custom()
        self.assertIn("http://", page.custom.status.text())
        page.custom_url.setText("http://box:8010/")
        page.custom_model.setText("laya-en")
        with mock.patch.object(models, "check", return_value=""):
            page.use_custom()
            self.wait(page)
        self.assertEqual(page.store.read(), models.ModelChoice("custom", "http://box:8010", "laya-en"))

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
