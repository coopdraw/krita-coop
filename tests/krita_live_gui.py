"""Live-sharing test with REAL brush strokes, inside the co-op build of Krita (with its GUI).

kritarunner has no views, and Krita only paints strokes for a view, so this one runs in the
real application: ``tests/run_live_gui.ps1`` starts the co-op build with a throwaway profile
whose only plugin calls ``main()`` here, then closes Krita again.

It checks, with strokes made by Krita's own brush engine (``Node.paintLine``):

A. the native calls while strokes are running: the stroke is seen before it ends (Wash mode),
   pausing it and writing pixels meanwhile loses neither the stroke nor the written pixels;
B. a whole session (host + guest in this one Krita, over localhost): the guest sees the host's
   stroke while it is painted, the host receives the guest's paint while its own strokes run,
   per-person undo, and both ending up with identical layers.
C. the same with a stroke drawn by hand (synthetic mouse events on the canvas): it stays open
   while it is fed, like a pen stroke.

Output: krita_live_gui.log (a line starting with FAIL means a check failed).
"""

import os
import sys
import time
import traceback
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["KRITA_COOP_NO_REGISTER"] = "1"
OUT = os.path.join(HERE, "krita_live_gui.log")
TRACE = os.path.join(HERE, "krita_live_gui.trace")

W, H = 2000, 1600
CLEAR = b"\x00\x00\x00\x00"


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] * 1000 if xs else 0


