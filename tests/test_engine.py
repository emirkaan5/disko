"""The playback engine on synthetic PCM, no sound device: handoff, seek, cancel, pause, end, snapshots.

Every sample is tagged with where it came from (deck A, the transition buffer or deck B, and its
index), so the tests check exactly what reached the output."""
import unittest

import numpy as np

from live.audio import SR, Deck
from live.engine import Engine
from live.mixes import Scheduled
from live.sources.fake import FakeSource

STEP = 2.0 ** -24  # float32 holds every multiple of this below 1 exactly


def tagged(n: int, base: float, sign: float = 1.0) -> np.ndarray:
    x = (base + np.arange(n) * STEP) * sign
    return np.ascontiguousarray(np.stack([x, x], axis=1), dtype=np.float32)


def origin(v: float) -> tuple:
    """('A' | 'B' | 'T', index) for an output sample."""
    if v < 0:
        return "T", int(round(-v / STEP)) - 1  # buffers start at -STEP: -0.0 would read as deck A
    if v >= 0.5:
        return "B", int(round((v - 0.5) / STEP))
    return "A", int(round(v / STEP))


class Linear:
    """Automation stand-in: both decks run straight through the buffer."""

    def source_beat(self, deck, beat):
        return beat


def run(engine: Engine, frames: int, block: int = 1000) -> np.ndarray:
    out = np.zeros((frames, 2), np.float32)
    for i in range(0, frames, block):
        n = min(block, frames - i)
        engine._callback(out[i:i + n], n, None, None)
    return out


