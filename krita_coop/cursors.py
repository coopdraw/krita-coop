"""Shared cursors: show where the other people are pointing on the canvas.

Best effort: Krita has no API for canvas overlays, so this finds the canvas
widget of the active view and lays a transparent, click-through widget on top.
Any failure just turns the feature off for that view.
"""

import time

try:
    from PyQt5.QtCore import QPointF, QRectF, Qt
    from PyQt5.QtGui import QColor, QCursor, QFont, QPainter, QPen
    from PyQt5.QtWidgets import QMdiArea, QWidget
except ImportError:  # Krita 6 / PyQt6
    from PyQt6.QtCore import QPointF, QRectF, Qt
    from PyQt6.QtGui import QColor, QCursor, QFont, QPainter, QPen
    from PyQt6.QtWidgets import QMdiArea, QWidget

from krita import Krita

CANVAS_CLASSES = ("KisOpenGLCanvas2", "KisQPainterCanvas")
COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6", "#bcf60c"]
SEND_INTERVAL = 0.05
STALE_AFTER = 15.0


class _Overlay(QWidget):
    def __init__(self, parent):
        QWidget.__init__(self, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.points = []

    def paintEvent(self, event):
        if not self.points:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        for x, y, name, color in self.points:
            c = QColor(color)
            p.setPen(QPen(QColor(0, 0, 0, 160), 3))
            p.drawEllipse(QPointF(x, y), 6, 6)
            p.setPen(QPen(c, 2))
            p.drawEllipse(QPointF(x, y), 6, 6)
            tw = p.fontMetrics().horizontalAdvance(name) + 10
            box = QRectF(x + 9, y + 6, tw, p.fontMetrics().height() + 4)
            p.fillRect(box, c)
            p.setPen(QColor("white"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, name)
        p.end()


def _active_subwindow(window):
    qwin = window.qwindow()
    if qwin is None:
        return None, None
    mdi = qwin.findChild(QMdiArea)
    sub = mdi.activeSubWindow() if mdi is not None else None
    return qwin, sub


def _find_canvas(root):
    for w in root.findChildren(QWidget):
        if w.metaObject().className() in CANVAS_CLASSES and w.isVisible():
            return w
    return None


class CursorSharing(object):
    def __init__(self):
        self.overlay = None
        self.canvas = None
        self._last_sent = None
        self._last_time = 0.0
        self._last_update = 0.0
        self._disabled = False
        self._sub = None
        self._sub_canvas = None

    def _canvas_for(self, win):
        qwin, sub = _active_subwindow(win)
        key = sub if sub is not None else qwin
        if key is None:
            return None
        if key is not self._sub or self._sub_canvas is None or not self._sub_canvas.isVisible():
            self._sub = key
            self._sub_canvas = _find_canvas(sub.widget() if sub is not None else qwin)
        return self._sub_canvas

    def stop(self):
        if self.overlay is not None:
            try:
                self.overlay.deleteLater()
            except RuntimeError:
                pass
        self.overlay = None
        self.canvas = None
        self._last_sent = None

    def _transforms(self, view):
        img_to_widget = view.flakeToImageTransform().inverted()[0] * view.flakeToCanvasTransform()
        return img_to_widget, img_to_widget.inverted()[0]

    def update(self, session, share):
        if self._disabled:
            return
        try:
            self._update(session, share)
        except Exception:  # noqa: BLE001
            self._disabled = True
            self.stop()
            session.log("Shared cursors were turned off (this Krita build is not supported).", "warn")

    def _update(self, session, share):
        now = time.perf_counter()
        if now - self._last_update < 0.033:
            return
        self._last_update = now
        k = Krita.instance()
        win = k.activeWindow()
        view = win.activeView() if win is not None else None
        same = view is not None and session.doc.same_document(view.document())
        canvas = self._canvas_for(win) if same else None
        if canvas is not self.canvas:
            self.stop()
            if canvas is not None:
                self.canvas = canvas
                self.overlay = _Overlay(canvas)
                self.overlay.show()
        if canvas is None:
            if self._last_sent is not None:
                session.send_cursor(None, None)
                self._last_sent = None
            return
        to_widget, to_image = self._transforms(view)
        # outgoing
        if share and now - self._last_time >= SEND_INTERVAL:
            wp = canvas.mapFromGlobal(QCursor.pos())
            if canvas.rect().contains(wp):
                ip = to_image.map(QPointF(wp))
                pos = (round(ip.x(), 1), round(ip.y(), 1))
                if pos != self._last_sent:
                    session.send_cursor(pos[0], pos[1])
                    self._last_sent = pos
                    self._last_time = now
            elif self._last_sent is not None:
                session.send_cursor(None, None)
                self._last_sent = None
                self._last_time = now
        # incoming
        pts = []
        for pid, (x, y, t) in list(session.cursors.items()):
            if now - t > STALE_AFTER:
                continue
            wpt = to_widget.map(QPointF(x, y))
            pts.append((wpt.x(), wpt.y(), session.peer_name(pid), COLORS[pid % len(COLORS)]))
        ov = self.overlay
        if ov.geometry() != canvas.rect():
            ov.setGeometry(canvas.rect())
        if pts or ov.points:
            ov.points = pts
            ov.raise_()
            ov.update()
