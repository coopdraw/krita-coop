"""The "Coop Drawing" docker (Settings > Dockers > Coop Drawing)."""

import html
import time

from krita import DockWidget

try:
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtGui import QTextCursor
    from PyQt5.QtWidgets import (QApplication, QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                                 QLineEdit, QMessageBox, QPushButton, QSpinBox, QTabWidget, QTextBrowser,
                                 QVBoxLayout, QWidget)
except ImportError:  # Krita 6 / PyQt6
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QTextCursor
    from PyQt6.QtWidgets import (QApplication, QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                                 QLineEdit, QMessageBox, QPushButton, QSpinBox, QTabWidget, QTextBrowser,
                                 QVBoxLayout, QWidget)

from . import net
from .controller import controller

KIND_COLORS = {"error": "#d9534f", "warn": "#e0a030", "chat": None, "join": "#4caf50", "leave": "#e0a030",
               "end": "#e0a030", "undo": "#5b9bd5", "undo-warn": "#e0a030"}
WAITING_TEXT = {"tool": "waiting for your current tool action to finish...", "scan": "checking your last change...",
                "host": "waiting for the host to confirm your last change...", "working": "undoing..."}


def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


class CoopDocker(DockWidget):
    def __init__(self):
        DockWidget.__init__(self)
        self.setWindowTitle("Coop Drawing")
        self.ctl = controller()
        root = QWidget(self)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(4, 4, 4, 4)

        # name
        row = QHBoxLayout()
        row.addWidget(QLabel("Your name:"))
        self.name_edit = QLineEdit(self.ctl.setting("name", ""))
        self.name_edit.setPlaceholderText("shown to the others")
        self.name_edit.setMaxLength(40)
        row.addWidget(self.name_edit)
        lay.addLayout(row)

        # start: host / join
        self.start_tabs = QTabWidget()
        host_page = QWidget()
        hl = QFormLayout(host_page)
        hl.addRow(QLabel("Share the document you have open.\nOthers join with your address and code."))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(int(self.ctl.setting("port", str(net.DEFAULT_PORT)) or net.DEFAULT_PORT))
        hl.addRow("Port:", self.port_spin)
        self.host_btn = QPushButton("Start hosting")
        self.host_btn.clicked.connect(self._host)
        hl.addRow(self.host_btn)
        self.start_tabs.addTab(host_page, "Host")

        join_page = QWidget()
        jl = QFormLayout(join_page)
        self.addr_edit = QLineEdit(self.ctl.setting("last_address", ""))
        self.addr_edit.setPlaceholderText("192.168.1.20:%d" % net.DEFAULT_PORT)
        jl.addRow("Address:", self.addr_edit)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("ABCD-EFGH")
        jl.addRow("Code:", self.code_edit)
        self.join_btn = QPushButton("Join")
        self.join_btn.clicked.connect(self._join)
        self.code_edit.returnPressed.connect(self._join)
        jl.addRow(self.join_btn)
        self.start_tabs.addTab(join_page, "Join")
        lay.addWidget(self.start_tabs)

        # live session
        self.session_box = QGroupBox("Session")
        sl = QVBoxLayout(self.session_box)
        self.status_lbl = QLabel()
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sl.addWidget(self.status_lbl)
        self.copy_btn = QPushButton("Copy invite")
        self.copy_btn.setToolTip("Copy the address and code to send to your partner")
        self.copy_btn.clicked.connect(self._copy_invite)
        sl.addWidget(self.copy_btn)
        self.layer_lbl = QLabel()
        self.layer_lbl.setWordWrap(True)
        sl.addWidget(self.layer_lbl)
        urow = QHBoxLayout()
        self.undo_btn = QPushButton("Undo mine")
        self.undo_btn.setToolTip("Undo your own last change (same as Ctrl+Z in the shared document)")
        self.undo_btn.clicked.connect(lambda: self.ctl.request("undo"))
        urow.addWidget(self.undo_btn)
        self.redo_btn = QPushButton("Redo mine")
        self.redo_btn.setToolTip("Redo your own last undone change (same as Ctrl+Shift+Z)")
        self.redo_btn.clicked.connect(lambda: self.ctl.request("redo"))
        urow.addWidget(self.redo_btn)
        sl.addLayout(urow)
        self.undo_lbl = QLabel()
        self.undo_lbl.setWordWrap(True)
        sl.addWidget(self.undo_lbl)
        self.cursor_chk = QCheckBox("Share my cursor")
        self.cursor_chk.setChecked(self.ctl.share_cursor)
        self.cursor_chk.toggled.connect(self._cursor_toggled)
        sl.addWidget(self.cursor_chk)
        self.leave_btn = QPushButton("Leave session")
        self.leave_btn.clicked.connect(self._leave)
        sl.addWidget(self.leave_btn)
        lay.addWidget(self.session_box)

        # log + chat
        self.log_view = QTextBrowser()
        self.log_view.setOpenLinks(False)
        self.log_view.setMinimumHeight(80)
        lay.addWidget(self.log_view, 1)
        crow = QHBoxLayout()
        self.chat_edit = QLineEdit()
        self.chat_edit.setPlaceholderText("Message")
        self.chat_edit.returnPressed.connect(self._chat)
        crow.addWidget(self.chat_edit)
        self.chat_btn = QPushButton("Send")
        self.chat_btn.clicked.connect(self._chat)
        crow.addWidget(self.chat_btn)
        lay.addLayout(crow)

        self.setWidget(root)
        for _, kind, text in self.ctl.history:
            self._append(kind, text)
        self.ctl.changed.connect(self._refresh)
        self.ctl.message.connect(self._append)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_status)
        self.timer.start(400)
        self._refresh()

    def canvasChanged(self, canvas):
        pass

    # ---- actions -----------------------------------------------------------------
    def _name(self):
        name = net.clean_name(self.name_edit.text(), "")
        if not name:
            name = "Host" if self.start_tabs.currentIndex() == 0 else "Guest"
        self.ctl.set_setting("name", self.name_edit.text().strip())
        return name

    def _host(self):
        try:
            self.ctl.set_setting("port", self.port_spin.value())
            self.ctl.host(self._name(), self.port_spin.value())
        except OSError as e:
            self._error("Could not open port %d: %s\nIs another session already using it?"
                        % (self.port_spin.value(), e.strerror or e))
        except Exception as e:  # noqa: BLE001
            self._error(str(e))

    def _join(self):
        try:
            self.ctl.set_setting("last_address", self.addr_edit.text().strip())
            self.ctl.join(self._name(), self.addr_edit.text(), self.code_edit.text())
        except Exception as e:  # noqa: BLE001
            self._error(str(e))

    def _leave(self):
        s = self.ctl.session
        if s is not None and s.role == "host" and len(s.peers) > 1:
            ans = QMessageBox.question(self, "Coop Drawing", "End the session for everyone?")
            if ans != QMessageBox.StandardButton.Yes:
                return
        self.ctl.leave()

    def _chat(self):
        s = self.ctl.session
        text = self.chat_edit.text().strip()
        if s is not None and text:
            s.send_chat(text)
            self.chat_edit.clear()

    def _cursor_toggled(self, on):
        self.ctl.share_cursor = bool(on)
        self.ctl.set_setting("share_cursor", "true" if on else "false")
        s = self.ctl.session
        if not on and s is not None:
            s.send_cursor(None, None)

    def _invite_text(self):
        s = self.ctl.session
        addrs = net.local_addresses() or ["<your IP address>"]
        return "Krita Coop - address: %s  code: %s" % (" or ".join("%s:%d" % (a, s.port) for a in addrs), s.code)

    def _copy_invite(self):
        s = self.ctl.session
        if s is not None and s.role == "host":
            QApplication.clipboard().setText(self._invite_text())
            self._append("info", "Invite copied to the clipboard.")

    def _error(self, text):
        self._append("error", text)
        QMessageBox.warning(self, "Coop Drawing", text)

    # ---- display -------------------------------------------------------------------
    def _append(self, kind, text):
        color = KIND_COLORS.get(kind)
        body = html.escape(text).replace("\n", "<br>")
        if kind == "chat" and ": " in text:
            who, msg = text.split(": ", 1)
            body = "<b>%s:</b> %s" % (html.escape(who), html.escape(msg))
        elif color:
            body = '<span style="color:%s">%s</span>' % (color, body)
        self.log_view.append('<span style="color:gray">%s</span> %s' % (time.strftime("%H:%M"), body))
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _refresh(self):
        live = self.ctl.session is not None
        self.start_tabs.setVisible(not live)
        self.session_box.setVisible(live)
        self.name_edit.setEnabled(not live)
        self.chat_edit.setEnabled(live)
        self.chat_btn.setEnabled(live)
        self._update_status()

    def _update_status(self):
        s = self.ctl.session
        if s is None:
            return
        st = s.status()
        people = ", ".join(("%s (you)" % n if pid == s.my_id else n) for pid, n in sorted(st["peers"].items()))
        lines = []
        if st["role"] == "host":
            addrs = net.local_addresses()
            lines.append("<b>Hosting</b> - code <b>%s</b>" % html.escape(st["code"]))
            lines.append("Address: %s" % (", ".join("%s:%d" % (a, st["port"]) for a in addrs) or
                                          "port %d" % st["port"]))
            self.copy_btn.setVisible(True)
        else:
            state = {"connecting": "Connecting...", "handshake": "Joining...", "live": "Connected"}.get(
                st["state"], st["state"])
            lines.append("<b>%s</b> to %s" % (state, html.escape(st["host"])))
            self.copy_btn.setVisible(False)
        lines.append("People: %s" % html.escape(people or "-"))
        if st["waiting"] and s.waiting_since is not None and time.perf_counter() - s.waiting_since > 1.0:
            lines.append('<span style="color:#e0a030">Holding %d update(s) until you finish the '
                         'current stroke/tool action.</span>' % st["pending"])
        if st["unsupported"]:
            lines.append('<span style="color:#e0a030">%d layer(s) here are not shared (only paint layers '
                         'and groups are).</span>' % st["unsupported"])
        lines.append('<span style="color:gray">sent %s, received %s</span>'
                     % (_fmt_bytes(st["out"]), _fmt_bytes(st["in"])))
        if s.doc is not None:
            if getattr(s.doc, "native", False):
                lines.append('<span style="color:gray">Live mode: strokes are shared while they are '
                             'painted, also while you draw.</span>')
            else:
                lines.append('<span style="color:gray">Standard Krita: others\' strokes appear here when you '
                             'lift the pen (the co-op build of Krita shares them live).</span>')
        self.status_lbl.setText("<br>".join(lines))

        u = st.get("undo") or {}
        if u.get("broken"):
            self.undo_lbl.setText('<span style="color:#d9534f">Undo is off for this session (internal error).</span>')
        else:
            text = "Your undo: %d &middot; redo: %d" % (u.get("undo", 0), u.get("redo", 0))
            if u.get("waiting"):
                text += ' <span style="color:gray">- %s</span>' % WAITING_TEXT.get(u["waiting"], "")
            self.undo_lbl.setText(text)
        self.undo_btn.setEnabled(not u.get("broken") and s.live)
        self.redo_btn.setEnabled(not u.get("broken") and s.live)

        sid, name, kind = s.active_layer()
        if s.doc is None:
            self.layer_lbl.setText("")
        elif sid is None:
            self.layer_lbl.setText('<span style="color:#e0a030">The selected layer is not shared '
                                   '(only paint layers and groups are).</span>')
        else:
            self.layer_lbl.setText("Selected %s <b>%s</b> is shared with everyone."
                                   % ("group" if kind == "group" else "layer", html.escape(name or "")))
