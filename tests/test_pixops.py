import os
import random
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from krita_coop import pixops  # noqa: E402


def naive_diff(a, b, ps):
    out = bytearray(len(a))
    for i in range(0, len(a), ps):
        if a[i:i + ps] != b[i:i + ps]:
            out[i:i + ps] = b"\xff" * ps
    return int.from_bytes(bytes(out), "little")


def naive_select(a, b, m, n):
    mb = m.to_bytes(n, "little")
    return bytes(b[i] if mb[i] else a[i] for i in range(n))


class PixopsTests(unittest.TestCase):
    def test_against_naive(self):
        rnd = random.Random(1)
        for ps in (1, 2, 4, 5, 8, 16):
            for w, h in ((64, 64), (7, 3), (1, 1), (64, 1)):
                n = w * h * ps
                a = bytes(rnd.randrange(256) for _ in range(n))
                b = bytearray(a)
                for _ in range(rnd.randrange(0, 40)):  # change a few random bytes
                    b[rnd.randrange(n)] = rnd.randrange(256)
                b = bytes(b)
                m = pixops.diff_mask(a, b, ps)
                self.assertEqual(m, naive_diff(a, b, ps), (ps, w, h))
                c = bytes(rnd.randrange(256) for _ in range(n))
                self.assertEqual(pixops.select(a, c, m, n), naive_select(a, c, m, n))
                pm = pixops.mask_to_pixels(m, n, ps)
                self.assertEqual(len(pm), w * h)
                self.assertEqual(pixops.pixels_to_mask(pm, ps), m)

    def test_identical_and_full(self):
        a = bytes(range(256)) * 64
        self.assertEqual(pixops.diff_mask(a, a, 4), 0)
        b = bytes(255 - x for x in a)
        self.assertEqual(pixops.diff_mask(a, b, 4), pixops.full_mask(len(a)))
        self.assertIs(pixops.select(a, b, 0, len(a)), a)

    def test_speed(self):
        rnd = random.Random(2)
        n = 64 * 64 * 4
        a = bytes(rnd.randrange(256) for _ in range(n))
        b = bytearray(a)
        for i in range(0, n, 7):
            b[i] ^= 1
        b = bytes(b)
        t = time.perf_counter()
        for _ in range(200):
            m = pixops.diff_mask(a, b, 4)
            pixops.select(a, b, m, n)
        per = (time.perf_counter() - t) / 200
        print("\n  diff+select per 64x64 RGBA tile: %.1f us" % (per * 1e6))
        self.assertLess(per, 0.002)


if __name__ == "__main__":
    unittest.main()
