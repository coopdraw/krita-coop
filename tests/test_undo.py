"""Per-person undo: host + guests over localhost sockets on fake documents.

Run with:  python -m unittest discover -s tests -v
"""

import json
import os
import random
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fakedoc import FakeDoc  # noqa: E402
from test_engine import BLUE, GREEN, RED, YELLOW, Group, fast_cfg, make_host_doc, px  # noqa: E402
from krita_coop import pixops  # noqa: E402
from krita_coop import protocol as P  # noqa: E402
from krita_coop.server import LOCAL, Server  # noqa: E402
from krita_coop.session import GuestSession, HostSession  # noqa: E402

CLEAR = b"\x00\x00\x00\x00"
PURPLE = b"\x80\x00\x80\xff"


def undo_cfg():
    c = fast_cfg()
    c.undo_wait = 0.3
    c.undo_wait_max = 3.0
    c.undo_echo_wait = 5.0
    return c


class UGroup(Group):
    """A Group whose sessions use undo-friendly test settings."""

    def __init__(self, hdoc, guests=1, cfg=None):
        self.hdoc = hdoc
        self.host = HostSession(hdoc, "Hana", port=0, cfg=cfg or undo_cfg())
        self.host.start()
        self.gdocs = [None] * guests
        self.guests = []
        for i in range(guests):
            def factory(info, i=i):
                d = FakeDoc(info["w"], info["h"], info["ps"], "guest%d" % i)
                d.add("Background")
                self.gdocs[i] = d
                return d
            g = GuestSession(factory, "Gus%d" % i if guests > 1 else "Gus", "127.0.0.1", self.host.port,
                             self.host.code, cfg=cfg or undo_cfg())
            g.start()
            self.guests.append(g)
        self._held = {}

    def docs(self):
        return [self.hdoc] + self.gdocs

    def sessions(self):
        return [self.host] + self.guests

    def doc_of(self, sess):
        return self.hdoc if sess is self.host else self.gdocs[self.guests.index(sess)]

    def hold(self, sess):
        """Stop ``sess`` from reading what the host sends (simulates network delay)."""
        self._held[sess] = True
        sess._drain = lambda: None

    def release(self, sess):
        if self._held.pop(sess, None):
            del sess._drain

    def quiet(self, sess):
        u = sess.undo
        return (u.tracker.state == "sealed" and u.hist.cur is None and not sess.replica.sealing()
                and not u.queue and u.pending is None and not sess.replica.unacked_keys)

    def settle(self, test, msg="did not settle"):
        ok = self.pump(lambda: all(self.quiet(s) for s in self.sessions() if s not in self._held) and
                       (self._held or self.converged()))
        if not ok:
            test.fail("%s\nhost: %s %s\nguest: %s %s" % (
                msg, self.host.undo_state(), self.host.events[-4:], self.guest.undo_state(), self.guest.events[-4:]))

    def stroke(self, test, sess, layer, x, y, w, h, color, busy=3, settle=True):
        """A stroke: Krita is busy while painting, then idle; waits until it is a closed undo unit."""
        d = self.doc_of(sess)
        d.idle = busy == 0
        d.paint(d.find(layer), x, y, w, h, color)
        for _ in range(busy):
            self.tick()
            time.sleep(0.003)
        d.idle = True
        if settle:
            self.settle(test, "stroke did not settle")

    def undo(self, test, sess, kind="undo"):
        test.assertTrue(sess.request_undo() if kind == "undo" else sess.request_redo())
        self.settle(test, "%s did not finish" % kind)

    def close(self):
        for s in list(self._held):
            self.release(s)
        for d in self.docs():
            if d is not None and d.busy_writes:
                raise AssertionError("wrote into a document while it was busy (%d)" % d.busy_writes)
        Group.close(self)


def snap(doc, layer):
    return bytes(doc.find(layer).pixels)


def events(sess, kind=None):
    return [t for k, t in sess.events if kind is None or k == kind]


class Base(unittest.TestCase):
    hosts_guests = 1

    def setUp(self):
        self.s = UGroup(make_host_doc(), guests=self.hosts_guests)
        self.addCleanup(self.s.close)
        self.s.sync(self, "join")
        self.s.settle(self, "join settle")
        self.h, self.g = self.s.host, self.s.guest
        self.hd, self.gd = self.s.hdoc, self.s.gdoc

    def both(self, layer, x, y):
        return px(self.hd, layer, x, y), px(self.gd, layer, x, y)


