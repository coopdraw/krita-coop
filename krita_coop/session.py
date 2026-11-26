"""Session engine (host and guest).

Everything here runs on Krita's GUI thread through ``tick()``; network threads
only fill queues.  The document is reached through an adapter
(``kritadoc.KritaDoc``; tests use a fake), so this module has no Krita or Qt
imports.

Architecture
------------
* The host runs a ``Server`` (pure data: layer tree + every tile) and a
  ``Client`` for its own document.  Guests run only a ``Client``.
* Every layer is shared.  Clients detect local edits pixel by pixel and send
  them as patches; the server merges patches in arrival order and broadcasts
  the merged tiles.  Clients merge those back into their document while
  keeping their own not-yet-confirmed edits (see ``replica.py``).
* Layer structure works the same way: clients send operations, the server
  applies them to its tree and broadcasts the tree.
* Writes into a Krita document happen only while it is idle (no stroke in
  progress) and use calls that bypass Krita's command system, so remote
  changes never appear as entries in anyone's undo history.
* With the co-op build of Krita (``doc.native``, see ``kritadoc.py``) the
  engine is told which parts of which layers change, sees a Wash stroke while
  it is being painted, and writes other people's pixels while you are
  mid-stroke: the stroke is held between two of its jobs for a few
  milliseconds (``_apply_live``).  Only what you are working on right now
  waits: the tiles under a Wash stroke in progress, and layers under an open
  tool action such as Transform.
* Krita's own undo can't work on a shared layer (it takes back the newest
  change on the layer, whoever made it), so during a session undo/redo is
  per person and done by the plugin (``undo.py``); the GUI routes Krita's
  Undo/Redo actions for the shared document to ``request_undo/redo``.
"""

import collections
import queue
import time
import traceback
import uuid as uuidlib

from . import net
from . import protocol as P
from .replica import UNKNOWN, Replica
from .server import LOCAL, Server
from .tree import KINDS, ROOT, SYNC_PROPS, Tree, diff_ops, valid_sid
from .undo import UndoEngine

HOST_ID = LOCAL


class Config(object):
    # Small, frequent slices keep Krita's GUI thread responsive while drawing.
    tick_ms = 16
    scan_fast = 0.004  # seconds of scanning per tick while changes are flowing
    scan_slow = 0.0015  # ... when nothing changed recently
    scan_bulk = 0.010  # ... while publishing whole layers
    fast_window = 2.0
    apply_budget = 0.008  # writing others' changes (only while you are not mid-stroke)
    apply_bulk = 0.025  # ... while a big transfer is queued (joining, resync)
    tree_interval = 0.25
    refresh_interval = 0.15
    max_out_bytes = 48 << 20  # pause scanning while this much is waiting to be sent
    # per-person undo (undo.py)
    undo_max_units = 100  # per person (the host keeps the same depth of history)
    undo_wait = 1.0  # drop a request after this long busy with no new pixels (an open tool action)...
    undo_wait_max = 10.0  # ... or after this long busy at all
    undo_echo_wait = 10.0  # max wait for the host's answer to an undo/redo
    undo_flush_cap = 0.040  # seconds per tick for re-reading the layer before an undo
    scan_seal = 0.008  # scan budget per tick while closing an action
    undo_queue_max = 32
    undo_max_skips = 10
    # co-op build of Krita only
    live_interval = 0.05  # seconds between writes of others' pixels while you are mid-stroke
    live_budget = 0.006  # ... and how long your stroke may be held for each of them
    scan_background = 0.0008  # full re-read of all layers per tick, on top of the reported changes
    scan_strip_cap = None  # tests: limit strips scanned per tick


