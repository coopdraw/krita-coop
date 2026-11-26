"""End-to-end tests: host + guest sessions on fake documents, over real
localhost sockets.  Run with:  python -m unittest discover -s tests -v
"""

import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fakedoc import FakeDoc  # noqa: E402
from krita_coop import net  # noqa: E402
from krita_coop import protocol as P  # noqa: E402
from krita_coop.session import Config, GuestSession, HostSession  # noqa: E402
from krita_coop.tree import Tree, apply_ops, diff_ops  # noqa: E402

RED, BLUE, GREEN, WHITE = b"\x00\x00\xff\xff", b"\xff\x00\x00\xff", b"\x00\xff\x00\xff", b"\xff\xff\xff\xff"
YELLOW = b"\x00\xff\xff\xff"


def fast_cfg():
    c = Config()
    c.tree_interval = 0.0
    c.refresh_interval = 0.0
    c.scan_fast = c.scan_slow = c.scan_bulk = 0.05
    return c


def snapshot(sess, doc):
    st = doc.read_structure()

    def walk(pu):
        out = []
        for u in st.children.get(pu, []):
            ln = st.nodes[u]
            if ln.kind == "other":
                continue
            p = ln.props
            body = hash(bytes(doc.nodes[u].pixels)) if ln.kind == "paint" else walk(u)
            out.append((sess.sid_of.get(u), ln.kind, p["name"], p["opacity"], p["blend"], p["locked"], body))
        return out

    return walk("")


def px(doc, layer, x, y):
    n = doc.find(layer)
    off = (y * doc.w + x) * doc.ps
    return bytes(n.pixels[off:off + doc.ps])


