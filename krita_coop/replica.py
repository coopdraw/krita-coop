"""Client-side pixel replica and change scanner.

For every tile of every shared layer a client tracks:

* ``srv``     - the host's latest content for the tile (compressed, with CRC)
* ``unacked`` - patches this client sent that the host has not confirmed yet
* ``exp``     - what the local layer is *expected* to contain, i.e. the host's
                content as last written locally plus everything we sent since.
                Only stored where it differs from ``srv``.

Local edits are exactly the pixels where the Krita layer differs from ``exp``.
They are sent as patches (changed pixels + mask).  When the host's content
for a tile changes, the local tile becomes ``srv + unacked + unsent edits``,
so nobody's work is lost when two people paint the same area at once: only
pixels that both changed go to whichever change reached the host last.

Per-person undo (``undo.py``, ``server.py``): every patch for a local edit
carries the id of the action ("unit") it belongs to, so the host can switch the
whole action off and on again later.  ``arm``/``seal_now`` re-read whole layers so
an action's last pixels are sent (and attributed to it) before its unit is closed.
History calls are guarded: a bug there switches undo off for the session but
never disturbs the replica itself.
"""

import time
import traceback
import zlib

from . import pixops
from . import protocol as P

TILE = P.TILE
UNKNOWN = "unknown"  # tile state sentinel


class RLayer(object):
    __slots__ = ("sid", "srv", "exp", "unacked", "complete", "authoritative", "default_exp",
                 "strip_crc", "cursor", "force_left", "pending", "seal_left", "hot", "touched", "busy_n")

    def __init__(self, sid, nty, authoritative, default_exp):
        self.sid = sid
        self.srv = {}
        self.exp = {}
        self.unacked = {}  # key -> [(pseq, value_blob, mask_blob)]
        self.complete = False
        self.authoritative = authoritative  # our local pixels are the truth for unknown tiles
        self.default_exp = default_exp  # expected local content of tiles we know nothing about
        self.strip_crc = [None] * nty
        self.cursor = 0
        self.force_left = nty if authoritative else 0
        self.pending = set()  # tiles whose host content changed and must be written locally
        self.seal_left = 0  # strips still to re-read before this layer counts as sealed
        self.hot = set()  # strips Krita reported as just changed (co-op build): read these first
        self.touched = set()  # tiles changed locally since the running stroke/tool action began
        self.busy_n = 0  # reads in a row that came back "busy" (a stroke being merged)


