"""How long the deep helper may spend on a track before it counts as hung (live/indexer.py): the
device it reports when ready decides, since allin1 on a CPU takes about as long as the track plays.
"""
import json
import time
import unittest

from live.indexer import DeepAnalyzer, hang_timeout
from live.library import Track


class FakeHelper:
    """A finished helper process whose stdout was these JSON lines."""
    def __init__(self, *msgs):
        self.stdout = Lines(json.dumps(m) + "\n" for m in msgs)

    def wait(self):
        return 0


class Lines(list):
    def close(self):
        pass


class HangTimeoutTest(unittest.TestCase):
    def test_a_gpu_gets_four_minutes_whatever_the_track(self):
        for device in ("mlx", "cuda", None):  # None: a helper that didn't say
            self.assertEqual(hang_timeout(device, 420.0), 240.0)

    def test_the_cpu_gets_a_few_times_the_track(self):
        self.assertEqual(hang_timeout("cpu", 420.0), 1680.0)
        self.assertEqual(hang_timeout("cpu", 60.0), 600.0)   # never less than ten minutes
        self.assertEqual(hang_timeout("cpu", 0.0), 1800.0)   # length unknown

    def test_the_deadline_follows_the_device_the_helper_reported(self):
        for device, expected in (("cpu", 1680.0), ("cuda", 240.0)):
            analyzer = DeepAnalyzer(store=None)
            self.addCleanup(analyzer.close)
            track = Track(uid="t1", duration=420.0)
            analyzer._tracks[track.uid], analyzer._keys["k1"] = track, track.uid
            deadlines = []
            analyzer._finish = lambda uid, **_: deadlines.append(analyzer._deadline[uid] - time.time())
            analyzer._read(FakeHelper({"event": "ready", "device": device}, {"event": "started", "id": "k1"},
                                      {"event": "failed", "id": "k1"}))
            self.assertAlmostEqual(deadlines[0], expected, delta=5)


if __name__ == "__main__":
    unittest.main()
