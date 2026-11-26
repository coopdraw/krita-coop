"""Per-pixel tile operations without numpy.

Tiles are raw pixel bytes (``ps`` bytes per pixel).  Masks are Python big
integers with every byte of a selected pixel set to 0xFF, so comparing,
selecting and merging a 64x64 tile costs a handful of C-level big-int
operations (tens of microseconds) instead of a Python loop over pixels.
"""

import zlib

_NONZERO_TO_FF = bytes([0] + [255] * 255)
_low_cache = {}
_full_cache = {}
_zero_cache = {}
_zcrc_cache = {}


def zero(n):
    z = _zero_cache.get(n)
    if z is None:
        z = _zero_cache[n] = bytes(n)
    return z


def zero_crc(n):
    c = _zcrc_cache.get(n)
    if c is None:
        c = _zcrc_cache[n] = zlib.crc32(zero(n))
    return c


def _low_mask(n, ps):
    key = (n, ps)
    m = _low_cache.get(key)
    if m is None:
        m = _low_cache[key] = int.from_bytes((b"\xff" + bytes(ps - 1)) * (n // ps), "little")
    return m


def full_mask(n):
    m = _full_cache.get(n)
    if m is None:
        m = _full_cache[n] = (1 << (8 * n)) - 1
    return m


def _collapse(m, ps):
    """OR the bytes of each pixel into the pixel's first byte."""
    if ps & (ps - 1) == 0:
        s = 8
        while s < 8 * ps:
            m |= m >> s
            s <<= 1
        return m
    acc = m
    for k in range(1, ps):
        acc |= m >> (8 * k)
    return acc


def _spread(m, ps):
    """Copy each pixel's first byte into all of its bytes."""
    if ps & (ps - 1) == 0:
        s = 8
        while s < 8 * ps:
            m |= m << s
            s <<= 1
        return m
    acc = m
    for k in range(1, ps):
        acc |= m << (8 * k)
    return acc


def diff_mask(a, b, ps):
    """Mask of the pixels that differ between ``a`` and ``b`` (0 when identical)."""
    if a == b:
        return 0
    n = len(a)
    x = (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(n, "little")
    m = int.from_bytes(x.translate(_NONZERO_TO_FF), "little")
    return _spread(_collapse(m, ps) & _low_mask(n, ps), ps)


def select(a, b, m, n):
    """Pixels of ``b`` where ``m`` is set, ``a`` elsewhere."""
    if m == 0:
        return a
    if m == full_mask(n):
        return b
    A = int.from_bytes(a, "little")
    B = int.from_bytes(b, "little")
    return ((A & ~m) | (B & m)).to_bytes(n, "little")


def mask_to_pixels(m, n, ps):
    """One byte per pixel (0x00 / 0xFF) - the compact form sent over the wire."""
    return m.to_bytes(n, "little")[0::ps]


def pixels_to_mask(pm, ps):
    buf = bytearray(len(pm) * ps)
    buf[0::ps] = pm
    return _spread(int.from_bytes(bytes(buf).translate(_NONZERO_TO_FF), "little"), ps)
