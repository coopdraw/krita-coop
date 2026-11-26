"""Wire protocol.

Every frame is::

    uint32 header_len | uint32 payload_len | header (UTF-8 JSON) | payload (bytes)

The header is a small JSON object with a ``t`` (type) field.  Two frame types
carry pixels in their payload, as a table of fixed-size entries followed by
zlib-compressed blobs:

* ``tiles`` (host -> everyone): authoritative tile contents after merging.
* ``patch`` (anyone -> host): pixels someone changed, plus a per-pixel mask of
  which pixels they changed.  A patch made by an action its author may undo
  carries ``"unit": <id>`` in its header; ``undo``/``redo`` messages name such
  a unit, and the host answers with ``undone`` (see ``server.py``).

Reader threads only split frames; blobs are inflated later on the GUI thread
(``inflate`` checks sizes, so a malicious blob cannot expand without bound).
Nothing here imports Qt or Krita.
"""

import json
import struct
import zlib

PROTO_VERSION = 4  # 4: per-person undo kept by the host (undo/redo/undone, patch "unit")
APP_ID = "krita-coop"
TILE = 64

FRAME_HEAD = struct.Struct(">II")
TILE_ENTRY = struct.Struct(">HHHHIII")  # tx, ty, w, h, raw_len, clen (0 = transparent), crc32
PATCH_ENTRY = struct.Struct(">HHHHIII")  # tx, ty, w, h, raw_len, vclen (0 = transparent), mclen (0 = every pixel)

MAX_HEADER = 8 << 20
MAX_PAYLOAD = 256 << 20
MAX_PIXEL_SIZE = 16  # RGBA F32
MAX_TILE_RAW = TILE * TILE * MAX_PIXEL_SIZE


class ProtocolError(Exception):
    pass


def encode_frame(header, payload=b""):
    h = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return FRAME_HEAD.pack(len(h), len(payload)) + h + payload


def _recv_exact(sock, n):
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(view[got:], n - got)
        if r == 0:
            raise ConnectionError("connection closed by peer")
        got += r
    return bytes(buf)


def read_frame(sock, max_header=MAX_HEADER, max_payload=MAX_PAYLOAD):
    """Block until one full frame is read. Returns (header_dict, payload_bytes).

    The limits are checked before anything is allocated, so a peer can't make us reserve memory
    by announcing a huge frame."""
    hlen, plen = FRAME_HEAD.unpack(_recv_exact(sock, FRAME_HEAD.size))
    if hlen > max_header or plen > max_payload:
        raise ProtocolError("frame too large (%d/%d)" % (hlen, plen))
    try:
        header = json.loads(_recv_exact(sock, hlen).decode("utf-8"))
    except ValueError as e:
        raise ProtocolError("bad header: %s" % e)
    if not isinstance(header, dict) or not isinstance(header.get("t"), str):
        raise ProtocolError("header without type")
    payload = _recv_exact(sock, plen) if plen else b""
    return header, payload


class Outgoing(object):
    size = 0

    def frame(self):
        raise NotImplementedError


class Frame(Outgoing):
    """Pre-encoded bytes; one Frame object can be queued on many connections."""

    __slots__ = ("data", "size")

    def __init__(self, header, payload=b""):
        self.data = encode_frame(header, payload)
        self.size = len(self.data)

    def frame(self):
        return self.data


def tiles_frame(header, entries):
    """entries: (tx, ty, w, h, raw_len, blob_or_None, crc)."""
    table = [TILE_ENTRY.pack(tx, ty, w, h, rl, len(b) if b else 0, crc) for tx, ty, w, h, rl, b, crc in entries]
    header = dict(header, t="tiles", n=len(entries))
    return Frame(header, b"".join(table) + b"".join(e[5] for e in entries if e[5]))


def patch_frame(header, entries):
    """entries: (tx, ty, w, h, raw_len, value_blob_or_None, mask_blob_or_None[, crc])."""
    table = [PATCH_ENTRY.pack(e[0], e[1], e[2], e[3], e[4], len(e[5]) if e[5] else 0, len(e[6]) if e[6] else 0)
             for e in entries]
    blobs = []
    for e in entries:
        if e[5]:
            blobs.append(e[5])
        if e[6]:
            blobs.append(e[6])
    header = dict(header, t="patch", n=len(entries))
    return Frame(header, b"".join(table) + b"".join(blobs))


def _check_entry(w, h, raw_len):
    if not (0 < w <= TILE and 0 < h <= TILE) or raw_len > MAX_TILE_RAW or raw_len % (w * h):
        raise ProtocolError("bad tile entry")


def _count(header, payload, size):
    n = header.get("n")
    if not isinstance(n, int) or n < 0 or n * size > len(payload):
        raise ProtocolError("bad tile count")
    return n


def split_tiles(header, payload):
    n = _count(header, payload, TILE_ENTRY.size)
    off = n * TILE_ENTRY.size
    out = []
    for i in range(n):
        tx, ty, w, h, rl, clen, crc = TILE_ENTRY.unpack_from(payload, i * TILE_ENTRY.size)
        _check_entry(w, h, rl)
        if off + clen > len(payload):
            raise ProtocolError("tile data truncated")
        out.append((tx, ty, w, h, rl, payload[off:off + clen] if clen else None, crc))
        off += clen
    return out


def split_patch(header, payload):
    n = _count(header, payload, PATCH_ENTRY.size)
    off = n * PATCH_ENTRY.size
    out = []
    for i in range(n):
        tx, ty, w, h, rl, vclen, mclen = PATCH_ENTRY.unpack_from(payload, i * PATCH_ENTRY.size)
        _check_entry(w, h, rl)
        if off + vclen + mclen > len(payload):
            raise ProtocolError("patch data truncated")
        v = payload[off:off + vclen] if vclen else None
        off += vclen
        m = payload[off:off + mclen] if mclen else None
        off += mclen
        out.append((tx, ty, w, h, rl, v, m))
    return out


def inflate(blob, n):
    """Decompress exactly ``n`` bytes (``None`` means a transparent/zero tile)."""
    if blob is None:
        return bytes(n)
    d = zlib.decompressobj()
    try:
        out = d.decompress(blob, n)
    except zlib.error as e:
        raise ProtocolError("bad tile data: %s" % e)
    if len(out) != n or d.unconsumed_tail or not d.eof:
        raise ProtocolError("tile size mismatch")
    return out


def deflate(raw):
    return zlib.compress(raw, 1)
