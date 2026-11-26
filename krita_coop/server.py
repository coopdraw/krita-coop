"""Authoritative session state, kept inside the host's Krita (pure Python).

The server never touches a Krita document.  It holds the layer tree and the
compressed content of every tile, merges incoming patches pixel by pixel in
arrival order, and broadcasts the result.  Because it is plain data it can
merge and relay immediately, even while the host is in the middle of a stroke.

The host's own Krita document is just another client (peer id 0) that talks
to this object through direct calls instead of a socket.

Per-person undo lives here too, the way Drawpile does it.  Every patch made by
an undoable action ("unit") is kept, per tile, in arrival order on top of a
base state.  A tile always equals its base with every *live* operation applied
in order (a patch sets the pixels in its mask).  Undo marks a person's unit
dead and recomputes its tiles; redo marks it live again.  So the result never
depends on who undoes first, and when everyone has undone everything the canvas
is back to its base.  Old units are frozen (no longer undoable) and folded into
the base once nothing older than them is left on a tile.
"""

import collections
import zlib

from . import pixops
from . import protocol as P
from .tree import Tree, apply_ops, valid_sid

LOCAL = 0
CHUNK = 256  # tiles per frame when sending whole layers
MAX_UNITS_PER_PEER = 100  # undo depth kept for each person
MAX_OP_BYTES = 384 << 20  # memory for undo history; beyond it the oldest units are frozen


class _Op(object):
    """One person's change to one tile within one unit: set the masked pixels to ``vals``.

    While its unit may still grow the op is kept raw (``vals`` bytes, ``mask`` big int) so
    later patches of the same unit merge into it cheaply; afterwards it is compressed."""

    __slots__ = ("pid", "unit", "vals", "mask", "vblob", "mblob", "live", "frozen", "nbytes")

    def __init__(self, pid, unit, vals, mask):
        self.pid, self.unit = pid, unit
        self.vals, self.mask = vals, mask
        self.vblob = self.mblob = None
        self.live = True
        self.frozen = unit is None
        self.nbytes = 0

    def get(self, n, npx, ps):
        if self.vals is not None:
            return self.vals, self.mask
        vals = P.inflate(self.vblob, n)
        m = pixops.full_mask(n) if self.mblob is None else pixops.pixels_to_mask(P.inflate(self.mblob, npx), ps)
        return vals, m


class _Log(object):
    __slots__ = ("base", "ops")

    def __init__(self, base):
        self.base = base  # (blob or None, crc): the tile before ops[0]
        self.ops = []


class _Layer(object):
    __slots__ = ("tiles", "complete", "logs")

    def __init__(self):
        self.tiles = {}  # (tx, ty) -> (blob or None, crc)
        self.complete = False  # True once every tile is known; missing tiles are transparent
        self.logs = {}  # (tx, ty) -> _Log, only for tiles with undoable history


class _Peer(object):
    __slots__ = ("pid", "name", "conn", "ops_ack")

    def __init__(self, pid, name, conn):
        self.pid = pid
        self.name = name
        self.conn = conn  # None for the host's own client
        self.ops_ack = 0


