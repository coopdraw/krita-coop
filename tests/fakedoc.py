"""In-memory stand-in for a Krita document, implementing the adapter interface.

It mimics the Krita behaviours the engine depends on: ``set_children``
detaches all current children and refuses nodes that still have a parent,
user edits (paint/add/delete) happen directly, and ``is_idle`` can be turned
off to simulate a stroke in progress.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from krita_coop.docmodel import LNode, Structure  # noqa: E402


class FakeNode(object):
    def __init__(self, kind, name, doc):
        self.uuid = "{%s}" % uuid.uuid4()
        self.kind = kind  # paint / group / other
        self.parent = None
        self.children = []
        self.props = {"name": name, "opacity": 255, "blend": "normal", "ia": False, "cl": 0,
                      "vis": True, "locked": False}
        self.pixels = bytearray(doc.w * doc.h * doc.ps) if kind == "paint" else None


class FakeDoc(object):
    def __init__(self, w=300, h=200, ps=4, name="fake"):
        self.w, self.h, self.ps = w, h, ps
        self.name = name
        self.root = FakeNode("group", "root", self)
        self.nodes = {}
        self.idle = True
        self.open = True
        self.refreshes = 0
        self.active = None
        self.blend_calls = 0
        self.selected = []
        self.time = 0  # animation frame
        self.modified = 0  # mark_modified() calls
        self.busy_writes = 0  # writes while a "stroke" was in progress (must stay 0)

    # ---- helpers for tests (simulated user actions) ------------------------
    def add(self, name, kind="paint", parent=None, index=None):
        n = FakeNode(kind, name, self)
        self.nodes[n.uuid] = n
        p = parent or self.root
        n.parent = p
        p.children.insert(len(p.children) if index is None else index, n)
        self.active = n.uuid
        return n

    def remove(self, n):
        n.parent.children.remove(n)
        n.parent = None

    def move(self, n, parent=None, index=None):
        self.remove(n)
        p = parent or self.root
        n.parent = p
        p.children.insert(len(p.children) if index is None else index, n)

    def paint(self, n, x, y, w, h, color):
        assert len(color) == self.ps
        row = bytes(color) * w
        for yy in range(y, y + h):
            off = (yy * self.w + x) * self.ps
            n.pixels[off:off + len(row)] = row

    def find(self, name):
        def walk(p):
            for c in p.children:
                if c.props["name"] == name:
                    return c
                r = walk(c)
                if r:
                    return r
        return walk(self.root)

    def names(self, parent=None):
        return [c.props["name"] for c in (parent or self.root).children]

    # ---- adapter interface ---------------------------------------------------
    def alive(self):
        return self.open

    def size(self):
        return (self.w, self.h)

    def pixel_size(self):
        return self.ps

    def info(self):
        return {"w": self.w, "h": self.h, "ps": self.ps, "model": "RGBA", "depth": "U8", "profile": "",
                "res": 72.0, "name": self.name, "bg": [255, 255, 255, 255]}

    def read_structure(self):
        st = Structure()

        def walk(p, pu):
            kids = []
            for c in p.children:
                if c.kind == "other":
                    st.unsupported += 1
                st.nodes[c.uuid] = LNode(c.kind, pu, dict(c.props) if c.kind != "other" else None)
                kids.append(c.uuid)
                if c.kind == "group":
                    walk(c, c.uuid)
            st.children[pu] = kids

        walk(self.root, "")
        return st

    def create_node(self, kind, name):
        n = FakeNode(kind, name, self)
        self.nodes[n.uuid] = n
        return n.uuid

    def _n(self, u):
        return self.root if u == "" else self.nodes[u]

    def set_children(self, pu, uuids):
        p = self._n(pu)
        for c in p.children:
            c.parent = None
        p.children = []
        for u in uuids:
            c = self.nodes[u]
            if c.parent is not None:
                raise AssertionError("node %s still has a parent" % c.props["name"])
            c.parent = p
            p.children.append(c)

    def set_props(self, u, props):
        self.nodes[u].props.update(props)

    def set_blend(self, u, blend):
        self.blend_calls += 1
        self.nodes[u].props["blend"] = blend

    def set_locked(self, u, locked):
        self.nodes[u].props["locked"] = locked

    def read_rows(self, u, y, h):
        n = self.nodes.get(u)
        if n is None or n.pixels is None:
            return None
        s = y * self.w * self.ps
        return bytes(n.pixels[s:s + h * self.w * self.ps])

    def read_tile(self, u, x, y, w, h):
        n = self.nodes.get(u)
        if n is None or n.pixels is None:
            return None
        rowb = w * self.ps
        return b"".join(bytes(n.pixels[((y + r) * self.w + x) * self.ps:((y + r) * self.w + x) * self.ps + rowb])
                        for r in range(h))

    def resize(self, w, h):
        """Simulated canvas resize anchored at the top-left (keeps overlapping pixels)."""
        ps = self.ps
        for n in self.nodes.values():
            if n.pixels is None:
                continue
            new = bytearray(w * h * ps)
            for y in range(min(h, self.h)):
                cw = min(w, self.w) * ps
                new[y * w * ps:y * w * ps + cw] = n.pixels[y * self.w * ps:y * self.w * ps + cw]
            n.pixels = new
        self.w, self.h = w, h

    def write_tile(self, u, x, y, w, h, data):
        if not self.idle:
            self.busy_writes += 1
        n = self.nodes[u]
        rowb = w * self.ps
        assert len(data) == rowb * h
        for r in range(h):
            off = ((y + r) * self.w + x) * self.ps
            n.pixels[off:off + rowb] = data[r * rowb:(r + 1) * rowb]

    def is_idle(self):
        return self.idle

    def refresh(self):
        self.refreshes += 1

    def mark_modified(self):
        self.modified += 1

    def current_time(self):
        return self.time

    def selected_uuids(self):
        return list(self.selected)

    def active_uuid(self):
        return self.active

    def set_active(self, u):
        self.active = u

    def resize_canvas(self, w, h):
        self.resize(w, h)


class NativeFakeDoc(FakeDoc):
    """A FakeDoc that also mimics the co-op build of Krita (``kritadoc.KritaDoc.native``):

    * it reports which parts of which layers were changed (``take_dirty``),
    * a brush stroke in "Wash" mode lives in an overlay that is merged into the layer only when
      the stroke ends (``wash`` / ``end_wash``); ``read_rows`` shows it, ``read_tile`` doesn't,
    * a running stroke can be held (``pause`` / ``resume``) so pixels can be written meanwhile,
    * ``is_idle`` means "no stroke is running" (``idle``), ``activity`` names the stroke.
    """

    native = True

    def __init__(self, w=300, h=200, ps=4, name="fake"):
        FakeDoc.__init__(self, w, h, ps, name)
        self.dirty_log = []
        self.report = True  # False: Krita "forgets" to report changes (the full re-read must find them)
        self.stroke = "FREEHAND_STROKE"  # what is running while not idle
        self.overlays = {}  # uuid -> (pixels, mask): the Wash stroke being painted on that layer
        self.merging = set()  # uuids whose stroke is being merged right now: reads answer "busy"
        self.paused = False
        self.pauses = 0
        self.unpaused_busy_writes = 0  # writes during a stroke that was not held (must stay 0)
        self.refuse_pauses = 0  # Krita answers "not now" to this many pause() calls
        self.refused_pauses = 0
        self.refused_writes = 0  # writes Krita refused because the layer was being merged
        self.live_writes = 0  # writes while a stroke was held
        self.settles = 0

    # ---- simulated user actions ------------------------------------------------
    def _log(self, n, x, y, w, h):
        if self.report:
            self.dirty_log.append((n.uuid, x, y, w, h))

    def paint(self, n, x, y, w, h, color):
        FakeDoc.paint(self, n, x, y, w, h, color)
        self._log(n, x, y, w, h)

    def touch(self, n, x, y, w, h):
        """A tool reports the area as changed without changing pixels yet (e.g. Transform starting)."""
        self._log(n, x, y, w, h)

    def wash(self, n, x, y, w, h, color):
        """Paint part of a Wash-mode stroke: it goes into the overlay, not into the layer."""
        ov = self.overlays.get(n.uuid)
        if ov is None:
            ov = self.overlays[n.uuid] = (bytearray(self.w * self.h * self.ps), bytearray(self.w * self.h))
        row = bytes(color) * w
        for yy in range(y, y + h):
            off = yy * self.w + x
            ov[0][off * self.ps:off * self.ps + len(row)] = row
            ov[1][off:off + w] = b"\x01" * w
        self._log(n, x, y, w, h)

    def end_wash(self, n):
        """The stroke ends: merge the overlay into the layer."""
        ov = self.overlays.pop(n.uuid, None)
        if ov is not None:
            n.pixels = bytearray(self._compose(n.pixels, ov))
            self._log(n, 0, 0, self.w, self.h)

    def cancel_wash(self, n):
        if self.overlays.pop(n.uuid, None) is not None:
            self._log(n, 0, 0, self.w, self.h)

    def _compose(self, pixels, ov):
        ps = self.ps
        out = bytearray(pixels)
        px, mask = ov
        i = mask.find(b"\x01")
        while i >= 0:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            out[i * ps:j * ps] = px[i * ps:j * ps]
            i = mask.find(b"\x01", j)
        return bytes(out)

    def shown(self, n):
        """The layer as the canvas shows it (with the stroke in progress)."""
        ov = self.overlays.get(n.uuid)
        return bytes(n.pixels) if ov is None else self._compose(n.pixels, ov)

    # ---- adapter interface -------------------------------------------------------
    def read_rows(self, u, y, h):
        n = self.nodes.get(u)
        if n is None or n.pixels is None:
            return None
        if u in self.merging:
            return b""
        s = y * self.w * self.ps
        return self.shown(n)[s:s + h * self.w * self.ps]

    def read_tile_shown(self, u, x, y, w, h):
        n = self.nodes.get(u)
        if n is None or n.pixels is None or u in self.merging:
            return None
        full = self.shown(n)
        rowb = w * self.ps
        return b"".join(full[((y + r) * self.w + x) * self.ps:((y + r) * self.w + x) * self.ps + rowb]
                        for r in range(h))

    def overlay_active(self, u):
        return u in self.overlays

    def write_tile(self, u, x, y, w, h, data):
        if u in self.merging:
            self.refused_writes += 1
            return False
        if not self.idle:
            if self.paused:
                self.live_writes += 1
            else:
                self.unpaused_busy_writes += 1
        idle, self.idle = self.idle, True  # (FakeDoc counts busy writes; here they are legal)
        try:
            FakeDoc.write_tile(self, u, x, y, w, h, data)
        finally:
            self.idle = idle
        return True

    def activity(self):
        return "" if self.idle else self.stroke

    def pause(self):
        if self.paused:
            return False
        if self.refuse_pauses > 0:
            self.refuse_pauses -= 1
            self.refused_pauses += 1
            return False
        self.paused = True
        self.pauses += 1
        return True

    def resume(self):
        self.paused = False

    def settle(self):
        self.settles += 1

    def take_dirty(self):
        out, self.dirty_log = self.dirty_log, []
        return out
