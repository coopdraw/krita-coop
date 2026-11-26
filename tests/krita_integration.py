"""Integration test against the real Krita API (no GUI).

Run from this folder with Krita's headless runner:

    cd tests
    "C:\\Program Files\\Krita (x64)\\bin\\kritarunner.exe" -s krita_integration -f main

Results are written to tests/krita_integration.log.
"""

import os
import sys
import time
import traceback
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["KRITA_COOP_NO_REGISTER"] = "1"
OUT = os.path.join(HERE, "krita_integration.log")

LOG = []
FAILS = []


def log(*a):
    LOG.append(" ".join(str(x) for x in a))


def check(cond, what):
    log(("PASS " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


def main(*args):
    try:
        run()
    except Exception:  # noqa: BLE001
        FAILS.append("exception")
        log("EXCEPTION\n" + traceback.format_exc())
    log("RESULT: %s" % ("OK" if not FAILS else "FAILED: %s" % FAILS))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG))


def rect(node, x, y, w, h, bgra):
    node.setPixelData(bytes(bgra) * (w * h), x, y, w, h)


def px(node, x, y):
    return bytes(node.pixelData(x, y, 1, 1))


def run():
    from krita import Krita
    from PyQt5.QtCore import QCoreApplication

    from krita_coop.kritadoc import KritaDoc
    from krita_coop.session import Config, GuestSession, HostSession

    k = Krita.instance()
    log("Krita", k.version())
    W, H = 1500, 1000
    hd = k.createDocument(W, H, "hostdoc", "RGBA", "U8", "", 300.0)
    root = hd.rootNode()
    bg = root.childNodes()[0]
    rect(bg, 0, 0, W, H, b"\xff\xff\xff\xff")
    sketch = hd.createNode("Sketch", "paintlayer")
    root.addChildNode(sketch, None)
    rect(sketch, 50, 50, 300, 200, b"\x00\x00\xff\xff")
    grp = hd.createNode("Group", "grouplayer")
    root.addChildNode(grp, None)
    ink = hd.createNode("Ink", "paintlayer")
    grp.addChildNode(ink, None)
    rect(ink, 700, 400, 500, 300, b"\xff\x00\x00\x80")
    vec = hd.createVectorLayer("Vec")
    root.addChildNode(vec, None)
    hd.refreshProjection()

    cfg = Config()
    cfg.tree_interval = 0.05
    cfg.refresh_interval = 0.0
    host_kd = KritaDoc(hd)
    host = HostSession(host_kd, "Hana", port=0, cfg=cfg)
    host.start()
    made = []

    def factory(info):
        kd = KritaDoc.create(info)
        made.append(kd)
        return kd

    guest = GuestSession(factory, "Gus", "127.0.0.1", host.port, host.code, cfg)
    guest.start()
    tick_times = []

    def pump(cond, timeout=30.0):
        end = time.time() + timeout
        settle = 0
        while time.time() < end:
            t0 = time.perf_counter()
            host.tick()
            guest.tick()
            tick_times.append(time.perf_counter() - t0)
            QCoreApplication.processEvents()
            if cond():
                settle += 1
                if settle > 8:
                    return True
            else:
                settle = 0
            time.sleep(0.005)
        return False

    def snap(sess, kd):
        st = kd.read_structure()
        W_, H_ = kd.size()

        def walk(pu):
            out = []
            for u in st.children.get(pu, []):
                ln = st.nodes[u]
                if ln.kind == "other":
                    continue
                p = ln.props
                body = zlib.crc32(kd.read_rows(u, 0, H_)) if ln.kind == "paint" else walk(u)
                out.append((sess.sid_of.get(u), ln.kind, p["name"], p["opacity"], p["blend"], p["locked"], body))
            return out
        return walk("")

    def converged():
        return guest.live and made and snap(host, host_kd) == snap(guest, made[0])

    def projection_equal():
        hd.refreshProjection()
        gd_ = made[0].d
        gd_.refreshProjection()
        w_, h_ = hd.width(), hd.height()
        return zlib.crc32(bytes(hd.pixelData(0, 0, w_, h_))) == zlib.crc32(bytes(gd_.pixelData(0, 0, w_, h_)))

    def gnode(name, parent=None):
        for n in (parent or made[0].d.rootNode()).childNodes():
            if n.name() == name:
                return n
        return None

    t0 = time.time()
    check(pump(converged), "guest joins and receives all layers")
    log("  join took %.2fs" % (time.time() - t0))
    gd = made[0].d
    names = [n.name() for n in gd.rootNode().childNodes()]
    check(names == ["Background", "Sketch", "Group"], "guest layer stack %s" % names)
    check(not gnode("Sketch").locked(), "shared layers are not locked")
    check(projection_equal(), "guest canvas matches host")

    # both draw on the same layer, in the same tiles, at the same time
    GREEN, YEL = b"\x00\xff\x00\xff", b"\x00\xff\xff\xff"
    rect(sketch, 64, 300, 30, 60, GREEN)
    rect(gnode("Sketch"), 98, 300, 30, 60, YEL)
    check(pump(converged), "simultaneous strokes on one layer converge")
    gs = gnode("Sketch")
    check(px(sketch, 70, 320) == GREEN and px(sketch, 110, 320) == YEL, "host has both strokes")
    check(px(gs, 70, 320) == GREEN and px(gs, 110, 320) == YEL, "guest has both strokes")
    check(projection_equal(), "canvases identical after simultaneous strokes")

    # guest paints on a host layer, edits properties, locks it
    rect(gnode("Ink", gnode("Group")), 750, 450, 100, 100, YEL)
    gs.setName("Sketch (renamed)")
    gs.setOpacity(100)
    gs.setBlendingMode("multiply")
    gs.setLocked(True)
    check(pump(converged), "guest edits on host layers reach host")
    check(sketch.name() == "Sketch (renamed)" and sketch.opacity() == 100 and sketch.blendingMode() == "multiply"
          and sketch.locked(), "host layer props %s %s %s locked=%s"
          % (sketch.name(), sketch.opacity(), sketch.blendingMode(), sketch.locked()))
    check(px(ink, 800, 500) == YEL, "guest paint on host's Ink layer")

    # guest adds a layer; host deletes it again
    mine = gd.createNode("Gus layer", "paintlayer")
    gd.rootNode().addChildNode(mine, gnode("Sketch (renamed)"))
    rect(mine, 100, 700, 600, 100, b"\x80\x40\x20\xff")
    check(pump(converged), "guest layer + paint reaches host")
    hmine = [n for n in root.childNodes() if n.name() == "Gus layer"]
    check(len(hmine) == 1, "host got the guest layer")
    hmine[0].remove()
    check(pump(converged), "host deleting the guest's layer reaches guest")
    check(gnode("Gus layer") is None, "guest layer deleted everywhere")

    # host restructures
    sketch.setLocked(False)
    sketch.remove()
    grp.addChildNode(sketch, None)
    check(pump(converged), "host restructure reaches guest")

    # host resizes the canvas
    hd.resizeImage(0, 0, 1200, 900)
    hd.waitForDone()
    rect(ink, 900, 700, 200, 150, GREEN)
    check(pump(converged, timeout=60), "canvas resize converges")
    check((gd.width(), gd.height()) == (1200, 900), "guest canvas resized to %dx%d" % (gd.width(), gd.height()))
    check(projection_equal(), "canvases identical after resize")

    # per-person undo, on real Krita pixels (headless: no windows, so call the API instead of Ctrl+Z)
    RED, CLEAR = b"\x00\x00\xff\xff", b"\x00\x00\x00\x00"
    gink = gnode("Ink", gnode("Group"))
    hd.setModified(False)
    rect(gink, 100, 100, 200, 50, RED)  # the guest's change
    check(pump(converged), "guest paint (to undo) converges")
    check(hd.modified(), "others' paint marks the host's document as modified")
    rect(ink, 150, 120, 30, 60, GREEN)  # the host paints over part of it afterwards
    check(pump(converged), "host overpaint converges")

    def undone(sess, n):
        return lambda: converged() and sess.undo.pending is None and not sess.undo.queue and \
            sess.undo_state()["undo"] == n

    t0 = time.time()
    n0 = guest.undo_state()["undo"]
    check(n0 >= 1, "guest has undo steps (%d)" % n0)
    check(guest.request_undo(), "guest undo accepted")
    check(pump(undone(guest, n0 - 1)), "guest undo converges")
    log("  guest undo took %.2fs" % (time.time() - t0))
    for name, node in (("host", ink), ("guest", gink)):
        check(px(node, 110, 110) == CLEAR, "guest's paint undone on %s" % name)
        check(px(node, 160, 130) == GREEN, "host's later paint kept on %s" % name)
    check(projection_equal(), "canvases identical after undo")
    check(guest.request_redo(), "guest redo accepted")
    check(pump(undone(guest, n0)), "guest redo converges")
    check(px(ink, 110, 110) == RED and px(gink, 160, 130) == GREEN, "redo restores only what undo removed")
    h0 = host.undo_state()["undo"]
    check(host.request_undo(), "host undo accepted")
    check(pump(undone(host, h0 - 1)), "host undo converges")
    check(px(ink, 160, 130) == RED and px(gink, 160, 130) == RED, "host undo shows the guest's paint under it")
    check(projection_equal(), "canvases identical after host undo")

    # Merge Down: does Krita keep the lower layer (in place) or make a new one? (undo design note)
    md = k.createDocument(100, 100, "mergeprobe", "RGBA", "U8", "", 72.0)
    la = md.createNode("A", "paintlayer")
    md.rootNode().addChildNode(la, None)
    lb = md.createNode("B", "paintlayer")
    md.rootNode().addChildNode(lb, None)
    ida, idb = la.uniqueId().toString(), lb.uniqueId().toString()
    lm = lb.mergeDown()
    md.waitForDone()
    ids = [n.uniqueId().toString() for n in md.rootNode().childNodes()]
    log("  merge-down probe: lower layer kept its id=%s, upper removed=%s" % (ida in ids, idb not in ids))
    del la, lb, lm
    md.close()

    errs = [t for s in (host, guest) for kk, t in s.events if kk == "error"]
    check(not errs, "no internal errors (%s)" % (errs[:2],))
    tt = sorted(tick_times)
    log("  tick time: median %.2f ms, p95 %.2f ms, max %.2f ms (both sessions in one process)" % (
        tt[len(tt) // 2] * 1000, tt[int(len(tt) * 0.95)] * 1000, tt[-1] * 1000))
    log("  host events:", [t for _, t in host.events][-5:])
    log("  guest events:", [t for _, t in guest.events][-5:])
    guest.stop("done")
    host.stop("done")
    del gs, mine, hmine, gink
    gd.close()
    hd.close()
    log("  documents closed")
