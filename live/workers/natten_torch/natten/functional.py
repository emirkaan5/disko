"""The four NATTEN 0.14 functions allin1 calls, in plain PyTorch.

allin1 (upstream, PyTorch) imports `natten1dqkrpb`, `natten1dav`, `natten2dqkrpb` and `natten2dav`
from `natten.functional`. NATTEN removed that API in 0.18 and ships builds only for a few
torch/CUDA pairs, so on Linux the deep pass puts this folder on sys.path instead (live/workers/deep.py)
and allin1 imports these. They gather each query's neighbourhood with index tables, which is slower
than NATTEN's kernels but needs nothing beyond torch, on CPU or GPU.

Boundary rules follow NATTEN's: a window near an edge is shifted to stay inside the sequence (never
padded), and the relative-position bias index moves with it. The formulas are NATTEN's, as
natten-mlx's `utils/window.py` restates them; tests/test_natten_torch.py checks these functions
against natten-mlx on the same inputs.

Shapes, as NATTEN 0.14 has them:
    natten1dqkrpb(q, k, rpb, kernel_size, dilation)  q, k: [B, H, L, D], rpb: [H, 2K-1]       -> [B, H, L, K]
    natten1dav(attn, v, kernel_size, dilation)        attn: [B, H, L, K], v: [B, H, L, D]      -> [B, H, L, D]
    natten2dqkrpb(q, k, rpb, kernel_size, dilation)  q, k: [B, H, X, Y, D], rpb: [H, 2K-1, 2K-1] -> [B, H, X, Y, K*K]
    natten2dav(attn, v, kernel_size, dilation)        attn: [B, H, X, Y, K*K], v: [B, H, X, Y, D] -> [B, H, X, Y, D]
"""
from functools import lru_cache

import torch


def _pair(value) -> tuple:
    return tuple(value) if isinstance(value, (tuple, list)) else (value, value)


def _window_start(i: int, length: int, kernel: int, dilation: int) -> int:
    half = kernel // 2
    if dilation <= 1:
        return max(i - half, 0) + (i + half >= length) * (length - i - half - 1)
    if i - half * dilation < 0:
        return i % dilation
    if i + half * dilation >= length:
        mod = i % dilation
        a = (length // dilation) * dilation
        b = length - a
        if mod < b:
            return length - b + mod - 2 * half * dilation
        return a + mod - kernel * dilation
    return i - half * dilation


def _bias_start(i: int, length: int, kernel: int, dilation: int) -> int:
    half = kernel // 2
    if dilation <= 1:
        return half + (i < half) * (half - i) + (i + half >= length) * (length - i - 1 - half)
    if i - half * dilation < 0:
        return kernel - 1 - i // dilation
    if i + half * dilation >= length:
        return (length - i - 1) // dilation
    return half


def _check(length: int, kernel: int, dilation: int):
    if kernel % 2 == 0 or kernel < 3:
        raise ValueError(f"kernel_size must be an odd number >= 3, got {kernel}")
    if kernel * dilation > length:
        raise ValueError(f"kernel_size * dilation ({kernel} * {dilation}) is larger than the input ({length})")


@lru_cache(maxsize=256)
def _tables_1d(length: int, kernel: int, dilation: int) -> tuple:
    """(neighbour index [L, K], bias index [L, K]) as Python lists, cached per shape."""
    _check(length, kernel, dilation)
    neigh, bias = [], []
    for i in range(length):
        start, pb = _window_start(i, length, kernel, dilation), _bias_start(i, length, kernel, dilation)
        neigh.append([start + j * dilation for j in range(kernel)])
        bias.append([min(max(pb + j, 0), 2 * kernel - 2) for j in range(kernel)])
    return neigh, bias


def _index(table: list, device) -> torch.Tensor:
    return torch.tensor(table, dtype=torch.long, device=device)


def natten1dqkrpb(query, key, rpb, kernel_size, dilation):
    _, _, length, _ = query.shape
    kernel, dil = _pair(kernel_size)[0], _pair(dilation)[0]
    neigh, bias = _tables_1d(length, kernel, dil)
    neigh, bias = _index(neigh, query.device), _index(bias, query.device)
    keys = key[:, :, neigh]                                      # [B, H, L, K, D]
    logits = torch.einsum("bhld,bhlkd->bhlk", query, keys)
    return logits + rpb[:, bias].unsqueeze(0).to(logits.dtype)   # rpb[:, bias]: [H, L, K]


def natten1dav(attn, value, kernel_size, dilation):
    _, _, length, _ = value.shape
    kernel, dil = _pair(kernel_size)[0], _pair(dilation)[0]
    neigh, _ = _tables_1d(length, kernel, dil)
    values = value[:, :, _index(neigh, value.device)]             # [B, H, L, K, D]
    return torch.einsum("bhlk,bhlkd->bhld", attn, values)


@lru_cache(maxsize=256)
def _tables_2d(height: int, width: int, kh: int, kw: int, dh: int, dw: int) -> tuple:
    """(flat neighbour index [X*Y, Kh*Kw], flat bias index [X*Y, Kh*Kw]), row-major over the kernel."""
    rows_n, rows_b = _tables_1d(height, kh, dh)
    cols_n, cols_b = _tables_1d(width, kw, dw)
    neigh, bias = [], []
    for x in range(height):
        for y in range(width):
            neigh.append([r * width + c for r in rows_n[x] for c in cols_n[y]])
            bias.append([r * (2 * kw - 1) + c for r in rows_b[x] for c in cols_b[y]])
    return neigh, bias


def natten2dqkrpb(query, key, rpb, kernel_size, dilation):
    b, h, height, width, d = query.shape
    (kh, kw), (dh, dw) = _pair(kernel_size), _pair(dilation)
    neigh, bias = _tables_2d(height, width, kh, kw, dh, dw)
    neigh, bias = _index(neigh, query.device), _index(bias, query.device)
    q = query.reshape(b, h, height * width, d)
    keys = key.reshape(b, h, height * width, d)[:, :, neigh]    # [B, H, XY, KK, D]
    logits = torch.einsum("bhnd,bhnkd->bhnk", q, keys)
    logits = logits + rpb.reshape(h, -1)[:, bias].unsqueeze(0).to(logits.dtype)
    return logits.reshape(b, h, height, width, kh * kw)


def natten2dav(attn, value, kernel_size, dilation):
    b, h, height, width, d = value.shape
    (kh, kw), (dh, dw) = _pair(kernel_size), _pair(dilation)
    neigh, _ = _tables_2d(height, width, kh, kw, dh, dw)
    values = value.reshape(b, h, height * width, d)[:, :, _index(neigh, value.device)]
    out = torch.einsum("bhnk,bhnkd->bhnd", attn.reshape(b, h, height * width, kh * kw), values)
    return out.reshape(b, h, height, width, d)