class PersonalUndoTests(Base):
    def test_undo_only_mine_when_partner_drew_last(self):
        """The first reported bug: Ctrl+Z took back the partner's newer line instead of mine."""
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 100, 300, 6, GREEN)
        s.stroke(self, self.h, "Sketch", 0, 102, 30, 2, YELLOW)  # same tiles, newer
        s.stroke(self, self.h, "Sketch", 200, 150, 40, 4, YELLOW)  # elsewhere
        s.undo(self, self.g)
        for d in (self.hd, self.gd):
            self.assertEqual(px(d, "Sketch", 100, 101), CLEAR)  # my green: gone
            self.assertEqual(px(d, "Sketch", 10, 102), YELLOW)  # partner's line on top: kept
            self.assertEqual(px(d, "Sketch", 210, 151), YELLOW)
        self.assertIn("Undid your change on “Sketch”.", events(self.g, "undo"))

    def test_everyone_undoes_everything_gives_the_start_back(self):
        """The second reported bug: overlapping strokes, both undo everything -> leftovers."""
        for order in ("guest first", "host first", "alternating"):
            with self.subTest(order=order):
                s = self.s
                start = snap(self.hd, "Sketch")
                rnd = random.Random(order)
                for _ in range(4):
                    s.stroke(self, self.g, "Sketch", rnd.randrange(0, 200), rnd.randrange(60, 160), 80, 12, RED)
                    s.stroke(self, self.h, "Sketch", rnd.randrange(0, 200), rnd.randrange(60, 160), 80, 12, BLUE)
                if order == "guest first":
                    seq = [self.g] * 4 + [self.h] * 4
                elif order == "host first":
                    seq = [self.h] * 4 + [self.g] * 4
                else:
                    seq = [self.g, self.h] * 4
                for who in seq:
                    s.undo(self, who)
                self.assertEqual(snap(self.hd, "Sketch"), start)
                self.assertEqual(snap(self.gd, "Sketch"), start)

    def test_partner_undo_under_my_stroke_stays_undone(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 100, 100, 20, RED)
        s.stroke(self, self.h, "Sketch", 50, 105, 100, 10, BLUE)  # over part of the red
        s.undo(self, self.g)  # red goes, except where blue covers it
        self.assertEqual(self.both("Sketch", 10, 101), (CLEAR, CLEAR))
        self.assertEqual(self.both("Sketch", 60, 106), (BLUE, BLUE))
        s.undo(self, self.h)  # blue goes: the red under it must NOT come back
        self.assertEqual(self.both("Sketch", 60, 106), (CLEAR, CLEAR))
        s.undo(self, self.g, "redo")  # ... until its owner redoes it
        self.assertEqual(self.both("Sketch", 60, 106), (RED, RED))

    def test_lifo_redo_and_nothing_left(self):
        s = self.s
        snaps = [snap(self.gd, "Sketch")]
        for i, c in enumerate((GREEN, BLUE, YELLOW)):
            s.stroke(self, self.g, "Sketch", 20 * i, 120, 40, 10, c)
            snaps.append(snap(self.gd, "Sketch"))
        self.assertEqual(self.g.undo_state()["undo"], 3)
        for i in (2, 1, 0):
            s.undo(self, self.g)
            self.assertEqual(snap(self.gd, "Sketch"), snaps[i])
            self.assertEqual(snap(self.hd, "Sketch"), snaps[i])
        s.undo(self, self.g)
        self.assertTrue(any(t.startswith("Nothing of yours to undo") for t in events(self.g, "undo-warn")))
        self.assertEqual(snap(self.gd, "Sketch"), snaps[0])
        for i in (1, 2, 3):
            s.undo(self, self.g, "redo")
            self.assertEqual(snap(self.hd, "Sketch"), snaps[i])
        self.assertEqual(self.g.undo_state()["redo"], 0)

    def test_new_edit_clears_redo(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 120, 50, 5, GREEN)
        s.undo(self, self.g)
        self.assertEqual(self.g.undo_state()["redo"], 1)
        s.stroke(self, self.g, "Sketch", 100, 150, 50, 5, BLUE)
        self.assertEqual(self.g.undo_state()["redo"], 0)
        s.undo(self, self.g, "redo")
        self.assertEqual(self.both("Sketch", 10, 121), (CLEAR, CLEAR))

    def test_redo_keeps_later_paint(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 120, 100, 10, GREEN)
        s.undo(self, self.g)
        s.stroke(self, self.h, "Sketch", 0, 120, 20, 10, YELLOW)  # partner paints where it was
        s.undo(self, self.g, "redo")
        self.assertEqual(self.both("Sketch", 5, 125), (YELLOW, YELLOW))
        self.assertEqual(self.both("Sketch", 50, 125), (GREEN, GREEN))

    def test_partner_erased_part_before_my_undo_then_redo(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 120, 100, 10, GREEN)
        s.stroke(self, self.h, "Sketch", 0, 120, 30, 10, CLEAR)  # partner erases part of it
        s.undo(self, self.g)
        s.undo(self, self.g, "redo")
        self.assertEqual(self.both("Sketch", 10, 125), (CLEAR, CLEAR))  # stays erased
        self.assertEqual(self.both("Sketch", 50, 125), (GREEN, GREEN))

    def test_undo_shows_what_was_there_before(self):
        """Partner's paint reached the host first and my stroke covered it: undo shows theirs."""
        s = self.s
        s.hold(self.g)
        self.hd.paint(self.hd.find("Sketch"), 0, 130, 60, 6, YELLOW)
        s.pump(lambda: s.quiet(self.h), timeout=5)
        s.stroke(self, self.g, "Sketch", 0, 128, 60, 10, GREEN, settle=False)
        s.pump(lambda: s.quiet(self.h) and self.g.undo.tracker.state == "sealed", timeout=5)
        time.sleep(0.05)
        s.release(self.g)
        s.settle(self)
        self.assertEqual(self.both("Sketch", 5, 131), (GREEN, GREEN))
        s.undo(self, self.g)
        self.assertEqual(self.both("Sketch", 5, 131), (YELLOW, YELLOW))
        self.assertEqual(self.both("Sketch", 5, 129), (CLEAR, CLEAR))

    def test_paint_still_on_its_way_is_kept(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 120, 100, 10, GREEN)
        s.hold(self.g)  # the guest won't see the host's next paint before undoing
        self.hd.paint(self.hd.find("Sketch"), 0, 120, 20, 10, YELLOW)
        s.pump(lambda: s.quiet(self.h), timeout=5)
        self.assertTrue(self.g.request_undo())
        s.pump(lambda: self.g.undo.pending is not None, timeout=5)
        s.release(self.g)
        s.settle(self)
        self.assertEqual(self.both("Sketch", 5, 125), (YELLOW, YELLOW))
        self.assertEqual(self.both("Sketch", 50, 125), (CLEAR, CLEAR))

    def test_undo_is_not_sent_back_as_an_edit(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 120, 100, 10, GREEN)
        sent = []
        r = self.g.replica
        orig = r.send_patch
        r.send_patch = lambda sid, pseq, entries, unit=None: (sent.append(unit), orig(sid, pseq, entries, unit))
        try:
            s.undo(self, self.g)
            for _ in range(30):
                s.tick()
            self.assertEqual(sent, [])
        finally:
            r.send_patch = orig
        self.assertEqual(self.g.undo_state(), {"undo": 0, "redo": 1, "waiting": None, "broken": False})

    def test_host_undo(self):
        s = self.s
        s.stroke(self, self.h, "Ink", 120, 80, 40, 40, GREEN)
        s.stroke(self, self.g, "Ink", 130, 90, 10, 10, YELLOW)
        s.undo(self, self.h)
        self.assertEqual(self.both("Ink", 150, 110), (BLUE, BLUE))  # host's original ink
        self.assertEqual(self.both("Ink", 135, 95), (YELLOW, YELLOW))

    def test_initial_content_not_undoable(self):
        before = snap(self.hd, "Sketch")
        self.s.undo(self, self.h)
        self.s.undo(self, self.g)
        self.assertEqual(snap(self.hd, "Sketch"), before)
        self.assertEqual(snap(self.gd, "Sketch"), before)

    def test_fully_covered_change_is_skipped(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 120, 50, 5, GREEN)  # A
        s.stroke(self, self.g, "Sketch", 100, 150, 20, 5, BLUE)  # B
        s.stroke(self, self.h, "Sketch", 95, 145, 30, 15, YELLOW)  # covers all of B
        s.undo(self, self.g)
        self.assertIn("Skipped one of your changes: others have painted over all of it.", events(self.g, "undo"))
        self.assertEqual(self.both("Sketch", 10, 121), (CLEAR, CLEAR))  # A undone instead
        self.assertEqual(self.both("Sketch", 105, 151), (YELLOW, YELLOW))
        s.undo(self, self.h)  # the host's cover goes: B stays undone
        self.assertEqual(self.both("Sketch", 105, 151), (CLEAR, CLEAR))

    def test_multi_layer_action(self):
        s = self.s
        gd = self.gd
        gd.idle = False
        gd.paint(gd.find("Sketch"), 0, 120, 50, 5, GREEN)
        gd.paint(gd.find("Ink"), 150, 150, 20, 5, GREEN)
        for _ in range(3):
            s.tick()
        gd.idle = True
        s.settle(self)
        self.assertEqual(self.g.undo_state()["undo"], 1)
        s.undo(self, self.g)
        self.assertEqual(self.both("Sketch", 10, 121), (CLEAR, CLEAR))
        self.assertEqual(self.both("Ink", 155, 151), (BLUE, BLUE))
        self.assertIn("Undid your change on 2 layers.", events(self.g, "undo"))


