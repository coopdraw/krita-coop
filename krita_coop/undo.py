"""Per-person undo for co-op sessions: the client side (pure Python, no Krita or Qt).

Krita's own undo can't be used on a shared document.  Each layer keeps one LIFO of
changes, and the pixels written for other people sit in it too, so Ctrl+Z would
take back whatever changed last on that layer, whoever made it.  During a session
undo works like in Drawpile instead:

* **Actions.**  Every patch the scanner sends for a local edit carries the id of
  the *unit* (one action: a stroke, a fill, a transform...) it belongs to.  A unit
  is closed once Krita is idle again and every layer the action touched has been
  re-read completely (``Tracker`` + ``Replica.arm``), so the last pixels of a stroke
  never land in the next unit.  A new action that starts before that simply joins
  the unit: coarser, never wrong.
* **The host keeps the history** (``server.py``): per tile, every unit's changes in
  arrival order on top of a base.  Undo switches a unit off and the host recomputes
  the tiles it touched from the base and all units still on; redo switches it back
  on.  The result never depends on who undoes first, and everyone undoing
  everything gives back the starting canvas.
* **This module** decides what Ctrl+Z means (your newest unit), makes sure your last
  action has been fully sent first, asks the host, and reports the answer.  Things
  that can't be undone during a session (layer changes, selections and other
  non-pixel commands Krita reports) become *barriers*: Ctrl+Z stops there with a
  message instead of silently undoing an older stroke.
"""

import collections
import time

SEALED, ACTIVE, TAIL = "sealed", "active", "tail"

LAYER_TEXT = ("Layer changes (add, delete, move, rename, opacity...) can't be undone during a session. "
              "Press undo again to undo your painting before it.")
FLAG_TEXT = {
    "publishing": "That change was made while the layer was still being shared at the start, so it can't be undone.",
    "layers": "That change also deleted layers, so it can't be undone during a session.",
}
TOOL_HINT = ("Undo skipped: Krita is still busy with a tool action. Finish or cancel it first "
             "(Transform/Move: Enter or Esc), then undo again.")


class Unit(object):
    __slots__ = ("uid", "kind", "sids", "dead", "flag", "text", "pushes", "epoch")

    def __init__(self, uid, kind="paint", text=None, epoch=0):
        self.uid = uid  # the id the host knows this action by (None for barriers)
        self.kind = kind  # "paint" or "barrier"
        self.sids = set()  # layers it changed
        self.dead = False
        self.flag = None  # reason this unit can't be undone (see FLAG_TEXT)
        self.text = text  # barrier message
        self.pushes = 0  # Krita undo commands attributed to this action
        self.epoch = epoch


class Request(object):
    __slots__ = ("kind", "t", "inputs", "merged", "skips", "flushed")

    def __init__(self, kind, t, inputs, merged, skips=0):
        self.kind = kind
        self.t = t
        self.inputs = inputs  # canvas presses seen when the key was pressed
        self.merged = merged
        self.skips = skips
        self.flushed = False


class Tracker(object):
    """Decides when a local action is over (its unit can be closed).

    SEALED: nothing in progress (the image is not even probed).  ACTIVE: an action is
    (or may be) running.  TAIL: Krita is idle again; waiting for the touched layers to be
    re-read completely (sealed) before the unit is cut.
    """

    def __init__(self):
        self.state = SEALED
        self.idle_now = None  # this tick's probe result (None: not probed)
        self.layers = set()  # layers that were selected while busy
        self.merged = 0  # times a new action started before the previous one was sealed
        self.records = 0
        self.last_record = 0.0

    def begin_tick(self):
        self.idle_now = None

    def on_probe(self, idle, active_sid, hist):
        """Returns True on the busy -> idle edge."""
        self.idle_now = idle
        if not idle:
            if self.state == TAIL and hist.cur is not None:
                self.merged += 1
            if self.state != ACTIVE:
                self.state = ACTIVE
                self.layers = set()
            if active_sid:
                self.layers.add(active_sid)
            return False
        if self.state == ACTIVE:
            self.state = TAIL
            return True
        return False

    def on_record(self, L, rep, now):
        self.records += 1
        self.last_record = now
        if self.idle_now:  # confirmed idle this tick: these pixels are final
            self.state = TAIL
            if not L.seal_left:
                L.seal_left = rep.nty  # straggler: make sure the whole layer is seen
        elif self.state == SEALED:
            self.state = ACTIVE  # not probed: assume the action may still be running
            self.layers = set()
        self.layers.add(L.sid)

    def settle(self, hist, rep):
        if rep.sealing():
            return
        hist.suspended = False
        if self.state == TAIL:
            hist.cut()
            self.state = SEALED
            self.layers = set()


