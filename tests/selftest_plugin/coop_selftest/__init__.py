"""A tiny Krita plugin that runs tests/krita_live_gui.py inside the real application.

It is not part of the co-op build. The test launchers (tests/run_live_gui.ps1 on Windows,
"krita-build/mac/build.sh gui" on macOS) copy it next to Krita's own Python plugins for one
run, enable it with KRITA_FORCE_PYTHON_PLUGINS=coop_selftest, and point COOP_SELFTEST_DIR at
the tests folder. Without COOP_SELFTEST_DIR it does nothing.
"""

import os
import sys
import time
import traceback

from krita import Extension, Krita
from PyQt5.QtCore import QTimer

DIR = os.environ.get("COOP_SELFTEST_DIR")


def trace(text):
    if DIR:
        with open(os.path.join(DIR, "krita_live_gui.trace"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), text))


class SelfTest(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self.started = False
        self.tries = 0

    def setup(self):
        trace("setup")

    def createActions(self, window):
        trace("createActions")
        if not self.started and DIR:
            self.started = True
            QTimer.singleShot(4000, self.run)

    def run(self):
        # (an exception escaping a Qt slot would open Krita's modal error dialog)
        try:
            if Krita.instance().activeWindow() is None and self.tries < 120:
                self.tries += 1
                if self.tries % 10 == 1:
                    trace("no active window yet")
                QTimer.singleShot(1000, self.run)
                return
            trace("running")
            sys.path.insert(0, DIR)
            import krita_live_gui
            krita_live_gui.main()
            trace("finished")
        except BaseException:  # noqa: BLE001
            trace("EXCEPTION\n" + traceback.format_exc())


Krita.instance().addExtension(SelfTest(Krita.instance()))