class TimingTests(Base):
    def test_request_while_stroke_finishing(self):
        s = self.s
        gd = self.gd
        gd.idle = False
        gd.paint(gd.find("Sketch"), 0, 120, 60, 6, GREEN)
        s.tick()
        self.assertTrue(self.g.request_undo())  # Ctrl+Z while Krita is still finishing the stroke
        for _ in range(3):
            s.tick()
        gd.idle = True
        s.settle(self)
        self.assertEqual(self.both("Sketch", 10, 121), (CLEAR, CLEAR))

    def test_new_stroke_after_press_cancels(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 100, 60, 6, BLUE)  # S0
        gd = self.gd
        gd.idle = False
        gd.paint(gd.find("Sketch"), 0, 120, 60, 6, GREEN)  # S1, still finishing
        s.tick()
        self.assertTrue(self.g.request_undo())
        self.g.note_input()  # the user starts S2 before the undo could run
        gd.paint(gd.find("Sketch"), 0, 140, 60, 6, YELLOW)
        for _ in range(3):
            s.tick()
        gd.idle = True
        s.settle(self)
        self.assertIn("Undo cancelled: you started drawing again.", events(self.g, "undo-warn"))
        for y, c in ((101, BLUE), (121, GREEN), (141, YELLOW)):
            self.assertEqual(self.both("Sketch", 10, y), (c, c))

    def test_long_busy_drops_request(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 120, 60, 6, GREEN)
        self.gd.idle = False  # e.g. the Transform tool is open
        self.assertTrue(self.g.request_undo())
        s.pump(lambda: not self.g.undo.queue, timeout=3)
        self.gd.idle = True
        s.settle(self)
        self.assertTrue(any(t.startswith("Undo skipped: Krita is still busy") for t in events(self.g, "undo-warn")))
        self.assertEqual(self.both("Sketch", 10, 121), (GREEN, GREEN))

    def test_waits_for_the_hosts_answer(self):
        s = self.s
        gd = self.gd
        gd.paint(gd.find("Sketch"), 0, 120, 60, 6, GREEN)  # a tap
        self.assertTrue(self.g.request_undo())
        for _ in range(40):  # the host is not running: no answer yet
            self.g.tick()
            time.sleep(0.002)
        self.assertEqual(self.g.undo_state()["waiting"], "host")
        self.assertEqual(px(gd, "Sketch", 10, 121), GREEN)
        s.settle(self)
        self.assertEqual(self.both("Sketch", 10, 121), (CLEAR, CLEAR))

    def test_undone_pixels_wait_while_busy(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 0, 300, 200, GREEN)
        self.assertTrue(self.g.request_undo())
        end = time.time() + 5
        while self.g.undo.pending is None and time.time() < end:
            self.g.tick()  # only the guest: the host has not seen the request yet
            time.sleep(0.002)
        self.assertIsNotNone(self.g.undo.pending)
        self.gd.idle = False  # the user starts something before the answer arrives
        for _ in range(10):
            s.tick()
            time.sleep(0.003)
        self.assertEqual(px(self.gd, "Sketch", 150, 100), GREEN)
        self.gd.idle = True
        s.settle(self)
        self.assertEqual(self.both("Sketch", 150, 100), (CLEAR, CLEAR))