class History(object):
    def __init__(self, cfg, log, name_of):
        self.cfg = cfg
        self.log = log
        self.name_of = name_of
        self.tracker = Tracker()
        self.undo = []
        self.redo = []
        self.cur = None  # the open unit (also the top of ``undo``, or just below a barrier)
        self.created = 0
        self.next_uid = 1
        self.broken = False
        self.suspended = False  # re-reading layers after a frame switch: edits are not undoable
        self.pending_actions = []  # Krita commands not yet matched to pixel changes
        self.last_cut = None
        self.last_cut_t = 0.0
        self.epoch = 0  # bumped by every reset
        self._said = set()

    # ---- failure ------------------------------------------------------------------
    def fail(self, tb):
        if not self.broken:
            self.broken = True
            self.log("Undo was switched off for this session after an internal error:\n" + tb, "error")
        self._clear()

    # ---- recording ----------------------------------------------------------------
    def unit_for_edit(self, L, rep):
        """The scanner is about to send local edits on ``L``: the id of the action they belong to
        (None: not undoable)."""
        if self.suspended:
            return None
        self.tracker.on_record(L, rep, time.perf_counter())
        if self.cur is None:
            self._open()
        if L.force_left:
            self.cur.flag = self.cur.flag or "publishing"
        self.cur.sids.add(L.sid)
        return self.cur.uid

    def local_unrecorded(self, L, rep):
        """The scanner published content it has no "before" for (a layer still being shared)."""
        if self.suspended:
            return
        if self.cur is None and self.tracker.state != SEALED:
            self._open()
        if self.cur is not None:
            self.cur.flag = self.cur.flag or "publishing"

    def _open(self):
        self.clear_redo()
        u = Unit(self.next_uid, epoch=self.epoch)
        self.next_uid += 1
        u.pushes = len(self.pending_actions)  # commands Krita reported just before: this action
        self.pending_actions = []
        self.undo.append(u)
        self.cur = u
        self.created += 1
        self._trim()

    # ---- unit boundaries ------------------------------------------------------------
    def cut(self):
        now = time.perf_counter()
        if self.cur is not None:
            self.cur.pushes += len(self.pending_actions)
            self.pending_actions = []
            self.last_cut, self.last_cut_t = self.cur, now
            self.cur = None
        elif self.pending_actions:
            text = self.pending_actions[-1]
            self.pending_actions = []
            self._barrier(None, "Your last change (%s) can't be undone during a session. "
                                "Press undo again to undo your painting before it." % text)

    def note_native_action(self, text, now):
        """Krita reported a new undo command on the shared document (a selection, a stroke...)."""
        if self.suspended:
            return False
        if self.cur is not None:
            self.cur.pushes += 1  # part of the running action
            return False
        if self.tracker.state == ACTIVE:
            self.pending_actions.append(text)
            return False
        lc = self.last_cut
        if lc is not None and not lc.dead and lc.pushes == 0 and now - self.last_cut_t < 1.5:
            lc.pushes += 1  # the command of the action we just closed, reported late
            return False
        self.pending_actions.append(text)
        return True  # caller must arm a check: did it change any pixels?

    def layer_ops(self, deletes, phase):
        """You changed the layer structure (seen by the session's tree diff)."""
        if self.suspended:
            return
        self.clear_redo()
        in_action = self.cur is not None and phase in (ACTIVE, "edge")
        if deletes:
            # Undoing the pixels of an action that also deleted layers (Merge Down, Cut...) would
            # not bring the deleted layers back, so such an action is refused as a whole.  Pixel
            # changes still to be sent for it join this unit.
            if not in_action:
                self._open()
                if self.tracker.state == SEALED:
                    self.tracker.state = ACTIVE
            self.cur.flag = self.cur.flag or "layers"
        elif in_action:
            self._barrier(self.cur, LAYER_TEXT)  # most likely came before the painting (e.g. paste)
        else:
            self._barrier(None, LAYER_TEXT)

    def _barrier(self, below, text):
        self.clear_redo()
        idx = self.undo.index(below) if below is not None and below in self.undo else len(self.undo)
        prev = self.undo[idx - 1] if idx > 0 else None
        if prev is not None and prev.kind == "barrier":
            return  # one barrier is enough
        self.undo.insert(idx, Unit(None, "barrier", text, self.epoch))
        self._trim()

    def clear_redo(self):
        for u in self.redo:
            u.dead = True
        self.redo = []

    # ---- stacks ---------------------------------------------------------------------
    def stack(self, kind):
        return self.undo if kind == "undo" else self.redo

    def top(self, kind):
        st = self.stack(kind)
        return st[-1] if st else None

    def discard(self, u):
        for st in (self.undo, self.redo):
            if u in st:
                st.remove(u)
        u.dead = True

    def counts(self):
        return len(self.undo), len(self.redo)

    def _trim(self):
        evicted = False
        while len(self.undo) + len(self.redo) > self.cfg.undo_max_units:
            victim = None
            for st in (self.undo, self.redo):
                for u in st[:-1]:  # never the top of a stack
                    if u is not self.cur:
                        victim = u
                        break
                if victim is not None:
                    break
            if victim is None:
                break
            self.discard(victim)
            evicted = True
        if evicted:
            self.say_once("trim", "Your oldest undo steps were dropped (limit of %d)." % self.cfg.undo_max_units,
                          "undo")

    # ---- invalidation ---------------------------------------------------------------
    def reset(self, reason):
        had = bool(self.undo or self.redo)
        self._clear()
        if had and reason:
            self.log({"size": "Your undo history was cleared because the canvas size changed.",
                      "frame": "Your undo history was cleared because the animation frame changed."}
                     .get(reason, "Your undo history was cleared."), "undo")

    def _clear(self):
        for u in self.undo + self.redo:
            u.dead = True
        if self.cur is not None:
            self.cur.dead = True
        self.epoch += 1
        self.undo, self.redo, self.cur = [], [], None
        self.pending_actions = []
        self.last_cut = None
        t = self.tracker
        t.state, t.layers = SEALED, set()

    def say_once(self, key, text, kind="undo"):
        if key not in self._said:
            self._said.add(key)
            self.log(text, kind)


