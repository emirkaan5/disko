"""The headless session: Kev DJing a synthetic set, commands, thread ownership, failures and source loss."""
import threading
import unittest

from live.audio import SR
from live.session import SessionError, WrongThread
from live.sources import Operation
from live.sources.fake import fake_policy

from helpers import fake_session, run_until


class AutopilotSet(unittest.TestCase):
    def test_kev_queues_and_mixes_into_the_next_track(self):
        session, engine, src, events = fake_session()
        reply = session.cmd_start(None)
        self.assertEqual(reply["playback"]["device"], None)  # no sound card in tests
        self.assertEqual(reply["sources"][0]["provider"], "fake")
        self.assertTrue(run_until(session, engine, lambda: session.state == "queued"))
        s = session.scheduled
        status = session.status()
        self.assertEqual(status["next"]["id"], str(s.deck.track.id))
        self.assertGreater(status["next"]["starts_in_s"], 0)
        engine.advance(s.start - engine.pos)  # straight to the mix
        self.assertTrue(run_until(session, engine, lambda: len(session.played) == 2))
        self.assertIs(session.current, s.deck)
        kinds = [k for k, _ in events]
        for kind in ("loading", "opened", "thinking", "queued", "promoted"):
            self.assertIn(kind, kinds)
        self.assertTrue(run_until(session, engine, lambda: session.state == "queued"))  # and on to the third

    def test_opens_on_the_calmest_track_or_the_one_asked_for(self):
        session, engine, src, _ = fake_session()
        calm = min(session.library.tracks, key=lambda t: t.energy)
        self.assertEqual(session.cmd_start(None)["starting"]["id"], str(calm.id))
        session2, *_ = fake_session()
        self.assertEqual(session2.cmd_start("Test Tone 3")["starting"]["title"], "Test Tone 3")
        with self.assertRaises(SessionError):
            session2.cmd_start("nothing like this")

    def test_cannot_start_twice(self):
        session, engine, *_ = fake_session()
        session.cmd_start(None)
        run_until(session, engine, lambda: session.state == "playing")
        with self.assertRaises(SessionError):
            session.cmd_start(None)


class Commands(unittest.TestCase):
    def setUp(self):
        self.session, self.engine, self.src, self.events = fake_session()
        self.session.cmd_start(None)
        self.assertTrue(run_until(self.session, self.engine, lambda: self.session.state == "queued"))

    def test_energy(self):
        s = self.session.cmd_energy("up")
        self.assertEqual(s["energy"], "lift")
        self.assertEqual(self.session.dj.lift, 1)
        with self.assertRaises(SessionError):
            self.session.cmd_energy("sideways")
        with self.assertRaises(SessionError):
            self.session.cmd_energy(True)

    def test_pick_again_drops_the_queued_mix(self):
        first = self.session.scheduled.deck.track
        self.session.cmd_pick_again()
        self.assertIsNone(self.session.scheduled)
        self.assertIsNone(self.engine.pending)
        self.assertTrue(run_until(self.session, self.engine, lambda: self.session.state == "queued"))
        self.assertIsNot(self.session.scheduled.deck.track, first)

    def test_pause_resume(self):
        self.session.cmd_pause()
        self.assertFalse(self.engine.playing)
        pos = self.engine.pos
        self.session.tick()
        self.assertEqual(self.engine.pos, pos)
        self.session.cmd_resume()
        self.assertTrue(self.engine.playing)

    def test_stop_and_start_again(self):
        s = self.session.cmd_stop()
        self.assertEqual(s["state"], "stopped")
        self.assertFalse(self.engine.playing)
        self.assertIsNone(self.engine.pending)
        self.assertIsNone(self.session.scheduled)
        self.assertEqual(self.session.cmd_stop()["state"], "stopped")  # stopping twice is harmless
        with self.assertRaises(SessionError):
            self.session.cmd_pick_again()
        self.session.cmd_resume()  # a stopped set resumes its current track; Kev picks again
        self.assertTrue(run_until(self.session, self.engine, lambda: self.session.state == "queued"))
        self.session.cmd_stop()
        self.session.cmd_start("Test Tone 2")
        self.assertTrue(run_until(self.session, self.engine, lambda: self.session.current.track.title == "Test Tone 2"))


class Threads(unittest.TestCase):
    def test_commands_from_other_threads_run_on_the_owner(self):
        session, engine, *_ = fake_session()
        result, errors = {}, []

        def other():
            try:
                session.stop()
            except WrongThread as e:
                errors.append(e)
            result["start"] = session.call("start", None).result(timeout=30)
            result["status"] = session.call("status").result(timeout=30)

        t = threading.Thread(target=other)
        t.start()
        run_until(session, engine, lambda: not t.is_alive(), timeout=30)
        t.join()
        self.assertEqual(len(errors), 1)
        self.assertIn("starting", result["start"])
        self.assertIn(result["status"]["state"], ("start", "playing", "queued"))

    def test_unknown_commands_refused(self):
        session, *_ = fake_session()
        with self.assertRaises(SessionError):
            session.call("open").result(timeout=1)


class Failures(unittest.TestCase):
    def test_unreadable_pick_is_skipped_not_retried(self):
        session, engine, src, events = fake_session(n=2, failures={"fake:t01": "decode"})
        session.cmd_start("Test Tone 1")
        self.assertTrue(run_until(session, engine,
                                  lambda: (session.notice or ("",))[0].startswith("That's every track"), timeout=60))
        self.assertIn("fake:t01", session.dj.broken)
        dj = [d["kind"] for k, d in events if k == "dj"]
        self.assertEqual(dj.count("skipped"), 1)
        asked = session.kev.asked
        run_until(session, engine, lambda: False, timeout=0.5)
        self.assertEqual(session.kev.asked, asked)  # nothing more to decide: no retry loop

    def test_expired_access_stops_the_set(self):
        session, engine, src, events = fake_session(n=2, failures={"fake:t01": "expired"})
        session.cmd_start("Test Tone 1")
        self.assertTrue(run_until(session, engine, lambda: session.state == "stopped", timeout=60))
        self.assertFalse(engine.playing)
        self.assertIn("access to the music was lost", session.notice[0])
        self.assertIn("source_lost", [k for k, _ in events])

    def test_disconnect_stops_and_refuses_new_reads(self):
        session, engine, src, events = fake_session()
        session.cmd_start(None)
        self.assertTrue(run_until(session, engine, lambda: session.state == "queued"))
        session.disconnect(src)
        self.assertEqual(session.state, "stopped")
        self.assertFalse(engine.playing)
        self.assertIsNone(engine.pending)
        reads = list(src.reads)
        with self.assertRaises(SessionError):
            session.cmd_start(None)  # the source is gone: refused before any read
        self.assertEqual(src.reads, reads)

    def test_policy_refuses_before_loading(self):
        session, engine, src, _ = fake_session(policy=fake_policy(allow={Operation.BROWSE, Operation.METADATA}))
        with self.assertRaises(SessionError):
            session.cmd_start(None)
        run_until(session, engine, lambda: False, timeout=0.3)
        self.assertEqual(src.reads, [])
        self.assertEqual(session.state, "start")


if __name__ == "__main__":
    unittest.main()