class TallCanvasTests(unittest.TestCase):
    """Slow scanning (few strips per tick) on a tall canvas."""

    def setUp(self):
        cfg = undo_cfg()
        cfg.scan_strip_cap = 2
        d = FakeDoc(128, 2048)
        d.add("Background")
        d.add("Sketch")
        self.s = UGroup(d, cfg=cfg)
        self.addCleanup(self.s.close)
        self.s.sync(self, "join")
        self.s.settle(self)
        self.h, self.hd = self.s.host, self.s.hdoc

    def test_tap_the_scanner_has_not_seen(self):
        s, hd = self.s, self.hd
        s.stroke(self, self.h, "Sketch", 0, 10, 40, 4, GREEN)
        hd.paint(hd.find("Sketch"), 0, 1900, 40, 4, BLUE)  # e.g. a fill, then Ctrl+Z at once
        self.assertTrue(self.h.request_undo())
        s.settle(self)
        self.assertEqual(px(hd, "Sketch", 5, 1901), CLEAR)
        self.assertEqual(px(hd, "Sketch", 5, 11), GREEN)

    def test_follow_up_during_seal_merges_never_splits(self):
        s, hd = self.s, self.hd
        s.stroke(self, self.h, "Sketch", 0, 10, 40, 4, GREEN, settle=False)
        s.stroke(self, self.h, "Sketch", 0, 1500, 40, 4, BLUE)  # started before A was sealed
        self.assertEqual(self.h.undo_state()["undo"], 1)
        s.undo(self, self.h)
        self.assertEqual(px(hd, "Sketch", 5, 11), CLEAR)
        self.assertEqual(px(hd, "Sketch", 5, 1501), CLEAR)

    def test_unscanned_change_after_an_earlier_undo(self):
        """Review finding: the pre-undo flush was skipped forever after any earlier request."""
        s, hd = self.s, self.hd
        s.stroke(self, self.h, "Sketch", 0, 10, 40, 4, GREEN)  # A
        s.stroke(self, self.h, "Sketch", 0, 30, 40, 4, BLUE)  # B
        s.undo(self, self.h)  # undoes B
        time.sleep(0.35)
        hd.paint(hd.find("Sketch"), 0, 1900, 40, 4, YELLOW)  # C: e.g. a fill by keyboard, not yet scanned
        self.assertTrue(self.h.request_undo())
        s.settle(self)
        self.assertEqual(px(hd, "Sketch", 5, 1901), CLEAR)  # C undone
        self.assertEqual(px(hd, "Sketch", 5, 11), GREEN)  # not A

    def test_layer_delete_while_closing_a_stroke_keeps_it_whole(self):
        """Review finding: a layer delete during the seal split the stroke into two units."""
        s, hd = self.s, self.hd
        hd.idle = False
        sk = hd.find("Sketch")
        hd.paint(sk, 0, 10, 40, 4, GREEN)
        hd.paint(sk, 0, 1500, 40, 4, GREEN)  # far down: read late by the slow scanner
        for _ in range(3):
            s.tick()
        hd.idle = True
        s.pump(lambda: self.h.undo.tracker.state == "tail", timeout=5)
        hd.remove(hd.find("Background"))
        s.settle(self)
        s.undo(self, self.h)  # the delete: refused
        s.undo(self, self.h)  # the whole stroke
        self.assertEqual(px(hd, "Sketch", 5, 11), CLEAR)
        self.assertEqual(px(hd, "Sketch", 5, 1501), CLEAR)

    def test_spaced_strokes_are_separate(self):
        s, hd = self.s, self.hd
        s.stroke(self, self.h, "Sketch", 0, 10, 40, 4, GREEN)
        s.stroke(self, self.h, "Sketch", 0, 1500, 40, 4, BLUE)
        self.assertEqual(self.h.undo_state()["undo"], 2)
        s.undo(self, self.h)
        self.assertEqual(px(hd, "Sketch", 5, 11), GREEN)
        self.assertEqual(px(hd, "Sketch", 5, 1501), CLEAR)


