"""Layer-tree model shared by host and guests.

A tree holds only *synced* nodes (paint layers and groups).  Node ids ("sids")
are stable across machines; each peer maps them to its own Krita node UUIDs.
Children lists are ordered bottom -> top, like ``Node.childNodes()``.

Every layer is shared: anyone may create, delete, move, regroup, rename,
lock or change the properties of any layer.
"""

import re

ROOT = ""
KINDS = ("paint", "group")
# Synchronised properties.  Visibility and group collapse stay personal.
SYNC_PROPS = ("name", "opacity", "blend", "ia", "cl", "locked")
DEFAULTS = {"name": "Layer", "opacity": 255, "blend": "normal", "ia": False, "cl": 0, "locked": False}

_SID_RE = re.compile(r"^[0-9A-Za-z{}\-]{1,64}$")
_BLEND_RE = re.compile(r"^[0-9A-Za-z_\-]{1,64}$")


def valid_sid(sid):
    return isinstance(sid, str) and bool(_SID_RE.match(sid))


def clean_props(props):
    """Validate untrusted props coming off the wire. Unknown keys are dropped."""
    out = {}
    if not isinstance(props, dict):
        return out
    if "name" in props:
        out["name"] = str(props["name"])[:256]
    if "opacity" in props:
        try:
            out["opacity"] = max(0, min(255, int(props["opacity"])))
        except (TypeError, ValueError):
            pass
    if "blend" in props and isinstance(props["blend"], str) and _BLEND_RE.match(props["blend"]):
        out["blend"] = props["blend"]
    for key in ("ia", "locked"):
        if key in props:
            out[key] = bool(props[key])
    if "cl" in props:
        try:
            out["cl"] = max(0, min(8, int(props["cl"])))
        except (TypeError, ValueError):
            pass
    return out


class Tree(object):
    def __init__(self):
        self.nodes = {}  # sid -> {type, vis, name, opacity, blend, ia, cl, locked}
        self.parent = {}  # sid -> parent sid (ROOT for top level)
        self.children = {ROOT: []}  # parent sid -> [sid] bottom -> top

    # ---- construction / serialisation -----------------------------------
    def copy(self):
        t = Tree()
        t.nodes = {k: dict(v) for k, v in self.nodes.items()}
        t.parent = dict(self.parent)
        t.children = {k: list(v) for k, v in self.children.items()}
        return t

    def add(self, sid, parent, node, index=None):
        if sid in self.nodes:
            raise ValueError("duplicate sid %s" % sid)
        self.nodes[sid] = node
        self.parent[sid] = parent
        kids = self.children.setdefault(parent, [])
        kids.insert(len(kids) if index is None else index, sid)
        if node["type"] == "group":
            self.children.setdefault(sid, [])

    def to_nested(self):
        def build(psid):
            out = []
            for sid in self.children.get(psid, []):
                d = dict(self.nodes[sid])
                d["id"] = sid
                if d["type"] == "group":
                    d["children"] = build(sid)
                out.append(d)
            return out
        return build(ROOT)

    @classmethod
    def from_nested(cls, nested, max_nodes=5000):
        """Build from untrusted JSON; invalid entries are skipped."""
        t = cls()

        def walk(items, psid, depth):
            if not isinstance(items, list) or depth > 64:
                return
            for d in items:
                if len(t.nodes) >= max_nodes:
                    return
                if not isinstance(d, dict):
                    continue
                sid = d.get("id")
                typ = d.get("type")
                if not valid_sid(sid) or sid in t.nodes or typ not in KINDS:
                    continue
                node = dict(DEFAULTS)
                node.update(clean_props(d))
                node["type"] = typ
                node["vis"] = bool(d.get("vis", True))
                t.add(sid, psid, node)
                if typ == "group":
                    walk(d.get("children", []), sid, depth + 1)

        walk(nested, ROOT, 0)
        return t

    # ---- queries ---------------------------------------------------------
    def preorder(self, start=ROOT):
        out = []

        def walk(psid):
            for sid in self.children.get(psid, []):
                out.append(sid)
                if self.nodes[sid]["type"] == "group":
                    walk(sid)
        walk(start)
        return out

    def descendants(self, sid):
        return self.preorder(sid) if self.nodes.get(sid, {}).get("type") == "group" else []

    def is_ancestor(self, anc, sid):
        p = self.parent.get(sid)
        while p is not None and p != ROOT:
            if p == anc:
                return True
            p = self.parent.get(p)
        return False

    def paint_sids(self):
        return [s for s, n in self.nodes.items() if n["type"] == "paint"]

    # ---- mutation --------------------------------------------------------
    def detach(self, sid):
        p = self.parent[sid]
        self.children[p].remove(sid)
        return p

    def move(self, sid, parent, index=None):
        self.detach(sid)
        self.parent[sid] = parent
        kids = self.children.setdefault(parent, [])
        kids.insert(len(kids) if index is None else max(0, min(index, len(kids))), sid)

    def remove_subtree(self, sid):
        gone = [sid] + self.descendants(sid)
        self.detach(sid)
        for s in gone:
            self.nodes.pop(s, None)
            self.parent.pop(s, None)
            self.children.pop(s, None)
        return gone

    def reorder(self, psid, order):
        """Put the listed children in the given relative order; others keep their slots."""
        cur = self.children.get(psid)
        if not cur:
            return
        present = set(cur)
        seen = set()
        listed = []
        for s in order:
            if s in present and s not in seen:
                listed.append(s)
                seen.add(s)
        slots = sorted(cur.index(s) for s in listed)
        new = list(cur)
        for slot, s in zip(slots, listed):
            new[slot] = s
        self.children[psid] = new