class Group(object):
    """A host and N guests."""

    def __init__(self, hdoc, guests=1, code=None, guest_code=None):
        self.hdoc = hdoc
        self.host = HostSession(hdoc, "Hana", port=0, code=code, cfg=fast_cfg())
        self.host.start()
        self.gdocs = [None] * guests
        self.guests = []
        for i in range(guests):
            def factory(info, i=i):
                d = FakeDoc(info["w"], info["h"], info["ps"], "guest%d" % i)
                d.add("Background")  # Krita gives new documents a default layer
                self.gdocs[i] = d
                return d
            g = GuestSession(factory, "Gus%d" % i if guests > 1 else "Gus", "127.0.0.1", self.host.port,
                             guest_code or self.host.code, cfg=fast_cfg())
            g.start()
            self.guests.append(g)

    @property
    def guest(self):
        return self.guests[0]

    @property
    def gdoc(self):
        return self.gdocs[0]

    def tick(self):
        self.host.tick()
        for g in self.guests:
            g.tick()

    def pump(self, cond, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            self.tick()
            if cond():
                for _ in range(6):  # let in-flight messages land too
                    self.tick()
                    time.sleep(0.01)
                if cond():
                    return True
            time.sleep(0.004)
        return False

    def converged(self):
        if not all(g.live for g in self.guests):
            return False
        ref = snapshot(self.host, self.hdoc)
        return all(snapshot(g, d) == ref for g, d in zip(self.guests, self.gdocs))

    def sync(self, test, msg="did not converge"):
        if not self.pump(self.converged):
            test.fail("%s\nhost:  %s\nguests: %s\nhost log: %s\nguest log: %s" % (
                msg, snapshot(self.host, self.hdoc),
                [snapshot(g, d) if d else None for g, d in zip(self.guests, self.gdocs)],
                self.host.events[-6:], self.guest.events[-6:]))

    def close(self):
        for g in self.guests:
            g.stop("test over")
        self.host.stop("test over")
        errors = [t for s in [self.host] + self.guests for k, t in s.events if k == "error"]
        if errors:
            raise AssertionError("engine logged internal errors:\n" + "\n".join(errors))


def make_host_doc():
    d = FakeDoc(300, 200)
    bg = d.add("Background")
    d.paint(bg, 0, 0, 300, 200, WHITE)
    sk = d.add("Sketch")
    d.paint(sk, 10, 10, 50, 40, RED)
    g = d.add("Group", kind="group")
    ink = d.add("Ink", parent=g)
    d.paint(ink, 100, 70, 90, 90, BLUE)
    d.add("Vector", kind="other")  # e.g. a vector layer: not shared
    return d


class SharedLayerTests(unittest.TestCase):
    def setUp(self):
        self.s = Group(make_host_doc())
        self.addCleanup(self.s.close)
        self.s.sync(self, "initial join")

    def test_join_copies_layers_unlocked(self):
        g = self.s.gdoc
        self.assertEqual(g.names(), ["Background", "Sketch", "Group"])
        self.assertEqual(g.names(g.find("Group")), ["Ink"])
        self.assertFalse(g.find("Sketch").props["locked"])
        self.assertEqual(self.s.guest.peers, {0: "Hana", 1: "Gus"})

    def test_host_paint_reaches_guest(self):
        h = self.s.hdoc
        h.paint(h.find("Sketch"), 200, 150, 30, 30, GREEN)
        self.s.sync(self)

    def test_guest_paints_on_host_layer(self):
        g = self.s.gdoc
        g.paint(g.find("Sketch"), 150, 20, 40, 40, GREEN)
        self.s.sync(self)
        self.assertEqual(px(self.s.hdoc, "Sketch", 160, 30), GREEN)

    def test_simultaneous_same_layer_same_tiles(self):
        h, g = self.s.hdoc, self.s.gdoc
        # Both paint the same layer, in the same 64x64 tiles, before either side syncs.
        h.paint(h.find("Sketch"), 0, 0, 30, 60, GREEN)
        g.paint(g.find("Sketch"), 34, 0, 30, 60, YELLOW)
        self.s.sync(self)
        for d in (h, g):
            self.assertEqual(px(d, "Sketch", 5, 50), GREEN)
            self.assertEqual(px(d, "Sketch", 40, 50), YELLOW)
            self.assertEqual(px(d, "Sketch", 20, 20), GREEN)  # host's stroke over the old red

    def test_simultaneous_overlapping_strokes(self):
        h, g = self.s.hdoc, self.s.gdoc
        h.paint(h.find("Sketch"), 20, 20, 60, 60, GREEN)
        g.paint(g.find("Sketch"), 50, 50, 60, 60, YELLOW)
        self.s.sync(self)
        for d in (h, g):
            self.assertEqual(px(d, "Sketch", 25, 25), GREEN)
            self.assertEqual(px(d, "Sketch", 100, 100), YELLOW)
            self.assertIn(px(d, "Sketch", 60, 60), (GREEN, YELLOW))

    def test_host_busy_guest_keeps_drawing(self):
        h, g = self.s.hdoc, self.s.gdoc
        h.idle = False  # host is in the middle of a long stroke
        h.paint(h.find("Sketch"), 0, 100, 100, 20, GREEN)
        g.paint(g.find("Sketch"), 0, 130, 100, 20, YELLOW)
        for _ in range(40):
            self.s.tick()
            time.sleep(0.005)
        self.assertNotEqual(px(h, "Sketch", 50, 140), YELLOW)  # not written while busy
        self.assertIsNotNone(self.s.host.waiting_since)
        self.assertEqual(px(g, "Sketch", 50, 110), GREEN)  # the host's stroke still reaches the guest
        h.idle = True
        self.s.sync(self)
        self.assertEqual(px(h, "Sketch", 50, 140), YELLOW)

    def test_guest_busy_defers_but_keeps_own_strokes(self):
        h, g = self.s.hdoc, self.s.gdoc
        g.idle = False
        h.paint(h.find("Sketch"), 0, 0, 64, 64, GREEN)
        g.paint(g.find("Sketch"), 10, 10, 10, 10, YELLOW)  # same tile, during the "stroke"
        for _ in range(30):
            self.s.tick()
            time.sleep(0.005)
        self.assertNotEqual(px(g, "Sketch", 40, 40), GREEN)  # host's paint held back while busy
        g.idle = True
        self.s.sync(self)
        self.assertEqual(px(g, "Sketch", 15, 15), YELLOW)
        self.assertEqual(px(g, "Sketch", 40, 40), GREEN)

    def test_guest_new_layer(self):
        g = self.s.gdoc
        mine = g.add("Guest art", index=2)
        g.paint(mine, 5, 5, 280, 20, GREEN)
        self.s.sync(self)
        self.assertEqual(self.s.hdoc.names()[:3], ["Background", "Sketch", "Guest art"])

    def test_guest_deletes_host_layer(self):
        g = self.s.gdoc
        g.remove(g.find("Sketch"))
        self.s.sync(self)
        self.assertIsNone(self.s.hdoc.find("Sketch"))

    def test_lock_and_props_sync(self):
        g = self.s.gdoc
        sk = g.find("Sketch")
        sk.props.update(locked=True, opacity=128, blend="multiply", name="Sketch v2")
        self.s.sync(self)
        hs = self.s.hdoc.find("Sketch v2")
        self.assertEqual((hs.props["locked"], hs.props["opacity"], hs.props["blend"]), (True, 128, "multiply"))

    def test_reorder_and_regroup(self):
        g = self.s.gdoc
        g.move(g.find("Ink"), None, None)
        g.move(g.find("Sketch"), g.find("Group"), 0)
        self.s.sync(self)
        h = self.s.hdoc
        self.assertEqual(h.names(), ["Background", "Group", "Vector", "Ink"])
        self.assertEqual(h.names(h.find("Group")), ["Sketch"])

    def test_move_out_then_delete_group(self):
        g = self.s.gdoc
        g.move(g.find("Ink"), None, 0)
        g.remove(g.find("Group"))
        self.s.sync(self)
        self.assertIsNotNone(self.s.hdoc.find("Ink"))
        self.assertIsNone(self.s.hdoc.find("Group"))

    def test_duplicated_layer_with_content(self):
        g = self.s.gdoc
        dup = g.add("Sketch copy")
        dup.pixels[:] = g.find("Sketch").pixels  # like Ctrl+J
        self.s.sync(self)
        self.assertEqual(px(self.s.hdoc, "Sketch copy", 20, 20), RED)

    def test_shared_undo_propagates(self):
        g = self.s.gdoc
        sk = g.find("Sketch")
        before = bytes(sk.pixels)
        h = self.s.hdoc
        h.paint(h.find("Sketch"), 200, 100, 50, 50, GREEN)
        self.s.sync(self)
        sk.pixels[:] = before  # Krita's undo restoring the layer to an earlier state
        self.s.sync(self)
        self.assertNotEqual(px(h, "Sketch", 220, 120), GREEN)

    def test_host_resizes_canvas(self):
        h = self.s.hdoc
        h.resize(200, 150)
        h.paint(h.find("Sketch"), 150, 100, 40, 40, GREEN)
        self.s.sync(self, "resize did not converge")
        self.assertEqual(self.s.gdoc.size(), (200, 150))
        self.assertFalse(any("restoring" in t for _, t in self.s.guest.events))

    def test_guest_resize_is_reverted(self):
        g = self.s.gdoc
        g.resize(100, 100)
        self.s.sync(self, "guest resize was not undone")
        self.assertEqual(g.size(), (300, 200))

    def test_chat(self):
        self.s.guest.send_chat("hello there")
        self.assertTrue(self.s.pump(lambda: any(t == "Gus: hello there" for _, t in self.s.host.events)))
        self.s.host.send_chat("hi")
        self.assertTrue(self.s.pump(lambda: any(t == "Hana: hi" for _, t in self.s.guest.events)))


class ThreePeopleTests(unittest.TestCase):
    def test_three_people_one_layer(self):
        s = Group(make_host_doc(), guests=2)
        self.addCleanup(s.close)
        s.sync(self, "join")
        a, b, c = s.hdoc, s.gdocs[0], s.gdocs[1]
        a.paint(a.find("Sketch"), 0, 150, 300, 10, GREEN)
        b.paint(b.find("Sketch"), 0, 165, 300, 10, YELLOW)
        c.paint(c.find("Sketch"), 0, 180, 300, 10, BLUE)
        s.sync(self)
        for d in (a, b, c):
            self.assertEqual((px(d, "Sketch", 150, 155), px(d, "Sketch", 150, 170), px(d, "Sketch", 150, 185)),
                             (GREEN, YELLOW, BLUE))


class AuthTests(unittest.TestCase):
    def test_wrong_code_rejected(self):
        s = Group(make_host_doc(), code="AAAA-BBBB", guest_code="AAAA-CCCC")
        self.addCleanup(s.close)
        self.assertTrue(s.pump(lambda: s.guest.closed, timeout=5))
        self.assertIn("wrong session code", s.guest.close_reason)

    def test_code_is_case_and_dash_insensitive(self):
        s = Group(make_host_doc(), code="AAAA-BBBB", guest_code="aaaabbbb")
        self.addCleanup(s.close)
        s.sync(self)


def _closed_by_peer(sock, timeout):
    """True if the other end closes ``sock`` within ``timeout`` seconds."""
    sock.settimeout(0.05)
    end = time.time() + timeout
    while time.time() < end:
        try:
            if sock.recv(4096) == b"":
                return True
        except socket.timeout:
            pass
        except OSError:
            return True
    return False


class StrangerTests(unittest.TestCase):
    """Someone who finds the host's port (it may be open to the internet) but has no code."""

    def setUp(self):
        self.s = Group(make_host_doc())
        self.addCleanup(self.s.close)
        self.s.sync(self, "join")
        self.socks = []
        self.addCleanup(lambda: [k.close() for k in self.socks])

    def stranger(self):
        k = socket.create_connection(("127.0.0.1", self.s.host.port), timeout=5)
        self.socks.append(k)
        return k

    def still_works(self):
        a = self.s.hdoc
        a.paint(a.find("Sketch"), 0, 150, 300, 10, GREEN)
        self.s.sync(self, "host stopped working for the real guest")

    def test_huge_frame_before_auth_is_refused(self):
        k = self.stranger()
        k.sendall(P.FRAME_HEAD.pack(20, 200 << 20))  # announces a 200 MB payload
        self.assertTrue(_closed_by_peer(k, 3))
        self.still_works()

    def test_pinging_without_code_is_dropped(self):
        old = net.HANDSHAKE_TIMEOUT
        net.HANDSHAKE_TIMEOUT = 0.5
        self.addCleanup(setattr, net, "HANDSHAKE_TIMEOUT", old)
        k = self.stranger()
        ping = P.encode_frame({"t": "ping"})
        end, dropped = time.time() + 5, False
        while time.time() < end and not dropped:
            try:
                k.sendall(ping)
            except OSError:
                dropped = True
                break
            dropped = _closed_by_peer(k, 0.1)
        self.assertTrue(dropped, "a stranger kept the connection open with pings")
        self.still_works()

    def test_waiting_strangers_are_capped(self):
        old = net.MAX_PENDING
        net.MAX_PENDING = 2
        self.addCleanup(setattr, net, "MAX_PENDING", old)
        self.stranger(), self.stranger()
        time.sleep(0.2)  # let the host accept both
        self.assertTrue(_closed_by_peer(self.stranger(), 3), "third stranger was not turned away")
        self.still_works()


class TreeTests(unittest.TestCase):
    def test_diff_apply_roundtrip(self):
        def node(name, typ="paint"):
            return {"type": typ, "vis": True, "name": name, "opacity": 255, "blend": "normal", "ia": False,
                    "cl": 0, "locked": False}
        base = Tree()
        base.add("a", "", node("A"))
        base.add("g", "", node("G", "group"))
        base.add("b", "g", node("B"))
        cur = base.copy()
        cur.move("b", "", 0)
        cur.nodes["a"]["name"] = "A2"
        cur.add("c", "g", node("C"))
        host = base.copy()
        apply_ops(host, diff_ops(base, cur))
        self.assertEqual(host.preorder(), cur.preorder())
        self.assertEqual(host.nodes["a"]["name"], "A2")


if __name__ == "__main__":
    unittest.main()
