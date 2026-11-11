"""live/workers/natten_torch against natten-mlx, on the same random inputs.

The PyTorch stand-in for NATTEN 0.14 (what upstream allin1 calls on Linux) must give what NATTEN gives,
boundary shifts and dilation included. natten-mlx's compat.v014 implements the same four functions
on MLX, so on an Apple Silicon Mac both run and are compared; elsewhere, or without torch and
natten-mlx in the running Python, this skips. Run it with any Python that has torch, mlx and natten-mlx:

    uv run --with torch --with mlx --with natten-mlx python -m unittest tests.test_natten_torch
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "live" / "workers" / "natten_torch"))

try:
    import mlx.core as mx
    import numpy as np
    import torch
    from natten_mlx.compat import v014 as ref

    from natten import functional as ours
except ImportError as e:  # not an Apple Silicon Python with all three
    raise unittest.SkipTest(f"needs torch, mlx and natten-mlx: {e}")


def same(test, a, b):
    test.assertEqual(tuple(a.shape), tuple(b.shape))
    np.testing.assert_allclose(np.asarray(a.detach().numpy()), np.asarray(b), rtol=1e-4, atol=1e-4)


class NattenTorchTest(unittest.TestCase):
    def test_1d(self):
        rng = np.random.default_rng(0)
        for length, kernel, dilation in [(9, 3, 1), (20, 5, 1), (21, 5, 2), (50, 5, 4), (37, 5, 7),
                                         (64, 7, 8), (40, 5, 8), (300, 5, 32), (5, 5, 1)]:
            with self.subTest(length=length, kernel=kernel, dilation=dilation):
                q, k, v = (rng.standard_normal((2, 3, length, 8)).astype(np.float32) for _ in range(3))
                rpb = rng.standard_normal((3, 2 * kernel - 1)).astype(np.float32)
                logits = ours.natten1dqkrpb(torch.tensor(q), torch.tensor(k), torch.tensor(rpb), kernel, dilation)
                same(self, logits, ref.natten1dqkrpb(mx.array(q), mx.array(k), mx.array(rpb), kernel, dilation))
                attn = torch.softmax(logits, dim=-1)
                out = ours.natten1dav(attn, torch.tensor(v), kernel, dilation)
                same(self, out, ref.natten1dav(mx.array(attn.numpy()), mx.array(v), kernel, dilation))

    def test_2d(self):
        rng = np.random.default_rng(1)
        for (x, y), kernel, dilation in [((7, 9), 3, 1), ((12, 20), 5, (1, 2)), ((15, 40), 5, (2, 4)),
                                         ((10, 33), 5, (1, 6)), ((5, 5), 5, 1)]:
            with self.subTest(shape=(x, y), kernel=kernel, dilation=dilation):
                q, k, v = (rng.standard_normal((2, 2, x, y, 4)).astype(np.float32) for _ in range(3))
                rpb = rng.standard_normal((2, 2 * kernel - 1, 2 * kernel - 1)).astype(np.float32)
                logits = ours.natten2dqkrpb(torch.tensor(q), torch.tensor(k), torch.tensor(rpb), kernel, dilation)
                same(self, logits, ref.natten2dqkrpb(mx.array(q), mx.array(k), mx.array(rpb), kernel, dilation))
                attn = torch.softmax(logits, dim=-1)
                out = ours.natten2dav(attn, torch.tensor(v), kernel, dilation)
                same(self, out, ref.natten2dav(mx.array(attn.numpy()), mx.array(v), kernel, dilation))

    def test_refuses_what_natten_refuses(self):
        q = torch.zeros(1, 1, 8, 4)
        with self.assertRaises(ValueError):
            ours.natten1dqkrpb(q, q, torch.zeros(1, 9), 5, 2)  # 5 * 2 > 8
        with self.assertRaises(ValueError):
            ours.natten1dqkrpb(q, q, torch.zeros(1, 7), 4, 1)  # even kernel


if __name__ == "__main__":
    unittest.main()