class Server(object):
    def __init__(self, info, deliver_local):
        self.info = dict(info)
        self.w, self.h, self.ps = int(info["w"]), int(info["h"]), int(info["ps"])
        self.tree = Tree()
        self.rev = 0
        self.layers = {}
        self.peers = {}
        self.deliver_local = deliver_local
        self.max_units = MAX_UNITS_PER_PEER
        self.max_op_bytes = MAX_OP_BYTES
        self._reset_history()

    def _reset_history(self):
        self.unit_tiles = {}  # (pid, unit) -> {(sid, key)} where its ops are
        self.units = collections.OrderedDict()  # (pid, unit) -> None, oldest first (not frozen yet)
        self.peer_units = {}  # pid -> deque of its units, oldest first
        self.frozen = set()  # (pid, unit) that can no longer be undone
        self.cur_unit = {}  # pid -> unit of its latest patch
        self.op_bytes = 0

    # ---- membership --------------------------------------------------------
    def add_local(self, name):
        self.peers[LOCAL] = _Peer(LOCAL, name, None)

    def peer_names(self):
        return {str(pid): p.name for pid, p in self.peers.items()}

    def add_peer(self, pid, name, conn):
        p = _Peer(pid, name, conn)
        self.peers[pid] = p
        conn.send_msg({"t": "welcome", "peer_id": pid, "peers": self.peer_names(), "doc": self.doc_info(),
                       "tree": self.tree.to_nested(), "rev": self.rev, "ack": 0})
        for sid in self.tree.preorder():
            if sid in self.layers:
                self._send_layer(pid, sid)
        self._emit({"t": "peers", "peers": self.peer_names()})

    def remove_peer(self, pid):
        if self.peers.pop(pid, None) is not None:
            for k in [k for k in self.units if k[0] == pid]:
                self._freeze(k)  # nobody can undo them any more
            self.peer_units.pop(pid, None)
            self._emit({"t": "peers", "peers": self.peer_names()})
            self._emit({"t": "cursor", "from": pid, "x": None, "y": None})

    def doc_info(self):
        return dict(self.info, w=self.w, h=self.h, ps=self.ps)

    # ---- output ------------------------------------------------------------------
    def _emit(self, header, entries=None, only=None, skip=None):
        frame = None
        for pid, p in list(self.peers.items()):
            if (only is not None and pid != only) or pid == skip:
                continue
            if p.conn is None:
                self.deliver_local((header["t"], header, entries))
            else:
                if frame is None:
                    frame = P.tiles_frame(header, entries) if entries is not None else P.Frame(header)
                p.conn.send(frame)

    def _broadcast_tree(self):
        self.rev += 1
        nested = self.tree.to_nested()
        for pid, p in list(self.peers.items()):
            self._emit({"t": "tree", "rev": self.rev, "ack": p.ops_ack, "tree": nested, "w": self.w, "h": self.h},
                       only=pid)

    def _send_layer(self, pid, sid):
        L = self.layers[sid]
        items = list(L.tiles.items())
        for i in range(0, len(items), CHUNK):
            entries = []
            for (tx, ty), (blob, crc) in items[i:i + CHUNK]:
                w, h, n = self._geom(tx, ty)
                entries.append((tx, ty, w, h, n, blob, crc))
            self._emit({"t": "tiles", "layer": sid, "origin": None, "oseq": None}, entries, only=pid)
        if L.complete:
            self._emit({"t": "complete", "layer": sid}, only=pid)

    # ---- geometry ------------------------------------------------------------------
    def _geom(self, tx, ty):
        w = min(P.TILE, self.w - tx * P.TILE)
        h = min(P.TILE, self.h - ty * P.TILE)
        return w, h, w * h * self.ps

    def _valid(self, tx, ty, w, h, rl):
        if tx * P.TILE >= self.w or ty * P.TILE >= self.h:
            return False
        ew, eh, n = self._geom(tx, ty)
        return (w, h, rl) == (ew, eh, n)

    def set_geometry(self, w, h):
        """Canvas size changed on the host: forget all pixels; the host republishes them."""
        self.w, self.h = int(w), int(h)
        for L in self.layers.values():
            L.tiles = {}
            L.complete = False
            L.logs = {}
        self._reset_history()
        self._broadcast_tree()

    # ---- input (from any peer, including the host's own client) ------------------
    def handle_ops(self, pid, header):
        seq = header.get("seq")
        if pid not in self.peers or not isinstance(seq, int):
            return
        res = apply_ops(self.tree, header.get("ops"))
        for sid in res.created:
            if self.tree.nodes[sid]["type"] == "paint":
                self.layers[sid] = _Layer()  # its creator now sends the pixels, then "complete"
        for sid in res.deleted:
            L = self.layers.pop(sid, None)
            if L is not None:
                self.op_bytes -= sum(op.nbytes for lg in L.logs.values() for op in lg.ops)
        self.peers[pid].ops_ack = max(self.peers[pid].ops_ack, seq)
        self._broadcast_tree()

    def handle_patch(self, pid, header, entries):
        """Merge changed pixels. Raises ProtocolError on malformed data."""
        sid, pseq = header.get("layer"), header.get("pseq")
        if pid not in self.peers or not isinstance(pseq, int):
            return
        L = self.layers.get(sid) if valid_sid(sid) else None
        if L is None:  # layer was deleted meanwhile: just acknowledge
            self._emit({"t": "ack", "oseq": pseq}, only=pid)
            return
        unit = header.get("unit")
        if not isinstance(unit, int) or isinstance(unit, bool):
            unit = None
        if unit is not None:
            self._begin_unit(pid, unit)
        out = []
        ps = self.ps
        for e in entries:
            tx, ty, w, h, n, vblob, mblob = e[:7]
            if not self._valid(tx, ty, w, h, n):
                continue
            key = (tx, ty)
            cur_state = L.tiles.get(key)
            if unit is None and key not in L.logs and mblob is None and pid == LOCAL and len(e) > 7:
                # Whole tile from the host's own client (same process): trust its checksum.
                L.tiles[key] = (vblob, e[7])
                out.append((tx, ty, w, h, n, vblob, e[7]))
                continue
            vals = P.inflate(vblob, n)
            m = pixops.full_mask(n) if mblob is None else pixops.pixels_to_mask(P.inflate(mblob, w * h), ps)
            if mblob is None:
                new, blob = vals, vblob
            else:
                cur = P.inflate(cur_state[0], n) if cur_state is not None else pixops.zero(n)
                new = pixops.select(cur, vals, m, n)
                if new is cur:
                    blob = cur_state[0] if cur_state is not None else None
                elif new is vals:
                    blob = vblob
                else:
                    blob = None if new == pixops.zero(n) else P.deflate(new)
            crc = zlib.crc32(new)
            self._log(L, sid, key, pid, unit, cur_state, vals, m, n)
            L.tiles[key] = (blob, crc)
            out.append((tx, ty, w, h, n, blob, crc))
        self._emit({"t": "tiles", "layer": sid, "origin": pid, "oseq": pseq}, out)
        if self.op_bytes > self.max_op_bytes:
            self._shed()

    def handle_complete(self, pid, header):
        sid = header.get("layer")
        L = self.layers.get(sid) if valid_sid(sid) else None
        if L is not None and not L.complete:
            L.complete = True
            self._emit({"t": "complete", "layer": sid})

    def handle_resync(self, pid, header):
        sid = header.get("layer")
        if pid in self.peers and valid_sid(sid) and sid in self.layers:
            self._send_layer(pid, sid)

    def relay(self, pid, header):
        """Chat and cursor messages: stamp the sender and pass on to everyone else."""
        t = header.get("t")
        if t == "chat":
            text = str(header.get("text", ""))[:2000]
            if text:
                self._emit({"t": "chat", "from": pid, "text": text}, skip=pid)
        elif t == "cursor":
            x, y = header.get("x"), header.get("y")
            ok = isinstance(x, (int, float)) and isinstance(y, (int, float))
            self._emit({"t": "cursor", "from": pid, "x": x if ok else None, "y": y if ok else None}, skip=pid)

    # ---- undo history ----------------------------------------------------------------
    def _begin_unit(self, pid, unit):
        k = (pid, unit)
        prev = self.cur_unit.get(pid)
        if prev != unit:
            if prev is not None:
                self._close_unit((pid, prev))  # that action is over: compress its ops
            self.cur_unit[pid] = unit
        if k not in self.units and k not in self.frozen:
            self.units[k] = None
            q = self.peer_units.setdefault(pid, collections.deque())
            q.append(unit)
            while len(q) > self.max_units:
                self._freeze((pid, q.popleft()))

    def _log(self, L, sid, key, pid, unit, prev_state, vals, m, n):
        """Remember an applied patch for undo (only on tiles that have undoable history)."""
        undoable = unit is not None and (pid, unit) in self.units
        lg = L.logs.get(key)
        if lg is None:
            if not undoable:
                return  # nothing to undo here: the new state simply is the base
            lg = L.logs[key] = _Log(prev_state if prev_state is not None else (None, pixops.zero_crc(n)))
        last = lg.ops[-1] if lg.ops else None
        if undoable and last is not None and last.vals is not None and last.pid == pid and last.unit == unit \
                and last.live:
            last.vals = pixops.select(last.vals, vals, m, n)  # same action, nothing in between: merge
            last.mask |= m
            return
        if last is not None and last.vals is not None:
            self._compress(last, key)
        op = _Op(pid, unit if undoable else None, vals, m)
        op.nbytes = 2 * n
        self.op_bytes += op.nbytes
        lg.ops.append(op)
        if undoable:
            self.unit_tiles.setdefault((pid, unit), set()).add((sid, key))
        else:
            self._compress(op, key)
            self._compact(L, key)

    def _compress(self, op, key):
        if op.vals is None:
            return
        w, h, n = self._geom(*key)
        op.vblob = None if op.vals == pixops.zero(n) else P.deflate(op.vals)
        op.mblob = None if op.mask == pixops.full_mask(n) else P.deflate(pixops.mask_to_pixels(op.mask, n, self.ps))
        op.vals = op.mask = None
        old = op.nbytes
        op.nbytes = len(op.vblob or b"") + len(op.mblob or b"") + 64
        self.op_bytes += op.nbytes - old

    def _close_unit(self, k):
        """The unit's action is over: compress its operations (they no longer grow)."""
        for sid, key in self.unit_tiles.get(k, ()):
            L = self.layers.get(sid)
            lg = L.logs.get(key) if L is not None else None
            if lg is not None:
                for op in lg.ops:
                    if op.vals is not None and (op.pid, op.unit) == k:
                        self._compress(op, key)

    def _recompute(self, lg, key):
        w, h, n = self._geom(*key)
        state = P.inflate(lg.base[0], n)
        for op in lg.ops:
            if op.live:
                vals, m = op.get(n, w * h, self.ps)
                state = pixops.select(state, vals, m, n)
        return state

    def _compact(self, L, key):
        """Fold frozen operations at the start of a tile's history into its base."""
        lg = L.logs.get(key)
        if lg is None or not lg.ops or not lg.ops[0].frozen:
            return
        w, h, n = self._geom(*key)
        state = None
        while lg.ops and lg.ops[0].frozen:
            op = lg.ops.pop(0)
            if op.live:
                if state is None:
                    state = P.inflate(lg.base[0], n)
                vals, m = op.get(n, w * h, self.ps)
                state = pixops.select(state, vals, m, n)
            self.op_bytes -= op.nbytes
        if state is not None:
            lg.base = (None if state == pixops.zero(n) else P.deflate(state), zlib.crc32(state))
        if not lg.ops:
            del L.logs[key]

    def _freeze(self, k):
        """Unit ``k`` can no longer be undone or redone."""
        self.units.pop(k, None)
        self.frozen.add(k)
        for sid, key in self.unit_tiles.pop(k, ()):
            L = self.layers.get(sid)
            lg = L.logs.get(key) if L is not None else None
            if lg is None:
                continue
            for op in lg.ops:
                if (op.pid, op.unit) == k:
                    self._compress(op, key)
                    op.frozen = True
            self._compact(L, key)

    def _shed(self):
        while self.op_bytes > self.max_op_bytes and self.units:
            self._freeze(next(iter(self.units)))

    def handle_undo(self, pid, header):
        """``undo`` / ``redo`` of one of ``pid``'s units: switch its operations off / on and
        recompute the tiles they touch.  Answers the requester with ``undone``."""
        kind = header.get("t")
        unit = header.get("unit")
        if pid not in self.peers or kind not in ("undo", "redo"):
            return
        k = (pid, unit)
        found = k in self.units
        tiles = self.unit_tiles.get(k) if found else None
        live = kind == "redo"
        changed = 0
        out = {}
        for sid, key in sorted(tiles or (), key=lambda t: (t[0], t[1][1], t[1][0])):
            L = self.layers.get(sid)
            lg = L.logs.get(key) if L is not None else None
            if lg is None:
                continue
            hit = False
            for op in lg.ops:
                if (op.pid, op.unit) == k and op.live != live:
                    op.live = live
                    hit = True
            if not hit:
                continue
            new = self._recompute(lg, key)
            crc = zlib.crc32(new)
            w, h, n = self._geom(*key)
            cur = L.tiles.get(key)
            if (cur[1] if cur is not None else pixops.zero_crc(n)) == crc:
                continue  # nothing visible changed here (painted over since)
            blob = None if new == pixops.zero(n) else P.deflate(new)
            L.tiles[key] = (blob, crc)
            out.setdefault(sid, []).append((key[0], key[1], w, h, n, blob, crc))
            changed += 1
        for sid, entries in out.items():
            for i in range(0, len(entries), CHUNK):
                self._emit({"t": "tiles", "layer": sid, "origin": None, "oseq": None}, entries[i:i + CHUNK])
        self._emit({"t": "undone", "kind": kind, "unit": unit, "req": header.get("req"),
                    "found": found, "changed": changed, "layers": sorted(out)}, only=pid)
