"""Adapter between the session engine and a live Krita ``Document``.

Everything here runs on Krita's GUI thread.  The calls were chosen for how they
behave in Krita 5.x (verified against libkis sources and on 5.3.1):

* ``Node.setPixelData`` writes straight into the layer: no undo entry, no
  repaint (hence ``refresh()``), and it works on locked layers.
* ``Node.setChildNodes`` uses ``image->addNode/removeNode`` directly: no undo
  entry.  ``addChildNode``/``remove`` would go on the user's undo stack, so they
  are never used for remote changes.
* ``setName/setOpacity/setVisible/setLocked`` change the node directly.
  ``setBlendingMode`` runs a one-command stroke and waits, so it is only called
  while the image is idle.
* ``refreshProjection`` waits for the image to finish all work, so the engine
  only calls it after ``is_idle()`` said no stroke is running.

The co-op build of Krita adds ``coop*`` functions to Document and Node (see
``krita-patches/``).  When they exist (``native``), the adapter can also

* read a layer *including* the stroke being painted in "Wash" mode,
* pause a running stroke between two of its jobs, so other people's pixels can be
  written while you draw,
* write pixels with an asynchronous update of just that area (no blocking
  full recomposite),
* tell which parts of which layers have just changed, and
* tell "no stroke is running" without locking the image.

On stock Krita the same code runs without these (remote changes then wait until
you lift the pen, and Wash strokes appear for others when they end).
"""

import struct
import uuid as uuidlib

from krita import Krita

try:
    from PyQt5.QtCore import QByteArray
    from PyQt5.QtGui import QColor
except ImportError:  # Krita 6 / PyQt6
    from PyQt6.QtCore import QByteArray
    from PyQt6.QtGui import QColor

from .docmodel import LNode, Structure

ROOT = ""