class UndoEngine(object):
    """Ctrl+Z / Ctrl+Shift+Z requests for one Client: closes your last action, asks the host
    to undo/redo it, and reports the answer.

    Uses from the client: ``cfg``, ``log``, ``replica``, ``doc``, ``sid_of``, ``_idle()``,
    ``_detect()``, ``_send_undo()``, ``layer_name()``, ``own_command_t``.
    """

    def __init__(self, client):
        self.c = client
        self.cfg = client.cfg
        self.hist = History(client.cfg, client.log, client.layer_name)
        self.tracker = self.hist.tracker
        self.queue = collections.deque()
        self.pending = None  # the request the host has not answered yet
        self._next_req = 1
        self.inputs = 0  # pointer presses on the shared canvas (from the GUI, while a request waits)
        self.native_notes = 0  # commands Krita reported on the shared document
        self.flush_mark = None
        self.flush_t = -1e9
        self._flushed_now = False
        self._epoch = self.hist.epoch
        self._nothing_t = -1e9

    # ---- input from the UI ------------------------------------------------------------
    def request(self, kind):
        h = self.hist
        if h.broken:
            h.say_once("broken", "Undo is off for the rest of this session (see the error above).", "undo-warn")
            return False
        if len(self.queue) >= self.cfg.undo_queue_max:
            return False
        self.queue.append(Request(kind, time.perf_counter(), self.inputs, self.tracker.merged))
        return True

    def note_input(self):
        self.inputs += 1

    def note_native_action(self, text):
        c = self.c
        now = time.perf_counter()
        self.native_notes += 1  # whatever it was, the next undo must re-read the layers first
        if self.hist.broken or now - c.own_command_t < 1.0:
            return  # e.g. a blending-mode change we applied for someone else
        if self.hist.note_native_action(text, now) and self.tracker.state == SEALED:
            self.tracker.state = TAIL  # settle() turns it into a barrier unless pixels show up
            self.tracker.layers = set()
            c.replica.arm(self._action_layers())

    def layer_ops(self, deletes, phase):
        h, t, r = self.hist, self.tracker, self.c.replica
        if h.broken:
            return
        if phase == TAIL and h.cur is not None:
            # A new action (the layer change) while the previous one is still being closed:
            # finish reading its pixels first, so none of them end up in the wrong unit.
            a = self._active_sid()
            while r.sealing():
                r.seal_now(time.perf_counter() + 1.0, a)
            t.settle(h, r)
            phase = t.state
        h.layer_ops(deletes, phase)

    def reset(self, reason):
        self.queue.clear()
        self.pending = None
        self.hist.reset(reason)

    def rebaseline(self):
        """The animation frame changed: every layer reads differently now, and that is not an edit."""
        self.reset("frame")
        self.hist.suspended = True
        self.c.replica.arm(list(self.c.replica.layers))

    # ---- from the host ------------------------------------------------------------------
    def on_undone(self, header):
        p = self.pending
        if p is None or header.get("req") != p["req"]:
            return
        self.pending = None
        c, h, u, kind = self.c, self.hist, p["unit"], p["kind"]
        if not header.get("found"):
            h.discard(u)
            c.log("That change is too old to %s (the host no longer keeps it)." % kind, "undo-warn")
            return
        changed = header.get("changed")
        if not changed:
            if p["skips"] < self.cfg.undo_max_skips:
                c.log("Skipped one of your changes: others have painted over all of it.", "undo")
                self.queue.appendleft(Request(kind, time.perf_counter(), self.inputs, self.tracker.merged,
                                              p["skips"] + 1))
            else:
                c.log("Stopped: your last changes were all painted over by others.", "undo-warn")
            return
        c._refresh_now = True  # its tiles arrived just before this answer: show them promptly
        sids = [s for s in (header.get("layers") or []) if isinstance(s, str)] or sorted(u.sids)
        names = sorted(c.layer_name(s) for s in sids)
        where = "“%s”" % names[0] if len(names) == 1 else "%d layers" % len(names)
        c.log("%s your change on %s." % ("Undid" if kind == "undo" else "Redid", where), "undo")

    # ---- per tick --------------------------------------------------------------------------
    def _active_sid(self):
        return self.c.sid_of.get(self.c.doc.active_uuid())

    def _action_layers(self):
        c = self.c
        sids = set()
        a = self._active_sid()
        if a:
            sids.add(a)
        for u in c.doc.selected_uuids():
            s = c.sid_of.get(u)
            if s:
                sids.add(s)
        return sids

    def track(self, now):
        """Probe the image only while something is in progress, and react to the idle edge."""
        t, h, c = self.tracker, self.hist, self.c
        if h.broken or (t.state == SEALED and not self.queue):
            return
        if t.on_probe(c._idle(), self._active_sid(), h):
            c._detect(now, force=True, phase="edge")  # a layer op in this action: before its pixels
            sids = set(t.layers) | self._action_layers()
            if h.cur is not None:
                sids |= h.cur.sids
            c.replica.arm(sids)

    def boost(self):
        t = self.tracker
        return (t.state == TAIL and (self.hist.cur is not None or bool(self.queue))) or \
            (bool(self.queue) and self.c.replica.sealing())

    def run(self, now):
        c, h = self.c, self.hist
        if h.broken or self._epoch != h.epoch:  # switched off, or the history was reset (resize...)
            self._epoch = h.epoch
            self.queue.clear()
            self.pending = None
            return
        if self.pending is not None:
            if now - self.pending["t"] > self.cfg.undo_echo_wait:
                self.pending = None
                self.queue.clear()
                c.log("No answer from the host for your undo (slow connection?). Check the picture "
                      "before undoing again.", "undo-warn")
            return  # one request at a time
        self._flushed_now = False
        while self.queue:
            req = self.queue[0]
            # A new stroke after the key press must never be what gets undone: a press on the
            # canvas, or a new busy period while the previous action was still closing, cancels.
            if self.inputs != req.inputs or self.tracker.merged != req.merged:
                self._cancel("%s cancelled: you started drawing again." % req.kind.capitalize())
                return
            if not c._idle():
                waited = now - req.t
                if waited > self.cfg.undo_wait_max or (
                        waited > self.cfg.undo_wait and now - self.tracker.last_record > self.cfg.undo_wait):
                    self._cancel(TOOL_HINT)
                return
            if not self._prepare(req):
                return  # still re-reading layers; continues next tick
            if self._flushed_now:
                return  # that took this tick's time: carry on next tick
            u = h.top(req.kind)
            if u is None:
                self.queue.popleft()
                if now - self._nothing_t > 2.0:
                    self._nothing_t = now
                    c.log("Nothing of yours to undo (only changes you made in this session can be undone)."
                          if req.kind == "undo" else "Nothing of yours to redo.", "undo-warn")
                continue
            if u.kind == "barrier":
                h.stack(req.kind).pop()
                self.queue.popleft()
                c.log(u.text, "undo-warn")
                continue
            if u.flag:
                text = FLAG_TEXT.get(u.flag, "That change can't be undone.")
                if u.flag == "layers" and not u.sids:
                    text = LAYER_TEXT  # a plain layer delete
                h.discard(u)
                self.queue.popleft()
                c.log(text, "undo-warn")
                continue
            self.queue.popleft()
            h.stack(req.kind).remove(u)
            h.stack("redo" if req.kind == "undo" else "undo").append(u)
            rid = self._next_req
            self._next_req += 1
            self.pending = {"req": rid, "kind": req.kind, "unit": u, "t": now, "skips": req.skips}
            c._send_undo(req.kind, u.uid, rid)
            return

    def _prepare(self, req):
        """Make sure everything you did before the key press has been sent and closed."""
        t, r = self.tracker, self.c.replica
        if t.state == ACTIVE:
            return False
        if not req.flushed:
            req.flushed = True
            mark = (self.inputs, t.records, self.native_notes)
            # Skip only for a quick repeat with nothing new in between (holding Ctrl+Z down).
            if mark != self.flush_mark or time.perf_counter() - self.flush_t > 0.3:
                # Your last action is on the selected layer(s); if none is shared, check them all.
                r.arm(self._action_layers() or list(r.layers))
        if r.sealing():
            self._flushed_now = True
            r.seal_now(time.perf_counter() + self.cfg.undo_flush_cap, self._active_sid())
        t.settle(self.hist, r)
        if t.state == SEALED and not r.sealing():
            self.flush_mark = (self.inputs, t.records, self.native_notes)
            self.flush_t = time.perf_counter()
            return True
        return False

    def _cancel(self, text):
        self.queue.clear()
        self.c.log(text, "undo-warn")

    # ---- status ------------------------------------------------------------------------------
    def state(self):
        n_undo, n_redo = self.hist.counts()
        waiting = None
        if self.pending is not None:
            waiting = "host"
        elif self.queue:
            waiting = "tool" if self.c._idle_memo is False else "scan"
        return {"undo": n_undo, "redo": n_redo, "waiting": waiting, "broken": self.hist.broken}