class StructureTests(Base):
    def test_layer_change_is_a_barrier(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 120, 60, 6, GREEN)
        self.gd.find("Ink").props["name"] = "Ink 2"
        s.settle(self)
        s.undo(self, self.g)
        self.assertTrue(any(t.startswith("Layer changes") for t in events(self.g, "undo-warn")))
        self.assertEqual(self.both("Sketch", 10, 121), (GREEN, GREEN))
        s.undo(self, self.g)
        self.assertEqual(self.both("Sketch", 10, 121), (CLEAR, CLEAR))

    def test_delete_in_same_action_refused(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 100, 60, 6, BLUE)
        gd = self.gd
        gd.idle = False
        gd.paint(gd.find("Sketch"), 0, 120, 60, 6, GREEN)
        gd.remove(gd.find("Ink"))
        for _ in range(4):
            s.tick()
            time.sleep(0.01)
        gd.idle = True
        s.settle(self)
        s.undo(self, self.g)
        self.assertIn("That change also deleted layers, so it can't be undone during a session.",
                      events(self.g, "undo-warn"))
        self.assertEqual(self.both("Sketch", 10, 121), (GREEN, GREEN))
        s.undo(self, self.g)
        self.assertEqual(self.both("Sketch", 10, 101), (CLEAR, CLEAR))

    def test_deleted_layer_does_not_block_undo(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 100, 60, 6, BLUE)
        s.hold(self.g)
        gd = self.gd
        gd.paint(gd.find("Ink"), 120, 80, 30, 30, GREEN)
        s.pump(lambda: self.g.undo.hist.cur is None and self.g.undo.tracker.state == "sealed", timeout=5)
        hd = self.hd
        hd.remove(hd.find("Ink"))  # the host deletes it while my patches are still on their way
        s.pump(lambda: "Ink" not in [c.props["name"] for c in hd.find("Group").children] and s.quiet(self.h))
        s.release(self.g)
        s.settle(self)
        s.undo(self, self.g)  # the Ink change: nothing left to undo, skipped
        s.settle(self)
        self.assertEqual(self.both("Sketch", 10, 101), (CLEAR, CLEAR))
        self.assertIsNone(self.g.undo_state()["waiting"])

    def test_resize_clears_history(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 100, 60, 6, BLUE)
        self.hd.resize(320, 210)
        s.pump(lambda: self.gd.size() == (320, 210))
        s.settle(self)
        self.assertIn("Your undo history was cleared because the canvas size changed.", events(self.g, "undo"))
        s.undo(self, self.g)
        self.assertEqual(self.both("Sketch", 10, 101), (BLUE, BLUE))

    def test_krita_command_without_pixels_is_a_barrier(self):
        s = self.s
        gd = self.gd
        gd.idle = False
        gd.paint(gd.find("Sketch"), 0, 120, 60, 6, GREEN)
        s.tick()
        self.g.note_native_action("Freehand Brush Stroke")  # reported while the stroke runs: absorbed
        for _ in range(3):
            s.tick()
        gd.idle = True
        s.settle(self)
        time.sleep(0.05)
        self.g.note_native_action("Select Rectangle")  # no pixels follow
        s.settle(self)
        s.undo(self, self.g)
        self.assertTrue(any("(Select Rectangle) can't be undone" in t for t in events(self.g, "undo-warn")))
        self.assertEqual(self.both("Sketch", 10, 121), (GREEN, GREEN))
        s.undo(self, self.g)
        self.assertEqual(self.both("Sketch", 10, 121), (CLEAR, CLEAR))

    def test_late_report_of_a_stroke_is_absorbed(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 120, 60, 6, GREEN)
        self.g.note_native_action("Freehand Brush Stroke")  # arrives after the unit was closed
        s.settle(self)
        s.undo(self, self.g)
        self.assertEqual(self.both("Sketch", 10, 121), (CLEAR, CLEAR))

    def test_frame_change_is_not_an_edit(self):
        s = self.s
        s.stroke(self, self.g, "Sketch", 0, 120, 60, 6, GREEN)
        gd = self.gd
        gd.time = 5
        gd.paint(gd.find("Sketch"), 0, 0, 300, 50, YELLOW)  # what the other frame shows
        s.settle(self)
        self.assertIn("Your undo history was cleared because the animation frame changed.", events(self.g, "undo"))
        s.undo(self, self.g)
        self.assertEqual(self.both("Sketch", 10, 10), (YELLOW, YELLOW))
        self.assertEqual(self.both("Sketch", 10, 121), (GREEN, GREEN))