class EngineTest(unittest.TestCase):
    def setUp(self):
        a, b = FakeSource(n=2).library_tracks()
        self.a = Deck(a, tagged(4 * SR, 0.0), 1.0)
        self.b = Deck(b, tagged(4 * SR, 0.5), 1.0)
        self.engine = Engine(audio=False)
        self.engine.volume = 1.0
        self.engine.load(self.a)
        self.engine.play()

    def mix(self, start: int, length: int, b_offset: int) -> Scheduled:
        return Scheduled(start=start, buffer=tagged(length, STEP, -1.0), deck=self.b, b_offset=b_offset,
                         plan=None, auto=Linear(), spb=SR / 2)

    def test_exact_handoff(self):
        s = self.mix(start=SR, length=SR // 2, b_offset=1000)
        self.assertTrue(self.engine.schedule(s))
        out = run(self.engine, 2 * SR, block=777)  # blocks that don't line up with the joins
        tags = [origin(v) for v in out[:, 0]]
        expect = ([("A", i) for i in range(SR)] + [("T", i) for i in range(SR // 2)]
                  + [("B", 1000 + SR // 2 + i) for i in range(SR // 2)])
        self.assertEqual(tags, expect)
        self.assertIs(self.engine.deck, self.b)
        self.assertEqual(self.engine.pos, 1000 + SR)
        self.assertIsNone(self.engine.pending)

    def test_seek(self):
        run(self.engine, SR // 10)
        self.assertTrue(self.engine.seek(2 * SR))
        out = run(self.engine, 100)
        self.assertEqual(origin(out[0, 0]), ("A", 2 * SR))
        s = self.mix(start=3 * SR, length=SR // 4, b_offset=0)
        self.assertTrue(self.engine.schedule(s))
        self.assertFalse(self.engine.seek(3 * SR - SR // 2))  # too close to the queued mix
        run(self.engine, s.start - self.engine.pos + 10)
        self.assertTrue(self.engine.in_mix)
        self.assertFalse(self.engine.seek(0))                 # never during a transition

    def test_schedule_refused_when_already_passed(self):
        run(self.engine, SR)
        self.assertFalse(self.engine.schedule(self.mix(start=SR + SR // 8, length=100, b_offset=0)))
        self.assertIsNone(self.engine.pending)

    def test_cancel(self):
        s = self.mix(start=SR, length=SR // 4, b_offset=0)
        self.engine.schedule(s)
        self.assertTrue(self.engine.cancel())
        out = run(self.engine, SR + 500)
        self.assertEqual(origin(out[-1, 0]), ("A", SR + 499))  # deck A played straight on
        self.engine.schedule(self.mix(start=SR + SR // 2, length=SR // 4, b_offset=0))
        run(self.engine, SR // 2)
        self.assertTrue(self.engine.in_mix)
        self.assertFalse(self.engine.cancel())                 # a transition, once begun, plays out

    def test_pause_holds_position_and_outputs_silence(self):
        run(self.engine, 5000)
        self.engine.play(False)
        out = run(self.engine, 5000)
        self.assertFalse(out.any())
        self.assertEqual(self.engine.pos, 5000)
        self.engine.play()
        out = run(self.engine, 10)
        self.assertEqual(origin(out[0, 0]), ("A", 5000))

    def test_end_of_deck_stops_cleanly(self):
        self.engine.seek(self.a.length - 300)
        out = run(self.engine, 1000)
        self.assertFalse(self.engine.playing)
        self.assertEqual(origin(out[299, 0]), ("A", self.a.length - 1))
        self.assertFalse(out[300:].any())
        self.engine.play()
        self.assertFalse(self.engine.playing)  # nothing left to play

    def test_snapshot_follows_what_is_heard(self):
        s = self.mix(start=SR, length=SR // 2, b_offset=2000)
        self.engine.schedule(s)
        run(self.engine, SR // 2)
        snap = self.engine.snapshot()
        self.assertEqual((snap.deck, snap.pos, snap.sched), (self.a, SR // 2, None))
        run(self.engine, SR // 2 + SR // 4)
        snap = self.engine.snapshot()
        self.assertIs(snap.sched, s)
        self.assertEqual(snap.tpos, SR // 4)
        self.assertEqual(snap.b_pos, 2000 + SR // 4)
        run(self.engine, SR // 2)
        snap = self.engine.snapshot()
        self.assertIs(snap.deck, self.b)
        self.assertIsNone(snap.sched)
        self.assertEqual(snap.pos, 2000 + SR // 2 + SR // 4)

    def test_volume_and_clipping(self):
        self.engine.volume = 0.5
        out = run(self.engine, 10)
        self.assertAlmostEqual(float(out[9, 0]), 9 * STEP * 0.5, places=12)
        loud = Deck(self.a.track, np.full((1000, 2), 3.0, np.float32), 1.0)
        self.engine.load(loud)
        self.engine.play()
        self.assertEqual(float(run(self.engine, 10).max()), 1.0)


class SyntheticSet(unittest.TestCase):
    """Milestone 2's acceptance run on the synthetic source only (no partner audio exists here):
    two consecutive tracks, prepared through the policy, joined by one rendered transition."""

    def test_two_tracks_one_transition(self):
        from live.audio import load_deck, sync_ratio
        from live.mixes import TECHNIQUES, Plan, build

        src = FakeSource(n=2, seconds=40.0)
        a_track, b_track = src.library_tracks()
        a = load_deck(a_track)
        b = load_deck(b_track, sync_ratio(b_track, a.period))
        self.assertEqual(src.reads, [a_track.id, b_track.id])
        self.assertLess(abs(b.period / a.period - 1), 1e-3)  # beatmatched
        tech = TECHNIQUES["smooth_fade"]
        params = tech.defaults()
        params["bars"] = 4
        plan = Plan(technique="smooth_fade", params=params, sync=True, out_bar=5, in_bar=1)
        s = build(plan, a, b)
        engine = Engine(audio=False)
        engine.load(a)
        engine.play()
        self.assertTrue(engine.schedule(s))
        engine.advance(s.end + 2 * SR)
        self.assertIs(engine.deck, b)
        self.assertTrue(engine.playing)
        src.close()


if __name__ == "__main__":
    unittest.main()