def diff_ops(base, cur):
    """Operations that turn ``base`` into ``cur`` (sent to the host)."""
    ops = []
    for sid in cur.preorder():  # creates (parents first), moves and property changes
        n = cur.nodes[sid]
        if sid not in base.nodes:
            ops.append({"op": "create", "id": sid, "parent": cur.parent[sid], "type": n["type"],
                        "props": {k: n[k] for k in SYNC_PROPS}, "vis": n["vis"]})
            continue
        if cur.parent[sid] != base.parent.get(sid):
            ops.append({"op": "reparent", "id": sid, "parent": cur.parent[sid]})
        b = base.nodes[sid]
        changed = {k: n[k] for k in SYNC_PROPS if n[k] != b[k]}
        if changed:
            ops.append({"op": "props", "id": sid, "props": changed})
    # Deletes come after moves, so a layer dragged out of a group survives
    # deleting that group.
    for sid in base.preorder():
        if sid not in cur.nodes:
            ops.append({"op": "delete", "id": sid})
    for psid, kids in cur.children.items():
        if kids != base.children.get(psid, []) and len(kids) > 1:
            ops.append({"op": "order", "parent": psid, "children": list(kids)})
    return ops


class OpResult(object):
    def __init__(self):
        self.created = []
        self.deleted = []


def apply_ops(tree, ops):
    """Apply operations to ``tree`` in place (host side)."""
    res = OpResult()
    if not isinstance(ops, list):
        return res
    for op in ops[:10000]:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        sid = op.get("id")
        if kind == "create":
            typ = op.get("type")
            if not valid_sid(sid) or sid in tree.nodes or typ not in KINDS:
                continue
            parent = op.get("parent", ROOT)
            if parent != ROOT and tree.nodes.get(parent, {}).get("type") != "group":
                parent = ROOT
            node = dict(DEFAULTS)
            node.update(clean_props(op.get("props")))
            node.update({"type": typ, "vis": bool(op.get("vis", True))})
            tree.add(sid, parent, node)
            res.created.append(sid)
        elif kind == "delete":
            if sid in tree.nodes:
                res.deleted.extend(tree.remove_subtree(sid))
        elif kind == "reparent":
            parent = op.get("parent", ROOT)
            if sid not in tree.nodes:
                continue
            if parent != ROOT and (tree.nodes.get(parent, {}).get("type") != "group"
                                   or parent == sid or tree.is_ancestor(sid, parent)):
                continue
            if tree.parent[sid] != parent:
                tree.move(sid, parent)
        elif kind == "order":
            parent = op.get("parent", ROOT)
            kids = op.get("children")
            if (parent == ROOT or parent in tree.nodes) and isinstance(kids, list):
                tree.reorder(parent, [k for k in kids if isinstance(k, str)])
        elif kind == "props":
            if sid in tree.nodes:
                tree.nodes[sid].update(clean_props(op.get("props")))
    return res