class Client(object):
    """One replica of the shared canvas, bound to a local document."""

    role = "?"

    def __init__(self, name, cfg=None):
        self.cfg = cfg or Config()
        self.name = net.clean_name(name, "Me")
        self.doc = None
        self.my_id = None
        self.peers = {}
        self.sid_of = {}  # local uuid -> sid
        self.uuid_of = {}  # sid -> local uuid
        self.expected = Tree()  # the tree as last agreed with the host
        self.seq = 0
        self.pending_tree = None
        self.replica = Replica(self._send_patch, self._send_complete, self._send_resync, self.uuid_of.get)
        self.size = (0, 0)  # the session's canvas size (set by the host)
        self._local_size = None  # what we last saw or set our own canvas to
        self.live = False
        self.events = []
        self.cursors = {}
        self.closed = False
        self.close_reason = None
        self.dirty = False
        self.last_refresh = 0.0
        self._refresh_cost = 0.0
        self.last_tree_check = 0.0
        self.waiting_since = None
        self.unsupported = 0
        self._last_structure = None
        self._idle_memo = None
        self._refresh_now = False
        self._modified_marked = False
        self.wrote_pixels = False  # we wrote into the document outside Krita's undo (others' paint, undo)
        self.own_command_t = -1e9  # when we last ran a Krita command for someone else's change
        self._frame = None
        self._last_live = -1e9
        self.undo = UndoEngine(self)
        self.replica.hist = self.undo.hist
        self.replica.strip_cap = self.cfg.scan_strip_cap

    # ---- transport (subclasses) ------------------------------------------------------
    def _send_ops(self, seq, ops):
        raise NotImplementedError

    def _send_patch(self, sid, pseq, entries, unit=None):
        raise NotImplementedError

    def _send_undo(self, kind, unit, req):
        raise NotImplementedError

    def _send_complete(self, sid):
        raise NotImplementedError

    def _send_resync(self, sid):
        raise NotImplementedError

    def _send_relay(self, header):
        raise NotImplementedError

    def _out_pressure(self):
        return 0

    def _shutdown_net(self):
        pass

    # ---- utilities ----------------------------------------------------------------------
    def log(self, text, kind="info"):
        self.events.append((kind, text))

    def peer_name(self, pid):
        return self.peers.get(pid, "someone")

    def layer_name(self, sid):
        n = self.expected.nodes.get(sid)
        return n.get("name", "a layer") if n else "a layer"

    def _idle(self):
        """Is the image idle (no stroke or tool action running)?  Probed at most once per tick:
        nothing can start a stroke during our tick, and our own calls leave the image idle."""
        if self._idle_memo is None:
            self._idle_memo = self.doc.is_idle()
        return self._idle_memo

    def _native(self):
        """Is the document in the co-op build of Krita (see kritadoc.py)?"""
        return bool(getattr(self.doc, "native", False))

    def _take_hints(self):
        """Co-op build: Krita reports which parts of which layers were just changed."""
        r = self.replica
        for u, x, y, w, h in self.doc.take_dirty() or ():
            sid = self.sid_of.get(u)
            if sid is not None:
                r.hint(sid, x, y, w, h)
        if self._idle():
            r.clear_touched()  # nothing is being worked on any more

    def mark_modified(self):
        """Pixels written without Krita's command system don't mark the document as changed."""
        if not self._modified_marked:
            self._modified_marked = True
            self.doc.mark_modified()

    def _u(self, fn, *args):
        """Call into the undo engine; a bug there switches undo off but never stops syncing."""
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001
            u = self.undo
            u.hist.fail(traceback.format_exc())
            u.queue.clear()
            u.pending = None
            return None

    def bind(self, sid, uuid):
        old = self.uuid_of.pop(sid, None)
        if old is not None:
            self.sid_of.pop(old, None)
        prev = self.sid_of.pop(uuid, None)
        if prev is not None:
            self.uuid_of.pop(prev, None)
        self.sid_of[uuid] = sid
        self.uuid_of[sid] = uuid

    def unbind_sid(self, sid):
        u = self.uuid_of.pop(sid, None)
        if u is not None:
            self.sid_of.pop(u, None)

    def _new_sid(self, uuid, used):
        cand = uuid.strip("{}")
        if valid_sid(cand) and cand not in self.uuid_of and cand not in used:
            return cand
        return uuidlib.uuid4().hex

    # ---- tree <-> document ---------------------------------------------------------------
    def read_structure(self):
        st = self.doc.read_structure()
        self._last_structure = st
        self.unsupported = st.unsupported
        return st

    def build_tree(self, st):
        t = Tree()
        used = set()

        def walk(pu, psid):
            for u in st.children.get(pu, []):
                ln = st.nodes[u]
                if ln.kind not in KINDS:
                    continue
                sid = self.sid_of.get(u)
                if sid is None or sid in used:
                    sid = self._new_sid(u, used)
                    self.bind(sid, u)
                used.add(sid)
                node = {k: ln.props[k] for k in SYNC_PROPS}
                node["type"] = ln.kind
                node["vis"] = ln.props["vis"]
                t.add(sid, psid, node)
                if ln.kind == "group":
                    walk(u, sid)

        walk(ROOT, ROOT)
        return t

    def reconcile(self, desired, drop=()):
        """Make the local document's layer tree match ``desired``. Returns created sids."""
        doc = self.doc
        st = self.read_structure()
        active = doc.active_uuid()
        drop = set(drop)
        present = {}
        for u, ln in st.nodes.items():
            if ln.kind in KINDS and u not in drop:
                s = self.sid_of.get(u)
                if s is not None and s not in present:
                    present[s] = u
        created = []
        for sid in desired.preorder():
            if sid not in present:
                n = desired.nodes[sid]
                u = doc.create_node(n["type"], n["name"])
                self.bind(sid, u)
                present[sid] = u
                created.append(sid)
        keep = set(present[s] for s in desired.nodes)

        def puuid(psid):
            return ROOT if psid == ROOT else present[psid]

        final_parent = {present[s]: puuid(desired.parent[s]) for s in desired.nodes}

        def deleted(u):
            ln = st.nodes.get(u)
            return u in drop or (ln is not None and ln.kind in KINDS and u in self.sid_of and u not in keep)

        parents = [ROOT] + [s for s in desired.preorder() if desired.nodes[s]["type"] == "group"]
        order_idx = {}
        final = collections.OrderedDict()
        for i, psid in enumerate(parents):
            pu = puuid(psid)
            order_idx[pu] = i
            result = [present[s] for s in desired.children.get(psid, [])]
            anchor = None
            for u in st.children.get(pu, []):
                if u in keep:
                    if final_parent[u] == pu:
                        anchor = u
                    continue
                if deleted(u):
                    continue
                # Unsynced local node (mask, vector layer, ...): keep it where it was.
                pos = result.index(anchor) + 1 if anchor in result else 0
                result.insert(pos, u)
                anchor = u
            final[pu] = result

        # A node can only have one parent: detach movers whose old parent would
        # otherwise be rebuilt after their new one.
        cur = {p: list(k) for p, k in st.children.items()}
        changed = False
        for p, kids in list(cur.items()):
            movers = [u for u in kids if u in final_parent and final_parent[u] != p]
            if movers and any(p not in order_idx or order_idx[p] > order_idx[final_parent[u]] for u in movers):
                rest = [u for u in kids if u not in movers]
                doc.set_children(p, rest)
                cur[p] = rest
                changed = True
        for pu, kids in final.items():  # root first, then groups top-down
            if cur.get(pu, []) != kids:
                doc.set_children(pu, kids)
                cur[pu] = kids
                changed = True

        deferred = []
        created_set = set(created)
        for sid in desired.preorder():
            n = desired.nodes[sid]
            u = present[sid]
            ln = None if sid in created_set else st.nodes.get(u)
            curp = ln.props if ln is not None else {"name": n["name"], "opacity": 255, "blend": "normal",
                                                     "ia": False, "cl": 0, "locked": False}
            ch = {k: n[k] for k in SYNC_PROPS if curp.get(k) != n[k]}
            if ln is None:
                ch["vis"] = n["vis"]
            blend = ch.pop("blend", None)
            if ch:
                doc.set_props(u, ch)
            if blend is not None:
                deferred.append((u, blend))
        for u, blend in deferred:  # runs through Krita's command system: needs an idle image
            doc.set_blend(u, blend)
            self.own_command_t = time.perf_counter()
        for sid in list(self.uuid_of):
            if sid not in desired.nodes:
                self.unbind_sid(sid)
        if changed and active is not None and (active in keep or (active in st.nodes and not deleted(active))):
            doc.set_active(active)
        self.dirty = True
        return created

    # ---- messages from the host --------------------------------------------------------
    def _on_server(self, kind, header, entries):
        if kind == "tree":
            self.pending_tree = header
            self._adopt_size(header)
        elif kind == "tiles":
            self.replica.on_tiles(header, entries or [], self.my_id)
        elif kind == "complete":
            self.replica.on_complete(header.get("layer"))
        elif kind == "ack":
            self.replica.on_ack(header.get("oseq"))
        elif kind == "undone":
            self._u(self.undo.on_undone, header)
        elif kind == "peers":
            peers = header.get("peers")
            if isinstance(peers, dict):
                self.peers = {int(k): net.clean_name(v) for k, v in peers.items() if str(k).isdigit()}
        elif kind == "chat":
            self.log("%s: %s" % (self.peer_name(header.get("from")), str(header.get("text", ""))[:2000]), "chat")
        elif kind == "cursor":
            pid, x, y = header.get("from"), header.get("x"), header.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                self.cursors[pid] = (float(x), float(y), time.perf_counter())
            else:
                self.cursors.pop(pid, None)
        elif kind == "notice":
            self.log(str(header.get("text", ""))[:500], "warn")

    def _adopt_size(self, header):
        try:
            w, h = int(header.get("w")), int(header.get("h"))
        except (TypeError, ValueError):
            return
        if (w, h) != self.size and 0 < w <= 65535 and 0 < h <= 65535:
            self.size = (w, h)
            self.replica.set_geometry(w, h, self.replica.ps, authoritative=False)
            if self.role == "guest":
                self.log("The host changed the canvas to %dx%d." % (w, h))

    # ---- main loop ------------------------------------------------------------------------
    def tick(self):
        if self.closed:
            return
        self._drain()  # echoes of our patches become undo records here
        if self.closed or self.doc is None or not self.live:
            return
        if not self.doc.alive():
            self.stop("The shared document was closed.")
            return
        self._idle_memo = None
        self._modified_marked = False
        self.undo.tracker.begin_tick()
        now = time.perf_counter()
        u = self.undo
        try:
            self._check_frame()
            if self._native():
                self._take_hints()
            self._detect(now)
            self._u(u.track, now)  # closes the undo unit of an action that just ended
            self._apply(now)
            self._u(u.run, now)  # after _apply (others' latest pixels count), before _scan
            self._scan(now)
            self._u(u.tracker.settle, u.hist, self.replica)
        except P.ProtocolError as e:
            self.log("Received damaged data (%s); asking for a fresh copy." % e, "warn")
            for sid in list(self.replica.layers):
                self._send_resync(sid)
        if self.dirty and self._idle():
            soon = self._refresh_now and time.perf_counter() - self.last_refresh >= 4 * self._refresh_cost
            if soon or self._refresh_due():
                self._refresh()

    def _check_frame(self):
        t = self.doc.current_time()
        if t != self._frame:
            if self._frame is not None:
                # Every animated layer now shows another frame: that's not an edit to undo.
                self._u(self.undo.rebaseline)
            self._frame = t

    def restore_from_host(self, why):
        """Krita changed our layers behind our back (its own undo ran, e.g. from the Crop tool):
        put the host's content back instead of sending those pixels to everyone."""
        if not self.live or self.closed:
            return
        r = self.replica
        for L in r.layers.values():
            for ty in range(r.nty):
                for tx in range(r.ntx):
                    key = (tx, ty)
                    if key not in L.srv:
                        if not L.complete:
                            continue
                        L.srv[key] = r._zero_state(r.geom(tx, ty)[2])
                    L.exp[key] = UNKNOWN  # what is in the layer now is not an edit of ours
                    L.pending.add(key)
            L.strip_crc = [None] * r.nty
        self._u(self.undo.reset, None)
        self.log("Krita undid a step of the shared document by itself (%s); restoring the shared "
                 "picture. Avoid that tool action during a session." % why, "warn")

    def _refresh_due(self):
        # Krita can only recomposite the whole image; on big canvases that is slow,
        # so refresh less often to keep it under ~12% of the GUI thread.
        interval = max(self.cfg.refresh_interval, 8 * self._refresh_cost)
        return time.perf_counter() - self.last_refresh >= interval

    def _refresh(self):
        t = time.perf_counter()
        self.doc.refresh()
        self.last_refresh = time.perf_counter()
        self._refresh_cost = 0.7 * self._refresh_cost + 0.3 * (self.last_refresh - t)
        self.dirty = False
        self._refresh_now = False

    def _detect(self, now=None, force=False, phase=None):
        now = time.perf_counter() if now is None else now
        if not force and now - self.last_tree_check < self.cfg.tree_interval:
            return
        self.last_tree_check = now
        if self.doc.size() != self.size:
            if self.role == "guest" and self.doc.size() == self._local_size:
                return  # the host resized the canvas; _apply() follows suit once idle
            self._on_local_resize()
            return
        st = self.read_structure()
        local = self.build_tree(st)
        ops = diff_ops(self.expected, local)
        deletes = False
        if ops:
            self.seq += 1
            self._send_ops(self.seq, ops)
            for op in ops:
                if op["op"] == "create" and op["type"] == "paint":
                    self.replica.add_local(op["id"])
                elif op["op"] == "delete":
                    deletes = True
                    self.replica.drop(op["id"])
                    self.unbind_sid(op["id"])
        self.expected = local
        if ops:
            self._u(self.undo.layer_ops, deletes, phase or self.undo.tracker.state)

    def _apply(self, now):
        tree_ready = self.pending_tree is not None and self.pending_tree.get("ack", -1) >= self.seq
        resize = self.role == "guest" and self.doc.size() != self.size
        if not (tree_ready or resize or self.replica.has_pending()):
            self.waiting_since = None
            return
        native = self._native()
        if not self._idle():  # a stroke or tool action is in progress
            if native and self.replica.has_pending():
                self._apply_live(now)
            if not (tree_ready or resize or self.replica.has_pending()):
                self.waiting_since = None
            elif self.waiting_since is None:
                self.waiting_since = now
            return
        self.waiting_since = None
        if native and (resize or tree_ready):
            self.doc.settle()  # canvas updates for pixels we wrote may still be running
        if resize:
            self.doc.resize_canvas(*self.size)
            self.own_command_t = time.perf_counter()
            self._local_size = self.size
            self.dirty = True
            self.mark_modified()
        if tree_ready:
            self._apply_tree()
        pending = sum(len(L.pending) for L in self.replica.layers.values())
        budget = self.cfg.apply_bulk if pending > 512 else self.cfg.apply_budget
        if self.replica.apply(budget, self.sid_of.get(self.doc.active_uuid())):
            if not native:  # (the co-op build repaints just the written area by itself)
                self.dirty = True
            self.wrote_pixels = True
            self.mark_modified()
        if self.dirty and self._refresh_due() and not self.undo.queue:
            self._refresh()  # (with an undo coming this tick, one refresh at the end of the tick)

    def _apply_live(self, now):
        """Co-op build: write others' pixels although a stroke or tool action is running here.

        The stroke is held between two of its jobs meanwhile, so reading a tile, merging and
        writing it back can't lose a dab; it never notices more than a few milliseconds."""
        cfg = self.cfg
        if now - self._last_live < cfg.live_interval:
            return
        # A brush stroke (or just its last jobs) versus any other tool action: see Replica.apply().
        mode = "paint" if self.doc.activity() in ("FREEHAND_STROKE", "") else "tool"
        active = self.sid_of.get(self.doc.active_uuid())
        hold = ()
        if mode == "tool":  # such a tool works on the selected layer(s), whether or not it has touched them yet
            hold = set(self.sid_of.get(u) for u in self.doc.selected_uuids())
            hold.add(active)
        if not self.doc.pause():
            return
        try:
            wrote = self.replica.apply(cfg.live_budget, active, live=mode, hold=hold)
        finally:
            self.doc.resume()
        self._last_live = time.perf_counter()
        if wrote:
            self.wrote_pixels = True
            self.mark_modified()

    def _apply_tree(self):
        header = self.pending_tree
        self._detect(force=True)  # anything new locally must reach the host first
        if self.seq > header.get("ack", -1):
            return  # keep waiting: the host will send a tree acknowledging it
        self.pending_tree = None
        desired = Tree.from_nested(header.get("tree"))
        if diff_ops(self.expected, desired):  # someone else changed the layers
            self.mark_modified()
        self.reconcile(desired)
        for sid in desired.paint_sids():
            if sid not in self.replica.layers:
                self.replica.ensure(sid)
        for sid in list(self.replica.layers):
            if sid not in desired.nodes:
                self.replica.drop(sid)
        self.expected = self.build_tree(self.read_structure())

    def _scan(self, now):
        if self.doc.size() != self.size or self._out_pressure() > self.cfg.max_out_bytes:
            return
        r = self.replica
        if r.busy():
            budget = self.cfg.scan_bulk
        elif now - r.last_change < self.cfg.fast_window:
            budget = self.cfg.scan_fast
        else:
            budget = self.cfg.scan_slow
        seal = None
        if self._u(self.undo.boost):
            seal = self.cfg.scan_seal  # extra time only for re-reading the layers of a finished action
        # Co-op build: Krita reports what changed, so re-reading everything is only a safety net.
        background = self.cfg.scan_background if self._native() and not r.busy() else None
        r.scan(budget, self.sid_of.get(self.doc.active_uuid()), seal, background)

    # ---- public API used by the UI --------------------------------------------------------
    def active_layer(self):
        """(sid, name, kind) of the selected layer, or (None, None, None) if not shared."""
        if self.doc is None or self.closed:
            return None, None, None
        u = self.doc.active_uuid()
        sid = self.sid_of.get(u)
        st = self._last_structure
        if sid is None or st is None or u not in st.nodes or not st.nodes[u].props:
            return None, None, None
        ln = st.nodes[u]
        return sid, ln.props["name"], ln.kind

    def send_chat(self, text):
        text = str(text)[:2000]
        if text:
            self.log("%s: %s" % (self.name, text), "chat")
            self._send_relay({"t": "chat", "text": text})

    def send_cursor(self, x, y):
        if self.live:
            self._send_relay({"t": "cursor", "x": x, "y": y})

    def pending_count(self):
        return sum(len(L.pending) for L in self.replica.layers.values())

    def request_undo(self):
        """Undo your last change (queued; carried out on the next ticks). False if refused."""
        return self.live and not self.closed and self.undo.request("undo")

    def request_redo(self):
        return self.live and not self.closed and self.undo.request("redo")

    def note_input(self):
        """The user pressed on the shared document's canvas (from the GUI)."""
        self.undo.note_input()

    def note_native_action(self, text):
        """Krita pushed a command onto the shared document's own undo stack (from the GUI)."""
        if self.live and not self.closed:
            self._u(self.undo.note_native_action, text)

    def undo_state(self):
        return self.undo.state()

    def stop(self, reason=None):
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        self.undo.reset(None)
        self._shutdown_net()
        release = getattr(self.doc, "release", None)
        if release is not None:
            release()  # never keep Krita node wrappers alive past the session
        self.log(reason or "Session ended.", "end")