class MiscTests(Base):
    def test_remote_changes_mark_the_document_modified(self):
        before = self.hd.modified
        self.s.stroke(self, self.g, "Sketch", 0, 120, 60, 6, GREEN)
        self.assertGreater(self.hd.modified, before)
        before = self.gd.modified
        self.s.undo(self, self.g)
        self.assertGreater(self.gd.modified, before)

    def test_history_limits(self):
        s = self.s
        self.g.cfg.undo_max_units = 3
        for i in range(5):
            s.stroke(self, self.g, "Sketch", 0, 100 + 5 * i, 60, 3, GREEN)
        self.assertEqual(self.g.undo_state()["undo"], 3)
        for _ in range(4):
            s.undo(self, self.g)
        self.assertEqual(self.both("Sketch", 10, 101), (GREEN, GREEN))  # the two oldest stay
        self.assertEqual(self.both("Sketch", 10, 111), (CLEAR, CLEAR))

    def test_host_forgets_old_changes(self):
        s = self.s
        self.h.server.max_units = 2
        for i in range(3):
            s.stroke(self, self.g, "Sketch", 0, 100 + 5 * i, 60, 3, GREEN)
        for _ in range(3):
            s.undo(self, self.g)
        self.assertTrue(any("too old to undo" in t for t in events(self.g, "undo-warn")))
        self.assertEqual(self.both("Sketch", 10, 101), (GREEN, GREEN))  # the oldest: kept
        self.assertEqual(self.both("Sketch", 10, 106), (CLEAR, CLEAR))

    def test_engine_bug_does_not_stop_the_session(self):
        s = self.s

        def boom(*a):
            raise RuntimeError("test")
        self.g.undo.run = boom
        self.assertTrue(self.g.request_undo())
        s.stroke(self, self.h, "Sketch", 0, 140, 60, 6, YELLOW)
        self.assertEqual(self.both("Sketch", 10, 141), (YELLOW, YELLOW))
        self.assertTrue(self.g.undo.hist.broken)
        self.assertFalse(self.g.closed)
        self.g.events = [e for e in self.g.events if e[0] != "error"]  # expected here

    def test_undo_bug_switches_undo_off_but_sync_goes_on(self):
        s = self.s

        def boom(*a):
            raise RuntimeError("test")
        self.g.undo.hist.unit_for_edit = boom
        s.stroke(self, self.g, "Sketch", 0, 120, 60, 6, GREEN)
        self.assertTrue(self.g.undo.hist.broken)
        self.assertFalse(self.g.request_undo())
        s.stroke(self, self.h, "Sketch", 0, 140, 60, 6, YELLOW)
        self.assertEqual(self.both("Sketch", 10, 141), (YELLOW, YELLOW))
        self.assertEqual(self.both("Sketch", 10, 121), (GREEN, GREEN))
        self.g.events = [e for e in self.g.events if e[0] != "error"]  # expected here

    def test_old_plugin_version_is_refused_with_a_message(self):
        k = socket.create_connection(("127.0.0.1", self.h.port), timeout=5)
        try:
            k.sendall(P.encode_frame({"t": "hello", "app": P.APP_ID, "proto": 2, "name": "Old"}))
            hl, pl = P.FRAME_HEAD.unpack(k.recv(8))
            header = json.loads(k.recv(hl))
            self.assertEqual(header.get("t"), "reject")
        finally:
            k.close()
        self.s.pump(lambda: any("another plugin version" in t for t in events(self.h)), timeout=5)
        self.assertTrue(any("another plugin version" in t for t in events(self.h)))


