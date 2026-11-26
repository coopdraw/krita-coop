"""Plain data returned by document adapters (no Krita imports)."""


class LNode(object):
    """One local node: kind is "paint", "group" or "other" (anything not synced)."""

    __slots__ = ("kind", "parent", "props")

    def __init__(self, kind, parent, props):
        self.kind = kind
        self.parent = parent
        self.props = props  # dict with name/opacity/blend/ia/cl/vis/locked, or None for "other"


class Structure(object):
    def __init__(self):
        self.nodes = {}  # uuid -> LNode
        self.children = {"": []}  # parent uuid ("" = root) -> [uuid] bottom -> top
        self.unsupported = 0  # layers of kinds that are not shared