class HostSession(Client):
    role = "host"

    def __init__(self, doc, name, port=net.DEFAULT_PORT, code=None, cfg=None):
        Client.__init__(self, name, cfg)
        self.doc = doc
        self.my_id = HOST_ID
        self.code = code or net.make_code()
        self.net_inbox = queue.Queue()
        self.local_inbox = collections.deque()
        self.listener = net.Server(port, self.code, self.net_inbox)
        self.port = port
        self.conns = {}
        self.next_peer = 1
        self.server = None

    def start(self):
        w, h = self.doc.size()
        ps = self.doc.pixel_size()
        self.server = Server(self.doc.info(), self.local_inbox.append)
        self.server.add_local(self.name)
        self.peers = {HOST_ID: self.name}
        self.size = (w, h)
        self.replica.doc = self.doc
        self.replica.set_geometry(w, h, ps, authoritative=True)
        self.listener.start()
        self.port = self.listener.port
        self.live = True
        self._detect(force=True)  # publishes the existing layers: create ops, then their pixels
        self.undo.reset(None)  # ... which are not undo steps
        self.log("Hosting on port %d. Session code: %s" % (self.port, self.code))

    # transport: the server lives in this process
    def _send_ops(self, seq, ops):
        self.server.handle_ops(HOST_ID, {"seq": seq, "ops": ops})

    def _send_patch(self, sid, pseq, entries, unit=None):
        header = {"layer": sid, "pseq": pseq}
        if unit is not None:
            header["unit"] = unit
        self.server.handle_patch(HOST_ID, header, entries)

    def _send_undo(self, kind, unit, req):
        self.server.handle_undo(HOST_ID, {"t": kind, "unit": unit, "req": req})

    def _send_complete(self, sid):
        self.server.handle_complete(HOST_ID, {"layer": sid})

    def _send_resync(self, sid):
        self.server.handle_resync(HOST_ID, {"layer": sid})

    def _send_relay(self, header):
        self.server.relay(HOST_ID, header)

    def _out_pressure(self):
        return max([c.pending_bytes for c in self.conns.values()] or [0])

    def _shutdown_net(self):
        self.listener.stop()
        for c in list(self.conns.values()):
            c.send_msg({"t": "bye", "reason": "host ended the session"})
            c.finish("host ended the session", notify=False)
        self.conns.clear()

    def _on_local_resize(self):
        w, h = self.doc.size()
        self.size = (w, h)
        self.replica.set_geometry(w, h, self.doc.pixel_size(), authoritative=True)
        self.server.set_geometry(w, h)
        self.log("Canvas is now %dx%d - sending it to everyone again." % (w, h))

    def _drain(self):
        while not self.closed:
            try:
                item = self.net_inbox.get_nowait()
            except queue.Empty:
                break
            try:
                self._on_net(*item)
            except Exception:  # noqa: BLE001
                self.log("Internal error handling a message:\n" + traceback.format_exc(), "error")
        while self.local_inbox and not self.closed:
            kind, header, entries = self.local_inbox.popleft()
            try:
                self._on_server(kind, header, entries)
            except Exception:  # noqa: BLE001
                self.log("Internal error:\n" + traceback.format_exc(), "error")

    def _on_net(self, kind, conn, header, payload, parsed):
        if kind == "joined":
            pid = self.next_peer
            self.next_peer += 1
            conn.peer_id = pid
            self.conns[pid] = conn
            self.server.add_peer(pid, conn.name, conn)
            self.log("%s joined." % conn.name, "join")
        elif kind == "closed":
            pid = conn.peer_id
            if pid is not None and self.conns.get(pid) is conn:
                del self.conns[pid]
                self.server.remove_peer(pid)
                self.cursors.pop(pid, None)
                self.log("%s left (%s)." % (conn.name, header.get("reason") or "disconnected"), "leave")
        elif kind == "log":
            self.log(header.get("text", ""))
        elif kind == "msg":
            pid = conn.peer_id
            if pid is None or self.conns.get(pid) is not conn:
                return
            t = header["t"]
            try:
                if t == "ops":
                    self.server.handle_ops(pid, header)
                elif t == "patch":
                    self.server.handle_patch(pid, header, parsed)
                elif t == "complete":
                    self.server.handle_complete(pid, header)
                elif t == "resync":
                    self.server.handle_resync(pid, header)
                elif t in ("undo", "redo"):
                    self.server.handle_undo(pid, header)
                elif t in ("chat", "cursor"):
                    self.server.relay(pid, header)
                elif t == "bye":
                    conn.close("left the session")
            except P.ProtocolError as e:
                conn.close("sent damaged data (%s)" % e)

    def status(self):
        return {"role": "host", "port": self.port, "code": self.code, "peers": dict(self.peers),
                "waiting": self.waiting_since is not None, "pending": self.pending_count(),
                "unsupported": self.unsupported, "undo": self.undo_state(),
                "out": sum(c.bytes_out for c in self.conns.values()),
                "in": sum(c.bytes_in for c in self.conns.values())}