class ThreePeopleUndoTests(Base):
    hosts_guests = 2

    def test_three_people(self):
        s = self.s
        g1, g2 = s.guests
        s.stroke(self, g1, "Sketch", 0, 100, 200, 10, GREEN)
        s.stroke(self, g2, "Sketch", 50, 105, 50, 20, YELLOW)
        s.stroke(self, self.h, "Sketch", 150, 100, 10, 10, BLUE)
        s.undo(self, g1)
        for d in s.docs():
            self.assertEqual(px(d, "Sketch", 10, 101), CLEAR)
            self.assertEqual(px(d, "Sketch", 60, 106), YELLOW)
            self.assertEqual(px(d, "Sketch", 155, 101), BLUE)

    def test_leaving_freezes_their_changes(self):
        s = self.s
        g1, g2 = s.guests
        s.stroke(self, g1, "Sketch", 0, 100, 60, 6, GREEN)
        g1.stop("bye")
        s.guests.remove(g1)
        s.gdocs.pop(0)
        s.pump(lambda: len(self.h.peers) == 2)
        self.assertFalse(any(k[0] != LOCAL and k[0] != g2.my_id for k in self.h.server.units))
        self.assertEqual(px(self.hd, "Sketch", 10, 101), GREEN)


class FuzzTests(unittest.TestCase):
    def test_random_sessions_converge_and_full_undo_restores(self):
        for seed in range(10):
            with self.subTest(seed=seed):
                self._run(seed)

    def _run(self, seed):
        rnd = random.Random(seed)
        s = UGroup(make_host_doc())
        try:
            s.sync(self, "join")
            s.settle(self)
            start = {n: snap(s.hdoc, n) for n in ("Sketch", "Ink")}
            people = [s.host, s.guest]
            colors = [GREEN, BLUE, YELLOW, RED, PURPLE, CLEAR]
            for _ in range(14):
                who = rnd.choice(people)
                act = rnd.random()
                if act < 0.55:
                    x, y = rnd.randrange(0, 260), rnd.randrange(0, 180)
                    s.stroke(self, who, rnd.choice(["Sketch", "Ink"]), x, y, rnd.randrange(1, 40),
                             rnd.randrange(1, 20), rnd.choice(colors), busy=rnd.choice([0, 1, 3]),
                             settle=rnd.random() < 0.7)
                elif act < 0.85:
                    who.request_undo()
                else:
                    who.request_redo()
                for _ in range(rnd.randrange(0, 4)):
                    s.tick()
            s.settle(self, "fuzz seed %d did not settle" % seed)
            s.sync(self, "fuzz seed %d did not converge" % seed)
            for who in people:  # everyone undoes everything
                while who.undo_state()["undo"]:
                    s.undo(self, who)
            s.sync(self)
            for n, data in start.items():
                self.assertEqual(snap(s.hdoc, n), data, "seed %d: %s not back to the start" % (seed, n))
        finally:
            s.close()