class KritaDoc(object):
    def __init__(self, document):
        self.d = document
        self._nodes = {}
        self._model = document.colorModel()
        self._depth = document.colorDepth()
        self._ps = None
        self.native = False  # the co-op build's extra API is there
        try:
            self.native = hasattr(document, "coopApiVersion") and document.coopApiVersion() >= 1
        except Exception:  # noqa: BLE001
            self.native = False
        self._paused = False
        if self.native:
            document.coopSetTracking(True)

    @classmethod
    def create(cls, info):
        """Create (and show) a new document matching the host's description."""
        k = Krita.instance()
        w, h = int(info["w"]), int(info["h"])
        name = "Coop - %s" % str(info.get("name") or "shared")[:80]
        model = str(info.get("model") or "RGBA")
        depth = str(info.get("depth") or "U8")
        res = float(info.get("res") or 300.0)
        doc = k.createDocument(w, h, name, model, depth, str(info.get("profile") or ""), res)
        if doc is None:  # profile not installed here: fall back to the default one
            doc = k.createDocument(w, h, name, model, depth, "", res)
        if doc is None:
            raise RuntimeError("Krita cannot create a %s/%s document" % (model, depth))
        bg = info.get("bg")
        if isinstance(bg, list) and len(bg) == 4:
            try:
                doc.setBackgroundColor(QColor(*[int(v) for v in bg]))
            except Exception:  # noqa: BLE001 - cosmetic
                pass
        win = k.activeWindow()
        if win is not None:
            win.addView(doc)
        return cls(doc)

    # ---- document ----------------------------------------------------------
    def alive(self):
        try:
            return self.d.rootNode() is not None
        except Exception:  # noqa: BLE001
            return False

    def size(self):
        return (self.d.width(), self.d.height())

    def pixel_size(self):
        if self._ps is None:
            self._ps = len(bytes(self.d.pixelData(0, 0, 1, 1)))
        return self._ps

    def info(self):
        bg = self.d.backgroundColor()
        return {"w": self.d.width(), "h": self.d.height(), "ps": self.pixel_size(),
                "model": self._model, "depth": self._depth, "profile": self.d.colorProfile(),
                "res": float(self.d.resolution()), "name": self.d.name() or "Untitled",
                "bg": [bg.red(), bg.green(), bg.blue(), bg.alpha()]}

    def release(self):
        """Drop all node wrappers (call before Krita shuts down or the doc closes)."""
        self._nodes = {}
        if self.native:
            try:
                self.resume()
                self.d.coopSetTracking(False)
            except Exception:  # noqa: BLE001 - the document may be gone already
                pass

    def same_document(self, other):
        """Same Krita document?  (libkis compares the underlying KisDocument; node ids are not
        enough: "Create Copy From Current Image" keeps them, Flatten replaces the root.)"""
        try:
            return other is not None and other == self.d
        except Exception:  # noqa: BLE001
            return False

    # ---- structure -----------------------------------------------------------
    def read_structure(self):
        st = Structure()
        self._nodes = {}
        self._walk(self.d.rootNode(), ROOT, st, 0)
        return st

    def _walk(self, node, pu, st, depth):
        kids = []
        for ch in node.childNodes():
            u = ch.uniqueId().toString()
            self._nodes[u] = ch
            t = ch.type()
            if t == "paintlayer" and ch.colorModel() == self._model and ch.colorDepth() == self._depth:
                kind = "paint"
            elif t == "grouplayer":
                kind = "group"
            else:
                kind = "other"
                if t.endswith("layer"):  # masks are not counted as unsupported layers
                    st.unsupported += 1
            props = None
            if kind != "other":
                props = {"name": ch.name(), "opacity": ch.opacity(), "blend": ch.blendingMode(),
                         "ia": ch.inheritAlpha(), "cl": ch.colorLabel(), "vis": ch.visible(),
                         "locked": ch.locked()}
            st.nodes[u] = LNode(kind, pu, props)
            kids.append(u)
            if kind == "group" and depth < 64:
                self._walk(ch, u, st, depth + 1)
        st.children[pu] = kids

    def create_node(self, kind, name):
        n = self.d.createNode(name, "paintlayer" if kind == "paint" else "grouplayer")
        u = n.uniqueId().toString()
        self._nodes[u] = n
        return u

    def _node(self, u):
        return self.d.rootNode() if u == ROOT else self._nodes[u]

    def set_children(self, pu, uuids):
        self._node(pu).setChildNodes([self._nodes[u] for u in uuids])

    def set_props(self, u, props):
        n = self._nodes[u]
        if "name" in props:
            n.setName(props["name"])
        if "opacity" in props:
            n.setOpacity(int(props["opacity"]))
        if "ia" in props:
            n.setInheritAlpha(bool(props["ia"]))
        if "cl" in props:
            n.setColorLabel(int(props["cl"]))
        if "vis" in props:
            n.setVisible(bool(props["vis"]))
        if "locked" in props:
            n.setLocked(bool(props["locked"]))

    def set_blend(self, u, blend):
        self._nodes[u].setBlendingMode(blend)

    def set_locked(self, u, locked):
        n = self._nodes.get(u)
        if n is not None:
            n.setLocked(bool(locked))

    # ---- pixels ------------------------------------------------------------
    def read_rows(self, u, y, h):
        """A full-width strip of the layer as others should see it: with the co-op build that
        includes the Wash stroke being painted.  An empty result means "busy, try again"."""
        n = self._nodes.get(u)
        if n is None:
            return None
        if self.native:
            return n.coopPixelData(0, y, self.d.width(), h)
        return n.pixelData(0, y, self.d.width(), h)  # QByteArray: supports the buffer protocol

    def read_tile(self, u, x, y, w, h):
        """The pixels stored in the layer itself (without a Wash stroke in progress)."""
        n = self._nodes.get(u)
        return bytes(n.pixelData(x, y, w, h)) if n is not None else None

    def read_tile_shown(self, u, x, y, w, h):
        """Like read_rows() for one tile; None while the stroke is being merged into the layer."""
        n = self._nodes.get(u)
        if n is None:
            return None
        if not self.native:
            return bytes(n.pixelData(x, y, w, h))
        b = bytes(n.coopPixelData(x, y, w, h))
        return b or None

    def overlay_active(self, u):
        """Is a Wash stroke being painted over this layer right now?"""
        n = self._nodes.get(u)
        return bool(self.native and n is not None and n.coopOverlayActive())

    def write_tile(self, u, x, y, w, h, data):
        if self.native:
            self._nodes[u].coopSetPixelData(data, x, y, w, h)  # + async update of that area
        else:
            self._nodes[u].setPixelData(data, x, y, w, h)

    # ---- scheduling ----------------------------------------------------------
    def is_idle(self):
        """True when no stroke or tool action is running.  (On stock Krita: also no canvas
        update, so blocking calls are safe.)"""
        if self.native:
            try:
                return not self.d.coopHasStrokes()
            except Exception:  # noqa: BLE001
                return False
        try:
            ok = self.d.tryBarrierLock()
        except Exception:  # noqa: BLE001
            return False
        if ok:
            self.d.unlock()
        return ok

    def activity(self):
        """Id of the stroke running now ("FREEHAND_STROKE" for brush strokes), "" if none,
        None if this Krita can't tell."""
        if not self.native:
            return None
        try:
            return self.d.coopActivity()
        except Exception:  # noqa: BLE001
            return None

    def pause(self):
        """Hold the running stroke between two of its jobs (co-op build only). While held, the
        layers can be read and written without racing against it. Always resume()."""
        if not self.native or self._paused:
            return False
        self._paused = bool(self.d.coopPause())
        return self._paused

    def resume(self):
        if self._paused:
            self._paused = False
            self.d.coopResume()

    def settle(self):
        """Wait for canvas updates still running (call only when no stroke is running)."""
        if self.native:
            self.d.waitForDone()

    def take_dirty(self):
        """[(uuid, x, y, w, h)] changed since the last call; None if this Krita can't tell."""
        if not self.native:
            return None
        raw = bytes(self.d.coopTakeDirty())
        out = []
        for off in range(0, len(raw) - 31, 32):
            x, y, w, h = struct.unpack_from("<iiii", raw, off + 16)
            out.append(("{%s}" % uuidlib.UUID(bytes=raw[off:off + 16]), x, y, w, h))
        return out

    def refresh(self):
        self.d.refreshProjection()

    def mark_modified(self):
        """setPixelData & co. don't flag the document as changed; without this, closing Krita
        would not offer to save what others painted (or what an undo changed).

        A plain setModified(True) is not enough: Krita clears it again whenever its undo stack
        returns to the saved state.  Adding (and removing) an annotation goes through
        KisImage::setModifiedWithoutUndo, which keeps the document modified until the next save
        - also when the change lands during a background save, so it must not be skipped just
        because the document already looks modified (the session calls this once per tick)."""
        try:
            self.d.setAnnotation("krita-coop-modified", "", QByteArray())
            self.d.removeAnnotation("krita-coop-modified")
        except Exception:  # noqa: BLE001
            try:
                self.d.setModified(True)
            except Exception:  # noqa: BLE001
                pass

    def current_time(self):
        try:
            return self.d.currentTime()
        except Exception:  # noqa: BLE001
            return 0

    def selected_uuids(self):
        """Layers selected in the active view (if it shows this document); [] if unknown."""
        try:
            win = Krita.instance().activeWindow()
            view = win.activeView() if win is not None else None
            if view is None or not self.same_document(view.document()):
                return []
            return [n.uniqueId().toString() for n in view.selectedNodes()]
        except Exception:  # noqa: BLE001
            return []

    def active_uuid(self):
        n = self.d.activeNode()
        return n.uniqueId().toString() if n is not None else None

    def set_active(self, u):
        n = self._nodes.get(u)
        if n is not None:
            self.d.setActiveNode(n)

    def resize_canvas(self, w, h):
        self.d.resizeImage(0, 0, int(w), int(h))
        self.d.waitForDone()
