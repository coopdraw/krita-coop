"""Performance check inside Krita (kritarunner): 3000x3000, several full layers.

Simulates a brush stroke (a dab per tick) on the host and measures:
* how long each session's tick() blocks the GUI thread,
* stroke latency until the dab is visible in the guest's layer,
* how long joining (full transfer) takes.
Output: krita_perf.log
"""

import os
import sys
import time
import traceback
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["KRITA_COOP_NO_REGISTER"] = "1"
OUT = os.path.join(HERE, "krita_perf.log")


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] * 1000 if xs else 0


def main(*args):
    lines = []
    try:
        from krita import Krita
        from PyQt5.QtCore import QCoreApplication

        from krita_coop.kritadoc import KritaDoc
        from krita_coop.session import GuestSession, HostSession

        k = Krita.instance()
        W = H = 3000
        hd = k.createDocument(W, H, "perf", "RGBA", "U8", "", 300.0)
        root = hd.rootNode()
        layers = [root.childNodes()[0]]
        for i in range(6):
            n = hd.createNode("L%d" % i, "paintlayer")
            root.addChildNode(n, None)
            layers.append(n)
        # fill with gradients (compressible but not trivial)
        for li, n in enumerate(layers):
            for y in range(0, H, 64):
                h = min(64, H - y)
                row = bytes(((x * 7 + li * 40) & 255, (y + li * 13) & 255, (x + y) & 255, 255 if li == 0 else 160)
                            for x in range(0, W, 1)) if False else None
                base = bytes([(li * 37) & 255, (y // 12) & 255, 128, 255 if li == 0 else 140])
                n.setPixelData(base * (W * h), 0, y, W, h)
        hd.refreshProjection()
        active = layers[3]
        hd.setActiveNode(active)
        lines.append("active node headless: %s" % (hd.activeNode().name() if hd.activeNode() else None))

        host_kd = KritaDoc(hd)
        if hd.activeNode() is None:  # headless Krita has no view; emulate the GUI's active layer
            active_uuid = active.uniqueId().toString()
            host_kd.active_uuid = lambda: active_uuid
        host = HostSession(host_kd, "Hana", port=0)
        host.start()
        made = []

        def factory(info):
            kd = KritaDoc.create(info)
            made.append(kd)
            return kd

        guest = GuestSession(factory, "Gus", "127.0.0.1", host.port, host.code)
        guest.start()
        ht, gt = [], []

        def step():
            t0 = time.perf_counter()
            host.tick()
            t1 = time.perf_counter()
            guest.tick()
            t2 = time.perf_counter()
            ht.append(t1 - t0)
            gt.append(t2 - t1)
            QCoreApplication.processEvents()

        t0 = time.time()
        while time.time() - t0 < 120:
            step()
            if guest.state == "live" and not host.replica.busy() and not guest.pending_count() and \
                    all(c.pending_bytes == 0 for c in host.conns.values()):
                break
            time.sleep(0.016)
        # drain the last batches
        for _ in range(60):
            step()
            time.sleep(0.016)
        lines.append("join (7 layers of 3000x3000 = %d MB raw): %.1f s, sent %.1f MB"
                     % (7 * W * H * 4 >> 20, time.time() - t0, sum(c.bytes_out for c in host.conns.values()) / 1e6))
        lines.append("  during join: host tick p50 %.1f / p95 %.1f / max %.1f ms; guest p50 %.1f / p95 %.1f / max %.1f ms"
                     % (pct(ht, .5), pct(ht, .95), pct(ht, 1), pct(gt, .5), pct(gt, .95), pct(gt, 1)))

        gkd = made[0]
        sid = host.sid_of[active.uniqueId().toString()]
        if gkd.d.activeNode() is None:  # emulate the guest having that layer selected, as in the GUI
            gkd.active_uuid = lambda: guest.uuid_of.get(sid)
        gnode = gkd._nodes.get(guest.uuid_of.get(sid))
        lines.append("guest has active layer: %s" % (gnode is not None))

        # idle cost
        ht.clear()
        gt.clear()
        for _ in range(150):
            step()
            time.sleep(0.016)
        lines.append("idle: host tick p50 %.2f / p95 %.2f ms; guest p50 %.2f / p95 %.2f ms"
                     % (pct(ht, .5), pct(ht, .95), pct(gt, .5), pct(gt, .95)))

        # simulated stroke: one dab per ~16 ms for 4 s
        ht.clear()
        gt.clear()
        lat = []
        pending = []
        t_end = time.time() + 4
        i = 0
        while time.time() < t_end or pending:
            if time.time() < t_end:
                x = 100 + (i * 25) % 2800  # dabs do not overlap, so each one can be checked
                y = 300 + 60 * ((i * 25) // 2800 % 40)
                col = bytes([i & 255, 255 - (i & 255), 77, 255])
                active.setPixelData(col * 400, x, y, 20, 20)
                pending.append((time.perf_counter(), x + 10, y + 10, col))
                i += 1
            step()
            gn = gkd._nodes.get(guest.uuid_of.get(sid))
            now = time.perf_counter()
            still = []
            for (tp, px, py, col) in pending:
                if gn is not None and bytes(gn.pixelData(px, py, 1, 1)) == col:
                    lat.append(now - tp)
                elif now - tp < 10:
                    still.append((tp, px, py, col))
            pending = still
            time.sleep(0.016)
        lines.append("host stroke -> guest: %d dabs, delivered %d" % (i, len(lat)))
        lines.append("  latency p50 %.0f / p95 %.0f / max %.0f ms" % (pct(lat, .5), pct(lat, .95), pct(lat, 1)))
        lines.append("  host tick p50 %.1f / p95 %.1f / max %.1f ms; guest p50 %.1f / p95 %.1f / max %.1f ms"
                     % (pct(ht, .5), pct(ht, .95), pct(ht, 1), pct(gt, .5), pct(gt, .95), pct(gt, 1)))

        # both draw on the same layer at once; measure the guest's dabs arriving at the host
        ht.clear()
        gt.clear()
        lat = []
        pending = []
        gn = gkd._nodes.get(guest.uuid_of.get(sid))
        t_end = time.time() + 4
        i = 0
        while time.time() < t_end or pending:
            if time.time() < t_end:
                x = 100 + (i * 25) % 2800
                y = 2000 + 60 * ((i * 25) // 2800 % 15)
                col = bytes([77, i & 255, 255 - (i & 255), 255])
                gn.setPixelData(col * 400, x, y, 20, 20)  # guest dab
                active.setPixelData(b"\x10\x80\xf0\xff" * 400, x, y - 30, 20, 20)  # host dab nearby
                pending.append((time.perf_counter(), x + 10, y + 10, col))
                i += 1
            step()
            now = time.perf_counter()
            still = []
            for (tp, px, py, col) in pending:
                if bytes(active.pixelData(px, py, 1, 1)) == col:
                    lat.append(now - tp)
                elif now - tp < 10:
                    still.append((tp, px, py, col))
            pending = still
            time.sleep(0.016)
        lines.append("both drawing, guest stroke -> host: %d dabs, delivered %d" % (i, len(lat)))
        lines.append("  latency p50 %.0f / p95 %.0f / max %.0f ms" % (pct(lat, .5), pct(lat, .95), pct(lat, 1)))
        lines.append("  host tick p50 %.1f / p95 %.1f / max %.1f ms; guest p50 %.1f / p95 %.1f / max %.1f ms"
                     % (pct(ht, .5), pct(ht, .95), pct(ht, 1), pct(gt, .5), pct(gt, .95), pct(gt, 1)))
        for _ in range(60):
            step()
            time.sleep(0.016)
        same = zlib.crc32(bytes(active.pixelData(0, 0, W, H))) == zlib.crc32(bytes(gn.pixelData(0, 0, W, H)))
        lines.append("layer identical on both after drawing together: %s" % same)

        # A real stroke keeps Krita busy from pen-down to pen-up (the dab phases above are
        # separate idle "taps", which makes every dab its own undo step).  Emulate that here.
        busy = [False]
        real_idle = host_kd.is_idle
        host_kd.is_idle = lambda: not busy[0] and real_idle()
        ht.clear()
        gt.clear()
        after = []
        for stroke_i in range(3):
            busy[0] = True
            t_end = time.time() + 1.5
            i = 0
            while time.time() < t_end:
                x = 100 + (i * 25) % 2800
                y = 2600 + 60 * stroke_i
                active.setPixelData(bytes([9, i & 255, 200, 255]) * 400, x, y, 20, 20)
                i += 1
                step()
                time.sleep(0.016)
            busy[0] = False
            n0 = len(ht)
            t0 = time.time()
            while time.time() - t0 < 1.0:
                step()
                time.sleep(0.016)
            after.extend(ht[n0:])
        during = ht[:]
        lines.append("real strokes (busy while drawing), host tick: p50 %.1f / p95 %.1f ms during the stroke; "
                     "p50 %.1f / p95 %.1f / max %.1f ms in the second after pen-up"
                     % (pct(during, .5), pct(during, .95), pct(after, .5), pct(after, .95), pct(after, 1)))
        lines.append("  undo steps recorded for 3 strokes: %d" % host.undo_state()["undo"])
        host_kd.is_idle = real_idle

        # per-person undo
        def undo_quiet(sess):
            u = sess.undo
            return not u.queue and u.pending is None and u.tracker.state == "sealed" \
                and not sess.replica.sealing() and not sess.replica.unacked_keys and not sess.pending_count()

        def wait_for(cond, timeout=30.0):
            t_ = time.time()
            while time.time() - t_ < timeout:
                step()
                if cond():
                    return True
                time.sleep(0.016)
            return False

        t = time.perf_counter()
        host.replica.arm({sid})
        host.replica.seal_now(time.perf_counter() + 2.0, sid)
        lines.append("re-reading one 3000x3000 layer before an undo (flush): %.1f ms"
                     % ((time.perf_counter() - t) * 1000))
        active.setPixelData(b"\x20\x40\x60\xff" * (640 * 640), 1000, 1000, 640, 640)  # 100 tiles
        wait_for(lambda: undo_quiet(host) and host.undo_state()["undo"] > 0)
        for _ in range(30):
            step()
            time.sleep(0.016)
        n_before = host.undo_state()["undo"]
        ht.clear()
        gt.clear()
        t = time.perf_counter()
        host.request_undo()
        ok = wait_for(lambda: undo_quiet(host) and host.undo_state()["undo"] == n_before - 1)
        dt = time.perf_counter() - t
        gone = bytes(active.pixelData(1300, 1300, 1, 1)) != b"\x20\x40\x60\xff"
        on_guest = wait_for(lambda: bytes(gn.pixelData(1300, 1300, 1, 1)) == bytes(active.pixelData(1300, 1300, 1, 1)))
        lines.append("host undo of a 640x640 block (100 tiles): %.0f ms until done (ok=%s, undone=%s, guest too=%s)"
                     % (dt * 1000, ok, gone, on_guest))
        lines.append("  host tick p50 %.1f / p95 %.1f / max %.1f ms during it" % (pct(ht, .5), pct(ht, .95), pct(ht, 1)))
        t = time.perf_counter()
        hd.refreshProjection()
        lines.append("full refreshProjection at this size: %.1f ms" % ((time.perf_counter() - t) * 1000))
        errs = [t for s in (host, guest) for kk, t in s.events if kk == "error"]
        lines.append("errors: %s" % errs[:2])
        guest.stop("done")
        host.stop("done")
        for kd in made:
            kd.d.close()
        hd.close()
    except Exception:  # noqa: BLE001
        lines.append("EXCEPTION\n" + traceback.format_exc())
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