class GuestSession(Client):
    role = "guest"

    def __init__(self, doc_factory, name, host, port, code, cfg=None):
        Client.__init__(self, name, cfg)
        self.doc_factory = doc_factory
        self.host_addr = (host, port)
        self.code = code
        self.conn = None
        self.inbox = queue.Queue()
        self.state = "connecting"

    def start(self):
        net.connect(self.host_addr[0], self.host_addr[1], self.code, self.name, self.inbox)
        self.log("Connecting to %s:%d ..." % self.host_addr)

    # transport: the server is across the network
    def _send(self, header):
        if self.conn is not None:
            self.conn.send_msg(header)

    def _send_ops(self, seq, ops):
        self._send({"t": "ops", "seq": seq, "ops": ops})

    def _send_patch(self, sid, pseq, entries, unit=None):
        if self.conn is not None:
            header = {"layer": sid, "pseq": pseq}
            if unit is not None:
                header["unit"] = unit
            self.conn.send(P.patch_frame(header, entries))

    def _send_undo(self, kind, unit, req):
        self._send({"t": kind, "unit": unit, "req": req})

    def _send_complete(self, sid):
        self._send({"t": "complete", "layer": sid})

    def _send_resync(self, sid):
        self._send({"t": "resync", "layer": sid})

    def _send_relay(self, header):
        self._send(header)

    def _out_pressure(self):
        return self.conn.pending_bytes if self.conn is not None else 0

    def _shutdown_net(self):
        if self.conn is not None:
            self.conn.send_msg({"t": "bye"})
            self.conn.finish("left", notify=False)

    def _on_local_resize(self):
        if not self._idle():
            return
        self.log("The canvas size is set by the host; restoring it.", "warn")
        self.doc.resize_canvas(*self.size)
        self.own_command_t = time.perf_counter()
        self._local_size = self.size
        w, h = self.size
        ps = self.replica.ps
        self.replica.w = 0  # force a reset: local pixels are no longer what we expected
        self.replica.set_geometry(w, h, ps, authoritative=False)
        for sid in list(self.replica.layers):
            self._send_resync(sid)

    def _drain(self):
        while not self.closed:
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                return
            try:
                self._on_net(*item)
            except Exception:  # noqa: BLE001
                self.log("Internal error handling a message:\n" + traceback.format_exc(), "error")

    def _on_net(self, kind, conn, header, payload, parsed):
        if kind == "connected":
            self.conn = conn
            if self.state == "connecting":
                self.state = "handshake"
        elif kind == "connect_failed":
            self.stop("Could not connect: %s" % header.get("reason"))
        elif kind == "closed":
            if conn is self.conn:
                self.stop("Disconnected from host: %s" % header.get("reason"))
        elif kind == "log":
            self.log(header.get("text", ""))
        elif kind == "msg":
            if self.conn is None:
                self.conn = conn
            t = header["t"]
            if t == "reject":
                self.stop("The host refused the connection: %s" % header.get("reason", "?"))
            elif t == "welcome":
                self._on_welcome(header)
            elif t == "bye":
                self.stop("The host ended the session.")
            elif self.live:
                self._on_server(t, header, parsed)

    def _on_welcome(self, header):
        info = header.get("doc") or {}
        try:
            w, h, ps = int(info["w"]), int(info["h"]), int(info["ps"])
            self.my_id = int(header.get("peer_id"))
        except (KeyError, TypeError, ValueError):
            self.stop("The host sent an invalid welcome.")
            return
        if not (0 < w <= 65535 and 0 < h <= 65535 and 0 < ps <= P.MAX_PIXEL_SIZE):
            self.stop("Unsupported canvas %dx%d." % (w, h))
            return
        peers = header.get("peers") or {}
        self.peers = {int(k): net.clean_name(v) for k, v in peers.items() if str(k).isdigit()}
        try:
            self.doc = self.doc_factory(info)
        except Exception as e:  # noqa: BLE001
            self.stop("Could not create the shared document: %s" % e)
            return
        if self.doc.pixel_size() != ps:
            self.stop("Krita could not create a matching %s/%s canvas." % (info.get("model"), info.get("depth")))
            return
        self.size = self._local_size = (w, h)
        self.replica.doc = self.doc
        self.replica.set_geometry(w, h, ps, authoritative=False)
        defaults = set(self.read_structure().nodes)
        tree = Tree.from_nested(header.get("tree"))
        self.reconcile(tree, drop=defaults)
        for sid in tree.paint_sids():
            self.replica.ensure(sid)
        self.expected = self.build_tree(self.read_structure())
        self.live = True
        self.state = "live"
        self.log("Joined %s's canvas (%dx%d)." % (self.peer_name(HOST_ID), w, h), "join")

    def status(self):
        return {"role": "guest", "state": self.state, "peers": dict(self.peers), "host": "%s:%d" % self.host_addr,
                "waiting": self.waiting_since is not None, "pending": self.pending_count(),
                "unsupported": self.unsupported, "undo": self.undo_state(),
                "out": self.conn.bytes_out if self.conn else 0, "in": self.conn.bytes_in if self.conn else 0}