class ServerHistoryTests(unittest.TestCase):
    """The host's per-tile history against a straightforward model."""

    def test_recompute_matches_a_model(self):
        rnd = random.Random(7)
        info = {"w": 64, "h": 64, "ps": 4}
        srv = Server(info, lambda item: None)
        srv.add_local("h")
        srv.peers[1] = srv.peers[LOCAL].__class__(1, "g", None)
        srv.handle_ops(LOCAL, {"seq": 1, "ops": [{"op": "create", "id": "L1", "parent": "", "index": 0,
                                                   "type": "paint", "props": {}, "vis": True}]})
        n = 64 * 64 * 4
        ops = []  # model: (pid, unit, vals, mask) in order; plus live flags
        live = {}
        units = {0: 0, 1: 0}
        pseq = 0
        for step in range(120):
            pid = rnd.choice([0, 1])
            if rnd.random() < 0.6 or not ops:
                if rnd.random() < 0.3:
                    units[pid] += 1
                u = units[pid]
                vals = bytes(rnd.choice([0, 255]) for _ in range(4)) * (64 * 64)
                pm = bytes(rnd.choice([0, 0, 0, 255]) for _ in range(64 * 64))
                pseq += 1
                srv.handle_patch(pid, {"layer": "L1", "pseq": pseq, "unit": u},
                                 [(0, 0, 64, 64, n, P.deflate(vals), P.deflate(pm))])
                ops.append((pid, u, vals, pixops.pixels_to_mask(pm, 4)))
                live.setdefault((pid, u), True)
            else:
                pid, u = rnd.choice(sorted(live))
                kind = "undo" if live[(pid, u)] else "redo"
                srv.handle_undo(pid, {"t": kind, "unit": u, "req": step})
                live[(pid, u)] = kind == "redo"
                if units[pid] == u:
                    units[pid] += 1  # a client only undoes closed actions: new edits get a new unit
            state = bytes(n)
            for p, u, vals, m in ops:
                if live[(p, u)]:
                    state = pixops.select(state, vals, m, n)
            tile = srv.layers["L1"].tiles.get((0, 0))
            got = P.inflate(tile[0], n) if tile else bytes(n)
            self.assertEqual(got, state, "step %d" % step)


if __name__ == "__main__":
    unittest.main()