class Replica(object):
    def __init__(self, send_patch, send_complete, request_resync, resolve):
        self.send_patch = send_patch  # (sid, pseq, entries, unit=None)
        self.send_complete = send_complete  # (sid)
        self.request_resync = request_resync  # (sid)
        self.resolve = resolve  # sid -> local uuid or None
        self.doc = None
        self.layers = {}
        self.w = self.h = self.ps = 0
        self.ntx = self.nty = 0
        self.pseq = 0
        self.unacked_keys = set()
        self.last_change = 0.0
        self._rr = 0
        self.hist = None  # undo.History, set by the session
        self.strip_cap = None  # tests: at most this many strips per scan()/seal_now() call

    # ---- undo history hooks (guarded) ------------------------------------------------
    def _h(self, name, *args):
        h = self.hist
        if h is None or h.broken:
            return None
        try:
            return getattr(h, name)(*args)
        except Exception:  # noqa: BLE001 - never let undo bookkeeping break syncing
            h.fail(traceback.format_exc())
            return None

    # ---- geometry --------------------------------------------------------------------
    def _new_layer(self, sid, authoritative, default_exp):
        return RLayer(sid, self.nty, authoritative, default_exp)

    def set_geometry(self, w, h, ps, authoritative=False):
        """Adopt a canvas size; forgets all pixel state when it changes."""
        if (w, h, ps) == (self.w, self.h, self.ps):
            return False
        self.w, self.h, self.ps = w, h, ps
        self.ntx = (w + TILE - 1) // TILE
        self.nty = (h + TILE - 1) // TILE
        self.unacked_keys = set()
        for sid in list(self.layers):
            self.layers[sid] = self._new_layer(sid, authoritative, UNKNOWN)
        self._h("reset", "size")
        return True

    def geom(self, tx, ty):
        w = min(TILE, self.w - tx * TILE)
        h = min(TILE, self.h - ty * TILE)
        return w, h, w * h * self.ps

    def _valid(self, tx, ty, w, h, n):
        return tx < self.ntx and ty < self.nty and (w, h, n) == self.geom(tx, ty)

    def _zero_state(self, n):
        return (None, pixops.zero_crc(n))

    def srv_of(self, L, key, n):
        s = L.srv.get(key)
        if s is not None:
            return s
        return self._zero_state(n) if L.complete else UNKNOWN

    def exp_of(self, L, key, n):
        e = L.exp.get(key)
        if e is not None:
            return e
        s = L.srv.get(key)
        if s is not None:
            return s
        if L.complete:
            return self._zero_state(n)
        return self._zero_state(n) if L.default_exp is None else UNKNOWN

    # ---- layers ----------------------------------------------------------------------
    def add_local(self, sid):
        """A layer this client created (or the host's existing layers): publish all of it."""
        self.layers[sid] = self._new_layer(sid, True, UNKNOWN)

    def ensure(self, sid):
        """A layer created elsewhere; locally it starts out empty."""
        L = self.layers.get(sid)
        if L is None:
            L = self.layers[sid] = self._new_layer(sid, False, None)
        return L

    def drop(self, sid):
        L = self.layers.pop(sid, None)
        if L is not None:
            for key in L.unacked:
                self.unacked_keys.discard((sid, key))

    def busy(self):
        return any(L.force_left for L in self.layers.values())

    # ---- change hints from Krita (co-op build) -----------------------------------------
    def hint(self, sid, x, y, w, h):
        """Krita says this part of the layer has just changed: read it before anything else."""
        L = self.layers.get(sid)
        if L is None or not self.nty:
            return
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        if x0 >= x1 or y0 >= y1:
            return
        tys = range(y0 // TILE, (y1 - 1) // TILE + 1)
        L.hot.update(tys)
        L.touched.update((tx, ty) for ty in tys for tx in range(x0 // TILE, (x1 - 1) // TILE + 1))

    def clear_touched(self):
        """No stroke or tool action is running any more."""
        for L in self.layers.values():
            if L.touched:
                L.touched = set()

    def has_pending(self):
        return any(L.pending for L in self.layers.values())

    # ---- messages from the host ----------------------------------------------------------
    def on_ack(self, oseq):
        if not isinstance(oseq, int):
            return
        for sid, key in list(self.unacked_keys):
            L = self.layers.get(sid)
            lst = L.unacked.get(key) if L is not None else None
            if lst is None:
                self.unacked_keys.discard((sid, key))
                continue
            lst = [p for p in lst if p[0] > oseq]
            if lst:
                L.unacked[key] = lst
            else:
                del L.unacked[key]
                self.unacked_keys.discard((sid, key))

    def on_tiles(self, header, entries, my_id):
        sid = header.get("layer")
        mine = header.get("origin") == my_id
        if mine:
            self.on_ack(header.get("oseq"))
        if not isinstance(sid, str) or not entries:
            return
        L = self.ensure(sid)
        for tx, ty, w, h, n, blob, crc in entries:
            if not self._valid(tx, ty, w, h, n):
                continue
            key = (tx, ty)
            e = L.exp.get(key)
            if mine and key not in L.unacked and key not in L.pending and e is not None \
                    and e is not UNKNOWN and e[1] == crc:
                # The host kept exactly what we sent: nothing to write locally.
                L.srv[key] = (blob, crc)
                del L.exp[key]
                continue
            if e is None:
                L.exp[key] = self.exp_of(L, key, n)  # what the local tile holds right now
            L.srv[key] = (blob, crc)
            L.pending.add(key)

    def on_complete(self, sid):
        if not isinstance(sid, str):
            return
        L = self.ensure(sid)
        L.complete = True
        if L.default_exp is UNKNOWN and not L.authoritative:
            # Our local pixels predate a reset (e.g. the canvas was resized): every tile the
            # host did not send is transparent on its side, so make it transparent here too.
            for ty in range(self.nty):
                for tx in range(self.ntx):
                    key = (tx, ty)
                    if key not in L.srv:
                        L.srv[key] = self._zero_state(self.geom(tx, ty)[2])
                        L.exp.setdefault(key, UNKNOWN)
                        L.pending.add(key)
            L.default_exp = None

    # ---- applying the host's content locally (image idle) ---------------------------------
    def apply(self, budget, active_sid=None, live=None, hold=()):
        """Write changed host tiles into the document. Returns True if anything was written.

        ``live`` is None when no stroke is running.  The co-op build can also write while one is
        (the caller holds it paused); what is being worked on has to wait all the same:

        * ``"paint"`` - a brush stroke: only the tiles under a Wash stroke in progress wait (the
          layer itself doesn't contain that stroke yet; writing there would bake it in twice);
        * ``"tool"`` - any other action (Transform, Move, a filter...): the layers it has touched,
          and the layers in ``hold`` (the selected ones: what such a tool works on), wait
          entirely, because such tools replace the layer with their own result at the end.
        """
        deadline = time.perf_counter() + budget
        wrote = False
        order = sorted(self.layers.values(), key=lambda L: L.sid != active_sid)
        for L in order:
            if not L.pending:
                continue
            u = self.resolve(L.sid)
            if u is None:
                continue  # its layer is not in the local document yet
            overlay = False
            if live is not None:
                if live == "tool" and (L.touched or L.sid in hold):
                    continue
                overlay = self.doc.overlay_active(u)
            for key in list(L.pending):
                if overlay and key in L.touched:
                    continue
                if time.perf_counter() >= deadline:
                    return wrote
                L.pending.discard(key)
                r = self._apply_key(L, u, key, overlay)
                if r is None:
                    L.pending.add(key)  # under the stroke being painted: later
                elif r:
                    wrote = True
        return wrote

    def _apply_key(self, L, u, key, overlay=False):
        """Make one local tile ``srv + unacked + unsent local edits``. Returns True if it wrote,
        None if the tile has to wait (``overlay``: a Wash stroke is being painted over the layer)."""
        tx, ty = key
        w, h, n = self.geom(tx, ty)
        s = L.srv.get(key)
        if s is None:
            return False
        try:
            base = P.inflate(s[0], n)
        except P.ProtocolError:
            base = None
        if base is None or zlib.crc32(base) != s[1]:
            L.srv.pop(key, None)
            self.request_resync(L.sid)
            return False
        for _, vblob, mblob in L.unacked.get(key, ()):
            vals = P.inflate(vblob, n)
            m = pixops.full_mask(n) if mblob is None else pixops.pixels_to_mask(P.inflate(mblob, w * h), self.ps)
            base = pixops.select(base, vals, m, n)
        x, y = tx * TILE, ty * TILE
        local = self.doc.read_tile(u, x, y, w, h)
        if local is None or len(local) != n:
            return False
        if overlay:
            shown = self.doc.read_tile_shown(u, x, y, w, h)
            if shown is None or shown != local:
                return None
        e = L.exp.get(key, UNKNOWN)
        if e is UNKNOWN or zlib.crc32(local) == e[1]:
            target = base
        else:  # keep local edits that have not been sent yet
            unsent = pixops.diff_mask(P.inflate(e[0], n), local, self.ps)
            target = pixops.select(base, local, unsent, n)
        wrote = False
        if target != local:
            self.doc.write_tile(u, x, y, w, h, target)
            L.strip_crc[ty] = None
            wrote = True
        if key in L.unacked:
            L.exp[key] = (None if base == pixops.zero(n) else P.deflate(base), zlib.crc32(base))
        else:
            L.exp.pop(key, None)
        return wrote

    # ---- sealing: re-read whole layers so an action is completely recorded ----------------------
    def arm(self, sids):
        for sid in sids:
            L = self.layers.get(sid)
            if L is not None and self.nty:
                L.seal_left = self.nty

    def sealing(self):
        return any(L.seal_left for L in self.layers.values())

    def seal_now(self, deadline, active_sid=None):
        """Re-read armed layers (the selected one first) until done or ``deadline``."""
        n = 0
        for L in sorted((L for L in self.layers.values() if L.seal_left), key=lambda L: L.sid != active_sid):
            while L.seal_left:
                if time.perf_counter() >= deadline or (self.strip_cap is not None and n >= self.strip_cap):
                    return False
                if self._scan_strip(L) is None:
                    return False  # the layer is busy (a stroke being merged): next tick
                n += 1
        return not self.sealing()

    # ---- scanning for local edits ------------------------------------------------------------
    def scan(self, budget, active_sid=None, seal_budget=None, background=None):
        """Scan for local edits for about ``budget`` seconds.  Layers being sealed (an action
        that just ended, an undo waiting) go first and may use up to ``seal_budget``.  Then the
        strips Krita reported as changed (co-op build), then everything else in turn; with
        ``background`` set, that last part gets only so many seconds (it is just a safety net
        when Krita tells us what changed)."""
        if not self.nty or not self.layers:
            return 0
        start = time.perf_counter()
        deadline = start + budget
        seal_deadline = start + max(budget, seal_budget or 0.0)
        cap = self.strip_cap
        n = 0
        sent = 0
        sealing = sorted((L for L in self.layers.values() if L.seal_left), key=lambda L: L.sid != active_sid)
        for L in sealing:
            while L.seal_left:
                if time.perf_counter() >= seal_deadline or (cap is not None and n >= cap):
                    return sent
                r = self._scan_strip(L)
                if r is None:
                    break  # busy (a stroke being merged): try again next tick
                sent += r
                n += 1
        for L in sorted((L for L in self.layers.values() if L.hot), key=lambda L: L.sid != active_sid):
            u = self.resolve(L.sid)
            if u is None:
                L.hot = set()
                continue
            for ty in sorted(L.hot):
                if time.perf_counter() >= deadline or (cap is not None and n >= cap):
                    return sent
                if ty >= self.nty:
                    L.hot.discard(ty)
                    continue
                r = self._scan_row(L, u, ty, False)
                if r is None:
                    break
                L.hot.discard(ty)
                sent += max(r, 0)
                n += 1
        if background is not None:
            deadline = min(deadline, time.perf_counter() + background)
        first = self.layers.get(active_sid)
        others = [L for L in self.layers.values() if L is not first]
        if others:
            self._rr = (self._rr + 1) % len(others)
            others = others[self._rr:] + others[:self._rr]
        if first is not None:
            share = deadline if not others else min(deadline, start + budget * 0.7)
            for _ in range(self.nty):
                if time.perf_counter() >= share or (cap is not None and n >= cap):
                    break
                r = self._scan_strip(first)
                if r is None:
                    break
                sent += r
                n += 1
        for L in others:
            for _ in range(self.nty):
                if time.perf_counter() >= deadline or (cap is not None and n >= cap):
                    return sent
                r = self._scan_strip(L)
                if r is None:
                    break
                sent += r
                n += 1
        return sent

    def _scan_strip(self, L):
        """Read the layer's next strip.  Returns the number of tiles sent, or None if the layer
        can't be read right now (nothing is advanced then)."""
        u = self.resolve(L.sid)
        if u is None:
            L.seal_left = 0
            return 0
        ty = L.cursor
        forced = L.force_left > 0
        sent = self._scan_row(L, u, ty, forced)
        if sent is None:
            return None
        L.cursor = (ty + 1) % self.nty
        L.hot.discard(ty)
        if L.seal_left:
            L.seal_left -= 1
        if sent < 0:
            return 0  # unreadable (its node is gone?): move on, but it doesn't count as published
        if forced:
            L.force_left -= 1
            if L.force_left == 0 and L.authoritative:
                L.authoritative = False
                L.complete = True
                self.send_complete(L.sid)
        return sent

    def _scan_row(self, L, u, ty, forced):
        y = ty * TILE
        h = min(TILE, self.h - y)
        buf = self.doc.read_rows(u, y, h)
        if buf is not None and len(buf) == 0 and L.busy_n < 400:
            L.busy_n += 1  # the co-op build answers "busy" while a stroke is merged into the layer
            return None
        L.busy_n = 0
        if buf is None or len(buf) != self.w * h * self.ps:
            return -1
        c = zlib.crc32(buf)
        if forced or c != L.strip_crc[ty]:
            sent = self._diff_strip(L, ty, h, buf)
            L.strip_crc[ty] = c
        else:
            sent = 0
        return sent

    def _diff_strip(self, L, ty, h, buf):
        mv = memoryview(buf)
        ps = self.ps
        rowb = self.w * ps
        edits = []  # local edits on known content: undoable, tagged with the action's unit
        pubs = []  # content published without a known "before" (a layer being shared): not undoable
        for tx in range(self.ntx):
            x = tx * TILE
            w = min(TILE, self.w - x)
            s = x * ps
            e = s + w * ps
            if w == self.w:
                tile = bytes(mv[:rowb * h])
            elif h == 1:
                tile = bytes(mv[s:e])
            else:
                tile = b"".join([mv[r * rowb + s:r * rowb + e] for r in range(h)])
            n = len(tile)
            key = (tx, ty)
            lc = zlib.crc32(tile)
            ex = self.exp_of(L, key, n)
            if ex is UNKNOWN:
                if not L.authoritative:
                    continue  # we don't know the host's version yet; never overwrite it blindly
                if tile == pixops.zero(n):
                    L.exp[key] = (None, lc)  # transparent tiles need not be sent for new layers
                    continue
                pubs.append((tx, ty, w, h, n, P.deflate(tile), None, lc))
                continue
            elif lc == ex[1]:
                continue
            else:
                m = pixops.diff_mask(P.inflate(ex[0], n), tile, ps)
                if m == 0:
                    L.exp[key] = (ex[0], lc)
                    continue
                vblob = None if tile == pixops.zero(n) else P.deflate(tile)
                mblob = None if m == pixops.full_mask(n) else P.deflate(pixops.mask_to_pixels(m, n, ps))
                edits.append((tx, ty, w, h, n, vblob, mblob, lc))
        if pubs:
            self._send(L, pubs, None)
            self._h("local_unrecorded", L, self)
        if edits:
            self._send(L, edits, self._h("unit_for_edit", L, self))
        return len(pubs) + len(edits)

    def _send(self, L, entries, unit):
        self.pseq += 1
        for tx, ty, w, h, n, vblob, mblob, lc in entries:
            key = (tx, ty)
            L.exp[key] = (vblob, lc)
            L.unacked.setdefault(key, []).append((self.pseq, vblob, mblob))
            self.unacked_keys.add((L.sid, key))
        self.send_patch(L.sid, self.pseq, entries, unit)
        self.last_change = time.perf_counter()
