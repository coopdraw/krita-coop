"""Smoke test for the docker/controller code inside Krita (kritarunner).

Builds the docker, hosts through the controller, joins with a guest session,
ticks everything and exercises the UI update paths.  Output: krita_ui_smoke.log
"""

import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["KRITA_COOP_NO_REGISTER"] = "1"
OUT = os.path.join(HERE, "krita_ui_smoke.log")


def main(*args):
    lines = []
    try:
        from krita import Krita
        from PyQt5.QtCore import QCoreApplication

        from krita_coop.controller import controller
        from krita_coop.docker import CoopDocker
        from krita_coop.kritadoc import KritaDoc
        from krita_coop.session import GuestSession

        k = Krita.instance()
        doc = k.createDocument(800, 600, "smoke", "RGBA", "U8", "", 300.0)
        k.setActiveDocument(doc)
        lines.append("active doc set: %s" % (k.activeDocument() is not None))
        ctl = controller()
        dock = CoopDocker()
        lines.append("docker built")
        dock.name_edit.setText("Hana")
        ctl.host(dock._name(), 0, doc)  # headless Krita has no views, so no "active document"
        lines.append("hosting: %s" % (ctl.session is not None))
        s = ctl.session
        made = []

        def factory(info):
            kd = KritaDoc.create(info)
            made.append(kd)
            return kd

        g = GuestSession(factory, "Gus", "127.0.0.1", s.port, s.code)
        g.start()
        end = time.time() + 15
        while time.time() < end and not (g.state == "live" and len(s.peers) == 2):
            ctl._tick()
            g.tick()
            QCoreApplication.processEvents()
            time.sleep(0.01)
        lines.append("guest live: %s peers=%s" % (g.state, s.peers))
        dock._update_status()
        lines.append("status: " + dock.status_lbl.text().replace("<br>", " | "))
        lines.append("layer: " + dock.layer_lbl.text())
        dock.chat_edit.setText("hello from the docker")
        dock._chat()
        for _ in range(30):
            ctl._tick()
            g.tick()
            QCoreApplication.processEvents()
            time.sleep(0.01)
        lines.append("guest got chat: %s" % any("hello from the docker" in t for _, t in g.events))
        lines.append("undo router: %s, windows adopted: %d" % (
            type(ctl.router).__name__, len(ctl.router.wins) if ctl.router else -1))
        ctl.router.sync()
        dock.undo_btn.click()  # nothing of mine to undo yet
        for _ in range(10):
            ctl._tick()
            g.tick()
            QCoreApplication.processEvents()
            time.sleep(0.01)
        dock._update_status()
        lines.append("undo label: " + dock.undo_lbl.text())
        lines.append("undo message: %s" % any("Nothing of yours to undo" in h[2] for h in ctl.history))
        lines.append("invite: " + dock._invite_text())
        errs = [t for t in [h[2] for h in ctl.history] if "Internal error" in t or "Undo routing problem" in t]
        lines.append("controller errors: %s" % errs[:2])
        g.stop("done")
        ctl.leave()
        lines.append("left: session=%s" % ctl.session)
        ctl.router.stop()
        lines.append("log tail: %s" % [h[2] for h in ctl.history][-4:])
        dock.timer.stop()
        for kd in made:
            kd.release()
            kd.d.close()
        doc.close()
        lines.append("RESULT: OK" if not errs and g.state == "live" else "RESULT: FAILED")
    except Exception:  # noqa: BLE001
        lines.append("EXCEPTION\n" + traceback.format_exc())
        lines.append("RESULT: FAILED")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
