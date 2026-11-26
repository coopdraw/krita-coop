"""Routes Krita's Undo/Redo to the plugin for the shared document.

Krita creates one ``edit_undo`` / ``edit_redo`` QAction per main window, connected once
(``KisMainWindow::createActions``) to the window's private ``undo()`` / ``redo()`` slots.
Every way of undoing goes through ``triggered`` of those actions: the user's shortcut
(Ctrl+Z, Cmd+Z in the mac menu bar), the Edit menu, toolbar buttons, the detached canvas,
touch gestures and scripts.  So the router disconnects ``triggered`` and decides per press:

* the active view shows the shared document  -> the session's per-person undo;
* a document that *was* shared (guarded)     -> Krita's undo only for steps made after the
  session (its older history mixes everyone's changes); otherwise ask;
* anything else                               -> Krita's own slot (``invokeMethod``), unchanged.

Nothing is written to Krita's shortcut settings, so a crash leaves no trace.  The Undo History
docker bypasses the actions (it sets the stack index directly), so its list is disabled for
shared/guarded documents.  That docker's model also shows Krita's undo stack of the active
document, which tells us

* when Krita pushes a command on the shared document (a selection, a stroke...), so the engine
  can tell "Ctrl+Z after a selection" from "Ctrl+Z after a stroke";
* when Krita undoes a step by itself (the Crop tool does that), so the shared picture can be
  restored instead of being sent to everyone;
* how far Krita's own undo may go in a guarded document.

While an undo request waits, presses on a canvas are counted so it never takes back a stroke
started after the key press.  Everything here is exception-safe: a failure only switches the
affected feature off (with a message), and never falls back to Krita's undo for the shared
document.
"""

import time
import traceback

from krita import Krita

try:
    from PyQt5 import sip
    from PyQt5.QtCore import QEvent, QMetaObject, QObject, QTimer
    from PyQt5.QtGui import QIcon
    from PyQt5.QtWidgets import QAbstractItemView, QApplication, QDockWidget, QMessageBox
except ImportError:  # Krita 6 / PyQt6
    from PyQt6 import sip
    from PyQt6.QtCore import QEvent, QMetaObject, QObject, QTimer
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QAbstractItemView, QApplication, QDockWidget, QMessageBox

CANVAS_CLASSES = ("KisOpenGLCanvas2", "KisQPainterCanvas")
PRESS_TYPES = (QEvent.Type.MouseButtonPress, QEvent.Type.TabletPress, QEvent.Type.TouchBegin)
GUARD_TEXT = ("This document's own Krita undo history still contains the co-op session, so undoing "
              "into it would remove other people's work (or bring back what you undid).\n\n"
              "Save it and reopen it to get a clean undo history.")


def _alive(obj):
    return obj is not None and not sip.isdeleted(obj)


class _Win(object):
    def __init__(self, w, q):
        self.w = w  # libkis Window (kept alive: a collected wrapper loses its signal connections)
        self.q = q  # its KisMainWindow
        self.u = self.r = None
        self.redirected = False
        self.view = None  # the History docker's list (KisUndoView)
        self.snap = None  # (document, rows, current row) of that list
        self.check_queued = False
        self.disabled = False  # fallback mode: native actions disabled while not "native"

    def position(self):
        """(rows, current row) of Krita's undo list for this window's active document."""
        if not _alive(self.view) or self.view.model() is None:
            return None
        m, sm = self.view.model(), self.view.selectionModel()
        rows = m.rowCount()
        return rows, (sm.currentIndex().row() if sm is not None else rows - 1)


class _Guard(object):
    """A document that was shared: Krita's undo is allowed only above ``base`` (its position
    in Krita's undo list when the session ended; None = unknown, so never).  Redo entries that
    existed then belong to the session too: redo is allowed only after a new step was made."""

    def __init__(self, doc, base):
        self.doc = doc
        self.base = base
        self.new_steps = False