def main(*args):
    lines = []

    def check(name, cond, extra=""):
        lines.append("%s  %s%s" % ("PASS" if cond else "FAIL", name, ("  [%s]" % extra) if extra else ""))
        flush()

    def note(text):
        lines.append("      " + text)
        flush()

    def flush():
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def mark(text):
        """Progress marker in the trace file: if Krita hangs, the last one says where."""
        with open(TRACE, "a", encoding="utf-8") as f:
            f.write("%s   %s\n" % (time.strftime("%H:%M:%S"), text))

    restore = []
    try:
        from krita import Krita
        from PyQt5.QtCore import QCoreApplication, QPoint

        from krita_coop.kritadoc import KritaDoc
        from krita_coop.session import GuestSession, HostSession

        k = Krita.instance()
        win = k.activeWindow()
        hd = k.createDocument(W, H, "live-host", "RGBA", "U8", "", 300.0)
        win.addView(hd)
        view = win.activeView()
        root = hd.rootNode()
        paint = hd.createNode("Paint", "paintlayer")
        root.addChildNode(paint, None)
        hd.waitForDone()
        hd.setActiveNode(paint)
        note("Krita %s, document %dx%d" % (k.version(), W, H))
        native = hasattr(hd, "coopApiVersion") and hd.coopApiVersion() >= 1
        check("this Krita has the co-op API", native)
        if not native:
            return
        # Krita has already imported its own copy of the engine as a plugin, so the imports above
        # (and everything below) use that copy, not the one in this repository.
        import krita_coop
        engine_dir = os.path.normcase(os.path.dirname(os.path.abspath(krita_coop.__file__)))
        note("co-op engine loaded from %s" % engine_dir)
        check("the co-op engine is the one built into this Krita",
              engine_dir != os.path.normcase(os.path.join(os.path.dirname(HERE), "krita_coop")))
        check("the Coop Drawing docker is there without installing anything",
              any(d.objectName() == "krita_coop_docker" for d in k.dockers()))

        ip = k.action("level_of_detail_mode")
        if ip is not None and ip.isChecked():
            ip.trigger()
            restore.append(ip)  # the setting is kept in kritarc: put it back afterwards
            note("Instant Preview switched off for the test")
        view.setBrushSize(60.0)
        try:
            note("brush preset: %s, size %.0f" % (view.currentBrushPreset().name(), view.brushSize()))
        except Exception:  # noqa: BLE001
            pass

        def lines_on(node, n, y0=100, dy=110, slope=40):
            for i in range(n):
                node.paintLine(QPoint(80, int(y0 + i * dy)), QPoint(W - 80, int(y0 + slope + i * dy)), 1.0, 1.0,
                               "ForegroundColor")

        def set_mode(mode):
            """Painting mode of the current brush: 2 = Wash (the stroke is merged into the layer when
            it ends), 1 = Build up (it is painted straight into the layer)."""
            import re

            from krita import Preset
            pr = Preset(view.currentBrushPreset())
            xml = pr.toXML()
            new, cnt = re.subn(r'(<param[^>]*name="PaintOpAction"[^>]*>\s*<!\[CDATA\[)\d+(\]\]>)',
                               lambda m: m.group(1) + str(mode) + m.group(2), xml)
            if not cnt:
                new = xml.replace("</Preset>", '<param type="string" name="PaintOpAction"><![CDATA[%d]]></param>'
                                               '</Preset>' % mode)
            pr.fromXML(new)

        # ------------------------------------------------------------------ A: the native calls
        hd.coopSetTracking(True)
        marker = bytes([10, 200, 30, 255]) * (64 * 64)
        n_lines = 120  # about a second of painting
        timing = {}

        def native_pass(label, mode, node, other):
            try:
                set_mode(mode)
            except Exception:  # noqa: BLE001
                note("%s: could not set the painting mode:\n%s" % (label, traceback.format_exc()))
            mark("%s: painting mode set" % label)
            hd.setActiveNode(node)
            hd.coopTakeDirty()
            lines_on(node, n_lines, 100, 800.0 / n_lines)
            mark("%s: %d strokes queued" % (label, n_lines))
            st = {"iter": 0, "overlay": 0, "preview": 0, "pauses": 0, "dirty": 0, "busy": 0}
            acts = set()
            pause_ms = []
            t0 = time.time()
            while hd.coopHasStrokes() and time.time() - t0 < 60:
                st["iter"] += 1
                first = st["iter"] <= 2
                acts.add(hd.coopActivity())
                ov = node.coopOverlayActive()
                if first:
                    mark("%s: round %d, stroke %r, overlay %s; reading" % (label, st["iter"], hd.coopActivity(), ov))
                shown = bytes(node.coopPixelData(0, 64, W, 960))
                raw = bytes(node.pixelData(0, 64, W, 960))
                if ov:
                    st["overlay"] += 1
                if not shown:
                    st["busy"] += 1
                elif shown != raw:
                    st["preview"] += 1
                tp = time.perf_counter()
                if first:
                    mark("%s: pausing" % label)
                hd.coopPause()
                if first:
                    mark("%s: paused; writing" % label)
                node.coopSetPixelData(marker, W - 80, H - 80, 64, 64)  # where the strokes never go
                for tx in (512, 960, 1408):  # under the strokes: read ...
                    for ty in (64, 128, 256, 448):
                        t = bytes(node.pixelData(tx, ty, 64, 64))
                        node.coopSetPixelData(t, tx, ty, 64, 64)  # ... and write back while they are held
                if first:
                    mark("%s: resuming" % label)
                hd.coopResume()
                if first:
                    mark("%s: resumed" % label)
                pause_ms.append(time.perf_counter() - tp)
                st["pauses"] += 1
                st["dirty"] += len(bytes(hd.coopTakeDirty())) // 32
                time.sleep(0.002)
            busy_time = time.time() - t0
            mark("%s: loop over after %d rounds, %.1f s; strokes still running: %s; waiting for them"
                 % (label, st["iter"], busy_time, hd.coopHasStrokes()))
            hd.waitForDone()
            mark("%s: strokes done" % label)
            st["dirty"] += len(bytes(hd.coopTakeDirty())) // 32
            timing[label] = busy_time
            note("%s: %d strokes took %.2f s; loop ran %d times; stroke ids seen: %s"
                 % (label, n_lines, busy_time, st["iter"], sorted(acts)))
            note("%s: overlay active in %d checks, stroke visible before it ended in %d, busy reads %d"
                 % (label, st["overlay"], st["preview"], st["busy"]))
            note("%s: pause + 13 tile writes: p50 %.2f / p95 %.2f / max %.2f ms"
                 % (label, pct(pause_ms, .5), pct(pause_ms, .95), pct(pause_ms, 1)))
            check("%s: strokes ran in the background while the script kept going" % label, st["iter"] > 3,
                  "iterations %d" % st["iter"])
            check("%s: the running stroke is reported as FREEHAND_STROKE" % label, "FREEHAND_STROKE" in acts,
                  str(sorted(acts)))
            check("%s: Krita reported the changed areas" % label, st["dirty"] > 0, "%d rects" % st["dirty"])
            if mode == 2:
                check("%s: the stroke is painted into an overlay" % label, st["overlay"] > 0)
                check("%s: ... and is visible through coopPixelData before it ends" % label, st["preview"] > 0)
            else:
                check("%s: the stroke is painted straight into the layer" % label, st["overlay"] == 0)
            full_raw = bytes(node.pixelData(0, 0, W, H))
            check("%s: after the strokes, coopPixelData == pixelData" % label,
                  bytes(node.coopPixelData(0, 0, W, H)) == full_raw)
            check("%s: the strokes left paint on the layer" % label, full_raw.count(b"\x00") < len(full_raw) - 4000)
            check("%s: pixels written during the strokes survived" % label,
                  bytes(node.pixelData(W - 80, H - 80, 64, 64)) == marker)
            # the same strokes on another layer, left alone: holding the strokes must not have lost a dab
            hd.setActiveNode(other)
            lines_on(other, n_lines, 100, 800.0 / n_lines)
            hd.waitForDone()
            a = bytes(node.pixelData(0, 0, W, H - 100))
            b = bytes(other.pixelData(0, 0, W, H - 100))
            diff = 0 if a == b else sum(1 for i in range(0, len(a), 4) if a[i:i + 4] != b[i:i + 4])
            check("%s: held strokes painted exactly what undisturbed strokes paint" % label, diff == 0,
                  "%d of %d pixels differ" % (diff, len(a) // 4) if diff else "")

        refs = []
        for label, mode in (("Wash", 2), ("Build-up", 1)):
            target = hd.createNode("Test " + label, "paintlayer")
            other = hd.createNode("Ref " + label, "paintlayer")
            root.addChildNode(target, None)
            root.addChildNode(other, None)
            hd.waitForDone()
            refs += [target, other]
            native_pass(label, mode, target, other)
        check("the canvas shows pixels written during a stroke (partial async update)",
              bytes(hd.pixelData(W - 60, H - 60, 1, 1)) == marker[:4], bytes(hd.pixelData(W - 60, H - 60, 1, 1)).hex())
        for n in refs:
            n.remove()
        hd.waitForDone()
        set_mode(2)
        hd.setActiveNode(paint)
        per_line = max(timing.get("Wash", 0.5) / n_lines, 0.01)

        # ------------------------------------------------------------------ B: a whole session
        host = HostSession(KritaDoc(hd), "Hana", port=0)
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
            t_0 = time.perf_counter()
            host.tick()
            t_1 = time.perf_counter()
            guest.tick()
            t_2 = time.perf_counter()
            ht.append(t_1 - t_0)
            gt.append(t_2 - t_1)
            QCoreApplication.processEvents()

        def wait_for(cond, timeout=60.0):
            t_ = time.time()
            while time.time() - t_ < timeout:
                step()
                if cond():
                    return True
                time.sleep(0.012)
            return False

        def quiet(sess):
            u = sess.undo
            return (not u.queue and u.pending is None and u.tracker.state == "sealed" and not sess.replica.sealing()
                    and not sess.replica.unacked_keys and not sess.pending_count() and not sess.replica.busy())

        sid = [None]

        def gnode():
            return made[0]._nodes.get(guest.uuid_of.get(sid[0])) if made else None

        def same():
            g = gnode()
            return g is not None and zlib.crc32(bytes(g.pixelData(0, 0, W, H))) == zlib.crc32(bytes(paint.pixelData(0, 0, W, H)))

        joined = wait_for(lambda: guest.state == "live" and quiet(host) and quiet(guest))
        sid[0] = host.sid_of.get(paint.uniqueId().toString())
        check("guest joined and received the picture", joined and same(), "state %s" % guest.state)
        check("both sides run in live mode", host._native() and guest._native())
        hd.setActiveNode(paint)

        # B1: the guest sees the host's stroke while it is being painted
        n1 = max(4, min(400, int(2.5 / per_line)))
        y_probe = 200
        ht.clear()
        gt.clear()
        paint.paintLine(QPoint(80, y_probe), QPoint(W - 80, y_probe), 1.0, 1.0, "ForegroundColor")  # first, so it is early
        lines_on(paint, n1, 400, 400.0 / n1, 0)  # rows 400..800
        seen_at = None
        t0 = time.time()
        while hd.coopHasStrokes() and time.time() - t0 < 60:
            step()
            g = gnode()
            if seen_at is None and g is not None and bytes(g.pixelData(W // 2, y_probe, 1, 1)) != CLEAR:
                seen_at = time.time() - t0
            time.sleep(0.012)
        total = time.time() - t0
        check("guest saw the host's stroke before the host finished drawing", seen_at is not None,
              "seen after %.2f s of %.2f s" % (seen_at if seen_at is not None else -1, total))
        note("host tick while drawing: p50 %.1f / p95 %.1f / max %.1f ms; guest tick p50 %.1f / p95 %.1f / max %.1f ms"
             % (pct(ht, .5), pct(ht, .95), pct(ht, 1), pct(gt, .5), pct(gt, .95), pct(gt, 1)))
        ok = wait_for(lambda: quiet(host) and quiet(guest) and same())
        check("identical layers after the host's strokes", ok)
        undo_before = host.undo_state()["undo"]

        # B2: the host receives the guest's paint while the host's own strokes are running
        g = gnode()
        gd = made[0].d
        y_g = 1450
        was_clear = bytes(paint.pixelData(W // 2, y_g, 1, 1))
        check("(the guest's spot is still empty)", was_clear == CLEAR)
        set_mode(1)  # Build up: the host's strokes go straight into the layer the guest paints on too
        lines_on(paint, 2 * n1, 900, 400.0 / (2 * n1), 0)  # rows 900..1300: the host is busy for a while
        g.paintLine(QPoint(300, y_g), QPoint(W - 300, y_g), 1.0, 1.0, "ForegroundColor")  # the guest draws
        got_at = None
        t0 = time.time()
        while hd.coopHasStrokes() and time.time() - t0 < 60:
            step()
            if got_at is None and bytes(paint.pixelData(W // 2, y_g, 1, 1)) != was_clear:
                got_at = time.time() - t0
            time.sleep(0.012)
        set_mode(2)
        total = time.time() - t0
        check("host received the guest's paint while its own strokes were still running", got_at is not None,
              "arrived after %.2f s of %.2f s" % (got_at if got_at is not None else -1, total))
        ok = wait_for(lambda: quiet(host) and quiet(guest) and same())
        check("identical layers after both drew at once", ok)
        gd.waitForDone()

        # B3: per-person undo of the host's last action
        n_undo = host.undo_state()["undo"]
        check("the host's strokes became undo steps", n_undo > undo_before, "%d -> %d" % (undo_before, n_undo))
        before = zlib.crc32(bytes(paint.pixelData(0, 850, W, 500)))
        host.request_undo()
        ok = wait_for(lambda: quiet(host) and quiet(guest) and host.undo_state()["undo"] == n_undo - 1, 30)
        after = zlib.crc32(bytes(paint.pixelData(0, 850, W, 500)))
        check("undo took back the host's last strokes", ok and after != before)
        check("... completely", bytes(paint.pixelData(0, 850, W, 500)).count(b"\x00") == W * 500 * 4)
        check("... and the guest's stroke is still there", bytes(paint.pixelData(W // 2, y_g, 1, 1)) != was_clear)
        ok = wait_for(lambda: quiet(host) and quiet(guest) and same())
        check("identical layers after the undo", ok)

        # ------------------------------------------------- C: a stroke drawn by hand (mouse events)
        # The strokes above were queued whole. A stroke drawn with the pen stays open while the
        # GUI thread keeps feeding it, which is the situation live mode is for.
        import math

        from PyQt5.QtCore import QEvent, QPointF, Qt
        from PyQt5.QtGui import QEnterEvent, QMouseEvent
        from PyQt5.QtWidgets import QApplication

        from krita_coop.cursors import _active_subwindow, _find_canvas

        hview = None
        for v in win.views():
            if v.document() == hd:
                hview = v
        check("C: found the host's view", hview is not None)
        win.showView(hview)
        for _ in range(30):
            step()
            time.sleep(0.01)
        tool = k.action("KritaShape/KisToolBrush")
        if tool is not None:
            tool.trigger()
        set_mode(2)
        hd.setActiveNode(paint)
        for _ in range(10):
            step()
            time.sleep(0.01)
        qwin, sub = _active_subwindow(win)
        canvas = _find_canvas(sub.widget() if sub is not None else qwin)
        check("C: found the canvas widget", canvas is not None)
        to_widget = hview.flakeToImageTransform().inverted()[0] * hview.flakeToCanvasTransform()
        pts = [(300 + 7 * i, 1100 + 120 * math.sin(i / 8.0)) for i in range(200)]  # rows 900..1300 are empty
        wp = [to_widget.map(QPointF(x, y)) for x, y in pts]
        inside = all(canvas.rect().contains(p.toPoint()) for p in wp)
        check("C: the stroke's area is visible in the view", inside,
              "canvas %dx%d, first point %d,%d" % (canvas.width(), canvas.height(), wp[0].x(), wp[0].y()))

        def mouse(kind, pt, button, buttons):
            ev = QMouseEvent(kind, pt, QPointF(canvas.mapToGlobal(pt.toPoint())), button, buttons, Qt.NoModifier)
            QApplication.sendEvent(canvas, ev)

        y_g2 = 1545
        check("C: (the guest's spot is still empty)", bytes(paint.pixelData(W // 2, y_g2, 1, 1)) == CLEAR)
        undo_n0 = host.undo_state()["undo"]
        canvas.setFocus()
        QApplication.sendEvent(canvas, QEnterEvent(wp[0], wp[0], QPointF(canvas.mapToGlobal(wp[0].toPoint()))))
        step()
        ht.clear()
        gt.clear()
        seen_at = got_at = None
        open_n = 0
        g = gnode()
        t0 = time.time()
        mouse(QEvent.MouseButtonPress, wp[0], Qt.LeftButton, Qt.LeftButton)
        for i in range(1, len(wp)):
            mouse(QEvent.MouseMove, wp[i], Qt.NoButton, Qt.LeftButton)
            step()
            if hd.coopHasStrokes():
                open_n += 1
            if i == 30:
                g.paintLine(QPoint(300, y_g2), QPoint(W - 300, y_g2), 1.0, 1.0, "ForegroundColor")  # the guest draws
            if seen_at is None and bytes(g.pixelData(int(pts[5][0]), int(pts[5][1]), 1, 1)) != CLEAR:
                seen_at = (i, time.time() - t0)
            if got_at is None and bytes(paint.pixelData(W // 2, y_g2, 1, 1)) != CLEAR:
                got_at = (i, time.time() - t0)
            time.sleep(0.008)
        still_open = hd.coopHasStrokes()
        act = hd.coopActivity()
        overlay = paint.coopOverlayActive()
        total = time.time() - t0
        mouse(QEvent.MouseButtonRelease, wp[-1], Qt.LeftButton, Qt.NoButton)
        note("C: hand-drawn stroke of %d points took %.2f s; a stroke was running in %d of %d checks; at the end: "
             "open=%s, id=%r, overlay=%s" % (len(wp), total, open_n, len(wp) - 1, still_open, act, overlay))
        note("C: host tick during it: p50 %.1f / p95 %.1f / max %.1f ms; guest tick p50 %.1f / p95 %.1f / max %.1f ms"
             % (pct(ht, .5), pct(ht, .95), pct(ht, 1), pct(gt, .5), pct(gt, .95), pct(gt, 1)))
        check("C: the mouse events drew a real brush stroke that stayed open", still_open and open_n > len(wp) // 2)
        check("C: guest saw the stroke while the 'pen' was still down", seen_at is not None,
              "at point %d, %.2f s of %.2f s" % (seen_at[0], seen_at[1], total) if seen_at else "")
        check("C: host received the guest's paint while its 'pen' was still down", got_at is not None,
              "at point %d, %.2f s of %.2f s" % (got_at[0], got_at[1], total) if got_at else "")
        ok = wait_for(lambda: quiet(host) and quiet(guest) and same())
        check("C: identical layers after the hand-drawn stroke", ok)
        check("C: the stroke left paint on the host's layer",
              bytes(paint.pixelData(int(pts[100][0]), int(pts[100][1]), 1, 1)) != CLEAR)
        check("C: the hand-drawn stroke is one undo step", host.undo_state()["undo"] == undo_n0 + 1,
              "%d -> %d" % (undo_n0, host.undo_state()["undo"]))
        host.request_undo()
        ok = wait_for(lambda: quiet(host) and quiet(guest) and host.undo_state()["undo"] == undo_n0, 30)
        check("C: undo removed it completely", ok and bytes(paint.pixelData(0, 900, W, 400)).count(b"\x00") == W * 400 * 4)
        check("C: ... and kept the guest's stroke", bytes(paint.pixelData(W // 2, y_g2, 1, 1)) != CLEAR)
        ok = wait_for(lambda: quiet(host) and quiet(guest) and same())
        check("C: identical layers after that undo", ok)

        errs = [t for s in (host, guest) for kk, t in s.events if kk == "error"]
        check("no internal errors logged", not errs, (errs[0][-300:] if errs else ""))
        guest.stop("done")
        host.stop("done")
        for kd in made:
            kd.d.setModified(False)
        hd.setModified(False)
    except Exception:  # noqa: BLE001
        lines.append("FAIL  EXCEPTION\n" + traceback.format_exc())
    finally:
        for a in restore:
            try:
                if not a.isChecked():
                    a.trigger()
            except Exception:  # noqa: BLE001
                pass
        lines.append("DONE")
        flush()
