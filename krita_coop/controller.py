"""Process-wide session controller.

Krita creates one docker per main window, so the session (and its timer)
lives here and every docker is just a view of it.
"""

import time
import traceback

from krita import Krita

try:
    from PyQt5.QtCore import QObject, QTimer, pyqtSignal
except ImportError:  # Krita 6 / PyQt6
    from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import net
from .cursors import CursorSharing
from .kritadoc import KritaDoc
from .session import GuestSession, HostSession

SETTINGS_GROUP = "krita_coop"


class Controller(QObject):
    changed = pyqtSignal()  # session started/ended
    message = pyqtSignal(str, str)  # kind, text

    def __init__(self):
        QObject.__init__(self)
        self.session = None
        self.history = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.cursors = CursorSharing()
        self.share_cursor = self.setting("share_cursor", "true") == "true"
        self._errors = 0
        self._ip_restore = False  # Instant Preview was on before the session
        self.router = None  # undokeys.UndoRouter, created with the first session
        try:
            Krita.instance().notifier().applicationClosing.connect(self.shutdown)
        except Exception:  # noqa: BLE001
            pass

    # ---- settings --------------------------------------------------------------
    def setting(self, key, default=""):
        return Krita.instance().readSetting(SETTINGS_GROUP, key, default)

    def set_setting(self, key, value):
        Krita.instance().writeSetting(SETTINGS_GROUP, key, str(value))

    # ---- session lifecycle -------------------------------------------------------
    def host(self, name, port, document=None):
        if self.session is not None:
            raise RuntimeError("You are already in a session.")
        doc = document or Krita.instance().activeDocument()
        if doc is None:
            raise RuntimeError("Open (or create) the document you want to share first.")
        if doc.animationLength() > 1 and doc.fullClipRangeEndTime() > 0:
            self._emit("warn", "Animated documents are shared one frame at a time (the current frame only).")
        s = HostSession(KritaDoc(doc), name, port=port)
        s.start()  # raises OSError if the port is taken
        self._begin(s)
        return s

    def join(self, name, address, code):
        if self.session is not None:
            raise RuntimeError("You are already in a session.")
        host, port = net.parse_address(address)
        if not net.normalize_code(code):
            raise ValueError("Enter the session code the host gave you.")
        s = GuestSession(KritaDoc.create, name, host, port, code)
        s.start()
        self._begin(s)
        return s

    def leave(self):
        if self.session is not None:
            self.session.stop("You left the session.")
            self._flush_events()
            self._end()

    def shutdown(self):
        if self.router is not None:
            self.router.stop()
        if self.session is not None:
            try:
                self.session.stop("Krita is closing.")
            except Exception:  # noqa: BLE001
                pass
            self._end()

    def _instant_preview(self, on):
        """Instant Preview paints a stroke on a small copy of the picture first and on the real
        layers only afterwards, so others would not see a stroke before it ends (and what they
        paint would not show up here during one). It is switched off for the session."""
        try:
            a = Krita.instance().action("level_of_detail_mode")
            if a is None or not a.isCheckable():
                return
            if not on and a.isChecked():
                a.trigger()
                self._ip_restore = True
                self._emit("info", "Instant Preview is switched off during the session, so strokes can be "
                                   "shared while they are painted. It comes back on when the session ends.")
            elif on and self._ip_restore:
                self._ip_restore = False
                if not a.isChecked():
                    a.trigger()
        except Exception:  # noqa: BLE001 - cosmetic
            pass

    def _begin(self, s):
        self.session = s
        self._errors = 0
        self._instant_preview(False)
        self.timer.start(s.cfg.tick_ms)
        try:
            if self.router is None:
                from .undokeys import UndoRouter
                self.router = UndoRouter(self)
                self.router.install()
            self.router.session_started(s)
        except Exception:  # noqa: BLE001
            self.report("warn", "Couldn't take over Undo/Redo (use the docker's Undo mine/Redo mine buttons):\n"
                        + traceback.format_exc())
        self._flush_events()
        self.changed.emit()

    def _end(self):
        s = self.session
        self.cursors.stop()
        self.session = None
        self.timer.stop()
        self._instant_preview(True)
        if self.router is not None and s is not None:
            self.router.session_ended(s)
        self.changed.emit()

    # ---- undo ----------------------------------------------------------------------
    def request(self, kind):
        """Undo/redo your own last change in the shared document (Ctrl+Z, menu, docker buttons)."""
        s = self.session
        if s is None or s.closed:
            return False
        if self.router is not None:
            self.router.watch_presses(True)  # a press on the canvas before it runs cancels it
        ok = s.request_undo() if kind == "undo" else s.request_redo()
        self._flush_events()
        return ok

    def report(self, kind, text, floating=False):
        """A message from the controller itself (not from the session)."""
        self._emit(kind, text)
        if floating:
            self._float(text)

    def _float(self, text):
        try:
            from .undokeys import floating
            floating(text)
        except Exception:  # noqa: BLE001
            pass

    # ---- loop ----------------------------------------------------------------------
    def _tick(self):
        s = self.session
        if s is None:
            self.timer.stop()
            return
        try:
            s.tick()
        except Exception:  # noqa: BLE001
            self._errors += 1
            s.log("Internal error:\n" + traceback.format_exc(), "error")
            if self._errors > 20:
                s.stop("Stopped after repeated internal errors (see the log above).")
        if not s.closed and s.doc is not None:
            try:
                self.cursors.update(s, self.share_cursor)
            except Exception:  # noqa: BLE001
                pass
        self._flush_events()
        if s.closed:
            self._end()

    def _emit(self, kind, text):
        self.history.append((time.strftime("%H:%M"), kind, text))
        del self.history[:-500]
        self.message.emit(kind, text)

    def _flush_events(self):
        s = self.session
        if s is None:
            return
        events, s.events = s.events, []
        for kind, text in events:
            self._emit(kind, text)
            if kind == "undo-warn":
                self._float(text)  # Ctrl+Z didn't do the obvious thing: say why, where the user looks


_INSTANCE = None


def controller():
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = Controller()
    return _INSTANCE
