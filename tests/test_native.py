"""The extras of the co-op build of Krita, on fake documents that mimic them
(``fakedoc.NativeFakeDoc``): strokes are seen while they are painted (also in
Wash mode), other people's paint arrives while you are mid-stroke, and Krita
reports what changed.

Run with:  python -m unittest discover -s tests -v
"""

import os
import random
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fakedoc import FakeDoc, NativeFakeDoc  # noqa: E402
from test_engine import BLUE, GREEN, RED, WHITE, YELLOW, px  # noqa: E402
from test_undo import CLEAR, UGroup, undo_cfg  # noqa: E402
from krita_coop.session import GuestSession, HostSession  # noqa: E402


def make_native_host_doc():
    d = NativeFakeDoc(300, 200)
    bg = d.add("Background")
    d.paint(bg, 0, 0, 300, 200, WHITE)
    sk = d.add("Sketch")
    d.paint(sk, 10, 10, 50, 40, RED)
    g = d.add("Group", kind="group")
    ink = d.add("Ink", parent=g)
    d.paint(ink, 100, 70, 90, 90, BLUE)
    return d


class NGroup(UGroup):
    """Host and guests all run the (fake) co-op build; ``stock`` lists guests on stock Krita."""

    def __init__(self, hdoc, guests=1, cfg=None, stock=()):
        self.hdoc = hdoc
        self.host = HostSession(hdoc, "Hana", port=0, cfg=cfg or undo_cfg())
        self.host.start()
        self.gdocs = [None] * guests
        self.guests = []
        for i in range(guests):
            def factory(info, i=i):
                cls = FakeDoc if i in stock else NativeFakeDoc
                d = cls(info["w"], info["h"], info["ps"], "guest%d" % i)
                d.add("Background")
                self.gdocs[i] = d
                return d
            g = GuestSession(factory, "Gus%d" % i if guests > 1 else "Gus", "127.0.0.1", self.host.port,
                             self.host.code, cfg=cfg or undo_cfg())
            g.start()
            self.guests.append(g)
        self._held = {}

    def close(self):
        for d in self.docs():
            if d is None or not getattr(d, "native", False):
                continue
            if d.unpaused_busy_writes:
                raise AssertionError("wrote into a document during a stroke that was not held (%d)"
                                     % d.unpaused_busy_writes)
            if d.paused:
                raise AssertionError("a stroke was left held")
        UGroup.close(self)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.s = NGroup(make_native_host_doc())
        self.s.sync(self, "initial sync")
        self.s.settle(self, "initial settle")

    def tearDown(self):
        self.s.close()

    # ---- seeing strokes while they are painted ---------------------------------------
    def test_wash_stroke_shows_for_others_while_it_is_painted(self):
        s, h = self.s, self.s.hdoc
        sk = h.find("Sketch")
        h.idle = False
        h.wash(sk, 200, 20, 30, 30, GREEN)
        self.assertTrue(s.pump(lambda: px(s.gdoc, "Sketch", 210, 30) == GREEN),
                        "the guest never saw the stroke in progress")
        self.assertEqual(px(h, "Sketch", 210, 30), CLEAR)  # on the host it is still only an overlay
        h.wash(sk, 230, 20, 30, 30, GREEN)  # the stroke goes on
        self.assertTrue(s.pump(lambda: px(s.gdoc, "Sketch", 240, 30) == GREEN))
        h.end_wash(sk)
        h.idle = True
        s.settle(self)
        for d in s.docs():
            self.assertEqual(px(d, "Sketch", 210, 30), GREEN)
            self.assertEqual(px(d, "Sketch", 240, 30), GREEN)
        # the whole stroke is one undo step
        s.undo(self, s.host)
        for d in s.docs():
            self.assertEqual(px(d, "Sketch", 210, 30), CLEAR)
            self.assertEqual(px(d, "Sketch", 240, 30), CLEAR)
            self.assertEqual(px(d, "Sketch", 20, 20), RED)

    def test_cancelled_wash_stroke_disappears_for_others(self):
        s, h = self.s, self.s.hdoc
        sk = h.find("Sketch")
        h.idle = False
        h.wash(sk, 200, 20, 30, 30, GREEN)
        h.wash(sk, 20, 20, 10, 10, GREEN)  # over existing paint
        self.assertTrue(s.pump(lambda: px(s.gdoc, "Sketch", 210, 30) == GREEN and px(s.gdoc, "Sketch", 25, 25) == GREEN))
        h.cancel_wash(sk)
        h.idle = True
        s.settle(self)
        for d in s.docs():
            self.assertEqual(px(d, "Sketch", 210, 30), CLEAR)
            self.assertEqual(px(d, "Sketch", 25, 25), RED)

    # ---- receiving while you draw ---------------------------------------------------------
    def test_others_paint_arrives_while_you_are_mid_stroke(self):
        s, g = self.s, self.s.gdoc
        g.idle = False  # the guest has a brush stroke running ("Build up": it paints into the layer)
        g.paint(g.find("Sketch"), 200, 100, 10, 10, BLUE)
        hs = s.hdoc.find("Sketch")
        s.hdoc.paint(hs, 215, 105, 10, 10, GREEN)  # the same tile as the guest's stroke
        s.hdoc.paint(hs, 100, 150, 20, 20, GREEN)  # another tile
        ok = s.pump(lambda: px(g, "Sketch", 105, 155) == GREEN and px(g, "Sketch", 220, 110) == GREEN and
                    px(s.hdoc, "Sketch", 205, 105) == BLUE)
        self.assertTrue(ok, "paint did not cross while the guest was mid-stroke")
        self.assertFalse(g.idle)
        self.assertGreater(g.pauses, 0)
        self.assertGreater(g.live_writes, 0)
        self.assertEqual(px(g, "Sketch", 205, 105), BLUE)  # the guest's own stroke is untouched
        g.paint(g.find("Sketch"), 240, 100, 10, 10, BLUE)  # ... and goes on
        g.idle = True
        s.settle(self)
        for d in s.docs():
            self.assertEqual(px(d, "Sketch", 245, 105), BLUE)
            self.assertEqual(px(d, "Sketch", 220, 110), GREEN)

    def test_tile_under_your_wash_stroke_waits_until_the_stroke_ends(self):
        s, g = self.s, self.s.gdoc
        sk = g.find("Sketch")
        g.idle = False
        g.wash(sk, 200, 100, 10, 10, BLUE)  # tile (3, 1)
        self.assertTrue(s.pump(lambda: px(s.hdoc, "Sketch", 205, 105) == BLUE), "host never saw the preview")
        hs = s.hdoc.find("Sketch")
        s.hdoc.paint(hs, 230, 110, 10, 10, GREEN)  # the tile under the guest's stroke
        s.hdoc.paint(hs, 20, 150, 10, 10, GREEN)  # another tile of the same layer
        self.assertTrue(s.pump(lambda: px(g, "Sketch", 25, 155) == GREEN), "the other tile did not arrive mid-stroke")
        for _ in range(25):
            s.tick()
            time.sleep(0.004)
        self.assertEqual(px(g, "Sketch", 235, 115), CLEAR)  # waits: the layer doesn't hold the stroke yet
        self.assertEqual(px(g, "Sketch", 205, 105), CLEAR)
        g.end_wash(sk)
        g.idle = True
        s.settle(self)
        for d in s.docs():
            self.assertEqual(px(d, "Sketch", 205, 105), BLUE)
            self.assertEqual(px(d, "Sketch", 235, 115), GREEN)
            self.assertEqual(px(d, "Sketch", 25, 155), GREEN)

    def test_open_tool_action_holds_only_the_layers_it_touches(self):
        s, g = self.s, self.s.gdoc
        g.idle = False
        g.stroke = "TRANSFORM_STROKE"
        g.touch(g.find("Sketch"), 0, 0, 300, 200)
        s.tick()
        s.hdoc.paint(s.hdoc.find("Sketch"), 20, 150, 10, 10, GREEN)
        s.hdoc.paint(s.hdoc.find("Ink"), 110, 80, 10, 10, GREEN)
        self.assertTrue(s.pump(lambda: px(g, "Ink", 115, 85) == GREEN), "the untouched layer did not update")
        for _ in range(25):
            s.tick()
            time.sleep(0.004)
        self.assertEqual(px(g, "Sketch", 25, 155), CLEAR)  # the layer under the tool waits
        self.assertTrue(s.guest.status()["waiting"])
        g.idle = True
        s.settle(self)
        self.assertEqual(px(g, "Sketch", 25, 155), GREEN)
        self.assertFalse(s.guest.status()["waiting"])

    def test_open_tool_action_holds_the_selected_layer_even_before_it_touches_it(self):
        s, g = self.s, self.s.gdoc
        g.active = g.find("Sketch").uuid
        g.idle = False
        g.stroke = "TRANSFORM_STROKE"
        s.hdoc.paint(s.hdoc.find("Sketch"), 20, 150, 10, 10, GREEN)
        s.hdoc.paint(s.hdoc.find("Ink"), 110, 80, 10, 10, GREEN)
        self.assertTrue(s.pump(lambda: px(g, "Ink", 115, 85) == GREEN), "the other layer did not update")
        for _ in range(25):
            s.tick()
            time.sleep(0.004)
        self.assertEqual(px(g, "Sketch", 25, 155), CLEAR)
        g.idle = True
        s.settle(self)
        self.assertEqual(px(g, "Sketch", 25, 155), GREEN)

    def test_refused_pause_is_simply_tried_again(self):
        s, g = self.s, self.s.gdoc
        g.idle = False
        g.paint(g.find("Sketch"), 200, 100, 10, 10, BLUE)
        g.refuse_pauses = 4  # Krita: "not now" (the running jobs can't finish while held back)
        s.hdoc.paint(s.hdoc.find("Sketch"), 100, 150, 20, 20, GREEN)
        self.assertTrue(s.pump(lambda: px(g, "Sketch", 105, 155) == GREEN), "paint never arrived")
        self.assertEqual(g.refused_pauses, 4)
        self.assertFalse(g.idle)
        g.idle = True
        s.settle(self)

    def test_write_refused_during_a_merge_is_done_afterwards(self):
        s, g = self.s, self.s.gdoc
        sk = g.find("Sketch")
        g.idle = False
        g.merging.add(sk.uuid)  # a finished stroke is being merged into the guest's layer
        s.hdoc.paint(s.hdoc.find("Sketch"), 100, 150, 20, 20, GREEN)
        s.hdoc.paint(s.hdoc.find("Ink"), 110, 80, 10, 10, GREEN)
        self.assertTrue(s.pump(lambda: px(g, "Ink", 115, 85) == GREEN), "the other layer did not update")
        for _ in range(25):
            s.tick()
            time.sleep(0.004)
        self.assertEqual(px(g, "Sketch", 105, 155), CLEAR)
        self.assertGreater(g.refused_writes, 0)
        g.merging.clear()
        self.assertTrue(s.pump(lambda: px(g, "Sketch", 105, 155) == GREEN), "the refused tile was dropped")
        g.idle = True
        s.settle(self)

    def test_layer_changes_still_wait_for_the_stroke_to_end(self):
        s, g = self.s, self.s.gdoc
        g.idle = False
        g.paint(g.find("Sketch"), 200, 100, 10, 10, BLUE)
        s.hdoc.add("Fresh")
        s.hdoc.paint(s.hdoc.find("Fresh"), 5, 5, 10, 10, YELLOW)
        for _ in range(40):
            s.tick()
            time.sleep(0.004)
        self.assertIsNone(g.find("Fresh"))  # the layer tree is only changed between strokes
        g.idle = True
        s.settle(self)
        self.assertEqual(px(g, "Fresh", 10, 10), YELLOW)
        self.assertGreater(g.settles, 0)

    # ---- robustness ------------------------------------------------------------------------
    def test_changes_krita_does_not_report_are_still_found(self):
        s = self.s
        s.hdoc.report = False
        s.stroke(self, s.host, "Sketch", 150, 150, 30, 30, YELLOW)
        self.assertEqual(px(s.gdoc, "Sketch", 160, 160), YELLOW)
        s.undo(self, s.host)
        self.assertEqual(px(s.gdoc, "Sketch", 160, 160), CLEAR)

    def test_layer_that_is_being_merged_is_read_afterwards(self):
        s, h = self.s, self.s.hdoc
        sk = h.find("Sketch")
        h.idle = False
        h.merging.add(sk.uuid)  # Krita is merging a finished stroke into the layer: reads say "busy"
        h.paint(sk, 150, 150, 30, 30, YELLOW)
        s.gdoc.paint(s.gdoc.find("Sketch"), 20, 150, 10, 10, GREEN)
        for _ in range(25):
            s.tick()
            time.sleep(0.004)
        self.assertEqual(px(s.gdoc, "Sketch", 160, 160), CLEAR)
        h.merging.clear()
        h.idle = True
        s.settle(self)
        for d in s.docs():
            self.assertEqual(px(d, "Sketch", 160, 160), YELLOW)
            self.assertEqual(px(d, "Sketch", 25, 155), GREEN)
        s.undo(self, s.host)
        for d in s.docs():
            self.assertEqual(px(d, "Sketch", 160, 160), CLEAR)
            self.assertEqual(px(d, "Sketch", 25, 155), GREEN)

    def test_both_drawing_at_once_then_everyone_undoes_everything(self):
        s = self.s
        start = [bytes(s.hdoc.find(n).pixels) for n in ("Sketch", "Ink")]
        rnd = random.Random(7)
        counts = {s.host: 0, s.guest: 0}
        for _ in range(12):
            for sess in (s.host, s.guest):
                d = s.doc_of(sess)
                layer = rnd.choice(("Sketch", "Ink"))
                n = d.find(layer)
                x, y = rnd.randrange(0, 260), rnd.randrange(0, 170)
                color = BLUE if sess is s.host else RED
                d.idle = False
                if rnd.random() < 0.5:
                    d.wash(n, x, y, 30, 25, color)
                else:
                    d.paint(n, x, y, 30, 25, color)
            for _ in range(rnd.randrange(2, 8)):  # both strokes are running at the same time
                s.tick()
                time.sleep(0.003)
            for sess in (s.host, s.guest):
                d = s.doc_of(sess)
                for n in list(d.nodes.values()):
                    d.end_wash(n)
                d.idle = True
                counts[sess] += 1
            s.settle(self, "strokes did not settle")
        for sess in (s.host, s.guest):
            for _ in range(sess.undo_state()["undo"]):
                s.undo(self, sess)
        for d in s.docs():
            self.assertEqual([bytes(d.find(n).pixels) for n in ("Sketch", "Ink")], start)


class MixedTests(unittest.TestCase):
    """A co-op build and a stock Krita in one session."""

    def test_stock_guest_with_coop_host(self):
        s = NGroup(make_native_host_doc(), stock=(0,))
        try:
            s.sync(self, "initial sync")
            s.settle(self)
            h = s.hdoc
            sk = h.find("Sketch")
            h.idle = False
            h.wash(sk, 200, 20, 30, 30, GREEN)
            self.assertTrue(s.pump(lambda: px(s.gdoc, "Sketch", 210, 30) == GREEN))  # the guest sees it live
            s.gdoc.idle = False  # ... but can't receive while its own stroke runs
            s.gdoc.paint(s.gdoc.find("Sketch"), 20, 150, 10, 10, BLUE)
            h.wash(sk, 230, 20, 30, 30, GREEN)
            self.assertTrue(s.pump(lambda: px(h, "Sketch", 25, 155) == BLUE))
            self.assertEqual(px(s.gdoc, "Sketch", 240, 30), CLEAR)
            s.gdoc.idle = True
            h.end_wash(sk)
            h.idle = True
            s.settle(self)
            self.assertEqual(px(s.gdoc, "Sketch", 240, 30), GREEN)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