class UndoRouter(QObject):
    def __init__(self, ctl):
        QObject.__init__(self)
        self.ctl = ctl
        self.wins = []
        self.guards = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self._safe("sync", self.sync))
        self._installed = False
        self._failures = {}
        self._saved_t = -1e9
        self._ignore_until = 0.0  # our own calls of Krita's undo/redo are not new commands
        self._prompted = []
        self._watching = False
        self._limit = 200
        self._read_limit()

    # ---- safety ---------------------------------------------------------------------
    def _safe(self, what, fn, *args):
        """Run a slot; never let an exception reach Krita's excepthook (it opens a dialog)."""
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001
            n = self._failures.get(what, 0) + 1
            self._failures[what] = n
            if n == 1:
                self.ctl.report("warn", "Undo routing problem in %s (ignored):\n%s" % (what, traceback.format_exc()))
            if what == "sync" and n >= 20:
                self.timer.stop()
            return None

    def _read_limit(self):
        try:
            self._limit = int(Krita.instance().readSetting("", "undoStackLimit", "200") or 200)
        except Exception:  # noqa: BLE001
            self._limit = 200

    # ---- lifecycle ------------------------------------------------------------------
    def install(self):
        if self._installed:
            return
        self._installed = True
        n = Krita.instance().notifier()
        n.windowCreated.connect(lambda: self._safe("windowCreated", self._adopt_all))
        for sig, fn in (("imageSaved", lambda *_a: setattr(self, "_saved_t", time.perf_counter())),
                        ("configurationChanged", lambda *_a: self._safe("config", self._read_limit))):
            try:
                getattr(n, sig).connect(fn)
            except Exception:  # noqa: BLE001
                pass
        self._adopt_all()
        self.timer.start(300)

    def stop(self):
        self.timer.stop()
        self.watch_presses(False)
        self.guards = []  # drop document references before Krita shuts down

    def session_started(self, session):
        self.sync()
        if not any(e.redirected for e in self.wins) and Krita.instance().windows():
            self.ctl.report("warn", "Couldn't take over Undo/Redo in Krita's window. Use the "
                                    "“Undo mine”/“Redo mine” buttons in this docker.")

    def session_ended(self, session):
        """Keep Krita's own undo off for a document we wrote pixels into outside its history."""
        try:
            kd = session.doc
            if session.wrote_pixels and kd is not None and kd.alive():
                base = None
                for e in self.wins:
                    v = e.w.activeView()
                    if v is not None and kd.same_document(v.document()):
                        pos = e.position()
                        if pos is not None:
                            base = pos[1]
                            break
                self.guards.append(_Guard(kd.d, base))
                self.ctl.report("warn", "Krita's own undo history for this document mixes everyone's changes: "
                                        "undo works for what you do from now on, not for older steps. "
                                        "Save and reopen the document to get a clean history.")
        except Exception:  # noqa: BLE001
            pass
        self.watch_presses(False)
        self.sync()

    # ---- windows --------------------------------------------------------------------
    def _adopt_all(self):
        for w in Krita.instance().windows():
            self._safe("adopt", self._adopt, w)
        self.sync()

    def _adopt(self, w):
        q = w.qwindow()
        if not _alive(q) or any(e.q is q for e in self.wins):
            return
        e = _Win(w, q)
        acts = {a.objectName(): a for a in q.actions()}  # the collection itself has no parent
        e.u, e.r = acts.get("edit_undo"), acts.get("edit_redo")
        mo = q.metaObject()
        if e.u is not None and e.r is not None and mo.indexOfMethod("undo()") >= 0 \
                and mo.indexOfMethod("redo()") >= 0:
            for a in (e.u, e.r):
                try:
                    a.triggered.disconnect()  # removes the C++ link to KisMainWindow::undo()/redo()
                except TypeError:
                    pass
            e.u.triggered.connect(lambda *_a, e=e: self._safe("undo", self._route, e, "undo"))
            e.r.triggered.connect(lambda *_a, e=e: self._safe("redo", self._route, e, "redo"))
            e.redirected = True
        dock = q.findChild(QDockWidget, "History")
        if dock is not None:
            e.view = dock.findChild(QAbstractItemView)
        if e.view is not None and e.view.model() is not None:
            e.view.model().modelReset.connect(lambda e=e: self._queue_check(e))
        try:
            w.activeViewChanged.connect(lambda e=e: self._safe("view", self._view_changed, e))
        except Exception:  # noqa: BLE001
            pass
        self.wins.append(e)

    def _guard_of(self, d):
        for g in self.guards:
            if g.doc == d:
                return g
        return None

    def _mode(self, e):
        """("session" | "guarded" | "native", libkis Document or None) for the window's active view."""
        v = e.w.activeView()
        d = v.document() if v is not None else None
        if d is None:
            return "native", None
        s = self.ctl.session
        if s is not None and not s.closed and s.doc is not None and s.doc.same_document(d):
            return "session", d
        if self.guards and self._guard_of(d) is not None:
            return "guarded", d
        return "native", d

    # ---- undo / redo presses -------------------------------------------------------------
    def _route(self, e, which):
        try:
            mode, d = self._mode(e)
        except Exception:  # noqa: BLE001 - never guess which document was meant
            self.ctl.report("warn", "Undo ignored: couldn't tell which document it was for.")
            return
        if mode == "session":
            self.watch_presses(True)  # a press before it runs cancels it
            self.ctl.request(which)
        elif mode == "guarded":
            self._guarded_press(e, d, which)
        else:
            QMetaObject.invokeMethod(e.q, which)

    def _native(self, e, which):
        self._ignore_until = time.perf_counter() + 0.5
        QMetaObject.invokeMethod(e.q, which)

    def _guarded_press(self, e, d, which):
        g = self._guard_of(d)
        pos = e.position()
        if pos is not None:
            rows, cur = pos
            if which == "undo" and g.base is not None and cur > g.base:
                self._native(e, which)  # a step made after the session
                return
            if which == "redo" and g.new_steps and g.base is not None and g.base <= cur < rows - 1:
                self._native(e, which)  # a step made after the session, undone again
                return
        if any(p == d for p in self._prompted):
            self.ctl.report("warn", "Undo is off for older steps of this document (it was shared). "
                                    "Save and reopen it to undo them.", floating=True)
            return
        self._prompted.append(d)
        box = QMessageBox(e.q)
        box.setWindowTitle("Coop Drawing")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(GUARD_TEXT)
        keep = box.addButton("Keep undo off", QMessageBox.ButtonRole.RejectRole)
        anyway = box.addButton("Use Krita's undo anyway", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() is anyway:
            self.guards = [x for x in self.guards if x is not g]
            self.sync()
            self._native(e, which)

    # ---- Krita's own undo stack ----------------------------------------------------------
    def _view_changed(self, e):
        e.snap = None
        self.sync()

    def _queue_check(self, e):
        if not e.check_queued:  # one model reset per push is common, several happen too
            e.check_queued = True
            QTimer.singleShot(0, lambda e=e: self._safe("history", self._check_push, e))

    def _check_push(self, e):
        e.check_queued = False
        if not _alive(e.q):
            return
        mode, d = self._mode(e)
        pos = e.position()
        if d is None or pos is None:
            e.snap = None
            return
        rows, cur = pos
        snap, e.snap = e.snap, (d, rows, cur)
        now = time.perf_counter()
        if snap is None or not (snap[0] == d) or now < self._ignore_until:
            return
        if mode == "session" and cur < snap[2]:
            self._krita_undid(d)
            return
        if mode == "guarded" and rows == 1:
            g = self._guard_of(d)
            if g is not None:
                g.base = 0  # Krita's list was cleared (e.g. undo size changed): no session steps left
        pushed = cur > snap[2] or (rows > snap[1] and cur == rows - 1)
        at_limit = (rows, cur) == snap[1:] and cur == rows - 1 and rows - 1 >= self._limit
        if not pushed and at_limit and now - self._saved_t > 1.0:
            pushed = True  # at the undo limit the counts stay put; the reset itself is the push
        if not pushed:
            return
        # The same document may show in two windows: report each new step once.
        for o in self.wins:
            if o is not e and o.snap is not None and o.snap[0] == d:
                o.snap = (d, rows, cur)
        if mode == "session":
            text = ""
            try:
                m = e.view.model()
                text = str(m.data(m.index(cur, 0)) or "")
            except Exception:  # noqa: BLE001
                pass
            self.ctl.session.note_native_action(text or "a Krita command")
        elif mode == "guarded":
            g = self._guard_of(d)
            if g is not None:
                # A new step drops any older redo entries.  (At Krita's undo limit the oldest
                # steps fall off and ``base`` would really move down; keeping it is the safe side.)
                g.new_steps = True

    def _krita_undid(self, d):
        """Krita ran its own undo on the shared document without the Undo action (e.g. clicking
        again with the Crop tool after a crop).  Its per-layer history may have taken back
        someone else's paint: don't send that to everyone, restore the shared picture."""
        s = self.ctl.session
        if s is None or s.closed or not s.live:
            return
        if s.doc.size() == s.size:
            s.restore_from_host("its own undo")
        else:
            self.ctl.report("warn", "Krita undid a step of the shared document by itself (probably the Crop "
                                    "tool). Check the picture; avoid adjusting a crop during a session.")

    # ---- presses while an undo waits ---------------------------------------------------------
    def watch_presses(self, on):
        app = QApplication.instance()
        if app is None or on == self._watching:
            return
        if on:
            app.installEventFilter(self)  # application filters run before Krita's canvas filters
        else:
            app.removeEventFilter(self)
        self._watching = on

    def eventFilter(self, obj, ev):
        try:
            if ev.type() in PRESS_TYPES and obj.metaObject().className() in CANVAS_CLASSES:
                s = self.ctl.session
                if s is not None and not s.closed:
                    s.note_input()
        except Exception:  # noqa: BLE001
            pass
        return False

    # ---- periodic ------------------------------------------------------------------------
    def sync(self):
        s = self.ctl.session
        self.wins = [e for e in self.wins if _alive(e.q)]
        if self.guards:
            docs = Krita.instance().documents()
            self.guards = [g for g in self.guards if any(g.doc == d for d in docs)]
            self._prompted = [p for p in self._prompted if any(p == d for d in docs)]
        for e in self.wins:
            mode, _d = self._mode(e)
            if _alive(e.view):
                want = mode == "native"
                if e.view.isEnabled() != want:
                    e.view.setEnabled(want)  # clicking a row would run Krita's undo directly
                if e.snap is None and mode != "native":
                    self._check_push(e)  # take a first snapshot
            if not e.redirected:
                self._fallback(e, mode)
        if self._watching and (s is None or s.closed or not (s.undo.queue or s.undo.pending)):
            self.watch_presses(False)
        busy = s is not None or self.guards or not all(e.redirected for e in self.wins)
        self.timer.setInterval(300 if busy else 1000)

    def _fallback(self, e, mode):
        """Without the redirect, at least keep the shortcut/menu/toolbar off the shared document."""
        for a in (e.u, e.r):
            if a is None:
                continue
            if mode != "native":
                if a.isEnabled():
                    a.setEnabled(False)
                e.disabled = True
            elif e.disabled and not a.isEnabled():
                a.setEnabled(True)
        if mode == "native":
            e.disabled = False


def floating(text):
    """Show ``text`` on the active canvas for a moment (best effort)."""
    try:
        win = Krita.instance().activeWindow()
        view = win.activeView() if win is not None else None
        if view is not None:
            view.showFloatingMessage(text, QIcon(), 2500, 1)
    except Exception:  # noqa: BLE001
        pass
