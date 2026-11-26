"""TCP transport: one reader and one writer thread per connection.

Reader threads never touch Krita.  They parse frames, inflate tiles and push
``(kind, conn, header, payload, tiles)`` tuples onto a ``queue.Queue`` that the
GUI thread drains every tick.
"""

import hashlib
import hmac
import itertools
import os
import queue
import secrets
import socket
import threading
import time

from . import protocol as P

DEFAULT_PORT = 27760
PING_INTERVAL = 5.0
READ_TIMEOUT = 30.0
# Until a connection to the host has proven the session code it may be anyone who found the port
# (it can be open to the internet), so it gets tiny frames, a short deadline, and a cap on how many
# such connections can wait at once.
HANDSHAKE_TIMEOUT = 15.0
PREAUTH_MAX_HEADER = 4096
MAX_PENDING = 16
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I

_CONTINUE, _DONE, _STOP = 0, 1, 2
_conn_ids = itertools.count(1)


def normalize_code(code):
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


def make_code():
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def clean_name(name, default="Guest"):
    name = " ".join(str(name or "").split())[:40]
    return name or default


def _mac(code, nonce):
    return hmac.new(normalize_code(code).encode("ascii"), b"krita-coop/1:" + nonce,
                    hashlib.sha256).hexdigest()


def _setup_socket(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


def local_addresses():
    """Non-loopback IPv4 addresses of this machine (no network traffic)."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("127."))


def parse_address(text, default_port=DEFAULT_PORT):
    text = text.strip()
    if text.startswith("[") and "]" in text:  # [ipv6]:port
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
    elif text.count(":") == 1:
        host, port = text.split(":")
    else:
        host, port = text, ""
    port = int(port) if port.strip() else default_port
    if not host or not (0 < port < 65536):
        raise ValueError("expected host:port")
    return host, port


class _CloseMarker(P.Outgoing):
    def __init__(self, reason, notify):
        self.reason = reason
        self.notify = notify


class Connection(object):
    def __init__(self, sock, inbox, handshake):
        self.id = next(_conn_ids)
        self.sock = sock
        self.inbox = inbox
        self.handshake = handshake
        self.out = queue.Queue()
        self._pending = 0
        self._plock = threading.Lock()
        self._clock = threading.Lock()
        self.closed = False
        self.peer_id = None
        self.name = "?"
        self.addr = None
        self.bytes_in = 0
        self.bytes_out = 0

    # ---- public (any thread) -------------------------------------------
    def start(self):
        threading.Thread(target=self._read_loop, name="coop-rx-%d" % self.id, daemon=True).start()
        threading.Thread(target=self._write_loop, name="coop-tx-%d" % self.id, daemon=True).start()

    @property
    def pending_bytes(self):
        return self._pending

    def send(self, item):
        if self.closed:
            return
        with self._plock:
            self._pending += item.size
        self.out.put(item)

    def send_msg(self, header, payload=b""):
        self.send(P.Frame(header, payload))

    def finish(self, reason, notify=True):
        """Close after everything already queued has been written."""
        self.out.put(_CloseMarker(reason, notify))

    def close(self, reason="closed", notify=True):
        with self._clock:
            if self.closed:
                return
            self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.out.put(None)
        if notify:
            self.inbox.put(("closed", self, {"reason": reason}, b"", None))

    # ---- threads ---------------------------------------------------------
    def _write_loop(self):
        while True:
            counted = True
            try:
                item = self.out.get(timeout=PING_INTERVAL)
            except queue.Empty:
                item = P.Frame({"t": "ping"})
                counted = False
            if item is None:
                return
            if isinstance(item, _CloseMarker):
                self.close(item.reason, item.notify)
                return
            try:
                data = item.frame()
                self.sock.sendall(data)
                self.bytes_out += len(data)
            except Exception as e:  # noqa: BLE001 - any failure ends the link
                self.close("send failed: %s" % e)
                return
            finally:
                if counted:
                    with self._plock:
                        self._pending -= item.size

    def _read_loop(self):
        try:
            untrusted = isinstance(self.handshake, _HostHandshake)
            deadline = time.monotonic() + HANDSHAKE_TIMEOUT
            self.sock.settimeout(HANDSHAKE_TIMEOUT if untrusted else READ_TIMEOUT)
            while not self.closed:
                if untrusted and self.handshake is not None:
                    header, payload = P.read_frame(self.sock, PREAUTH_MAX_HEADER, 0)
                    if time.monotonic() > deadline:  # pings would otherwise keep it open forever
                        raise P.ProtocolError("no valid session code within %d s" % HANDSHAKE_TIMEOUT)
                else:
                    header, payload = P.read_frame(self.sock)
                self.bytes_in += P.FRAME_HEAD.size + len(payload) + 64
                t = header["t"]
                if t == "ping":
                    continue
                if self.handshake is not None:
                    state = self.handshake(self, header, payload)
                    if state == _DONE:
                        self.handshake = None
                        self.sock.settimeout(READ_TIMEOUT)
                    elif state == _STOP:
                        return
                    continue
                if t == "tiles":
                    parsed = P.split_tiles(header, payload)
                elif t == "patch":
                    parsed = P.split_patch(header, payload)
                else:
                    parsed = None
                self.inbox.put(("msg", self, header, payload, parsed))
        except socket.timeout:
            self.close("connection timed out")
        except P.ProtocolError as e:
            self.close("protocol error: %s" % e)
        except (ConnectionError, OSError) as e:
            self.close("disconnected (%s)" % e if not self.closed else "closed")
        except Exception as e:  # noqa: BLE001
            self.close("error: %r" % (e,))


class _HostHandshake(object):
    def __init__(self, code):
        self.code = code
        self.nonce = None
        self.name = None

    def __call__(self, conn, header, payload):
        t = header["t"]
        if self.nonce is None:
            if t != "hello" or header.get("app") != P.APP_ID:
                raise P.ProtocolError("not a krita-coop client")
            if header.get("proto") != P.PROTO_VERSION:
                conn.send_msg({"t": "reject", "reason": "plugin version mismatch (host protocol %d, yours %s)"
                               % (P.PROTO_VERSION, header.get("proto"))})
                conn.finish("version mismatch", notify=False)
                conn.inbox.put(("log", None, {"text": "%s (at %s) tried to join with another plugin version "
                                              "(protocol %s; yours is %d). Everyone needs the same version."
                                              % (clean_name(header.get("name")),
                                                 conn.addr[0] if conn.addr else "?", header.get("proto"),
                                                 P.PROTO_VERSION)}, b"", None))
                return _STOP
            self.name = clean_name(header.get("name"))
            self.nonce = os.urandom(16)
            conn.send_msg({"t": "challenge", "nonce": self.nonce.hex()})
            return _CONTINUE
        if t != "auth":
            raise P.ProtocolError("expected auth")
        if not hmac.compare_digest(str(header.get("mac", "")), _mac(self.code, self.nonce)):
            conn.send_msg({"t": "reject", "reason": "wrong session code"})
            conn.finish("wrong code", notify=False)
            conn.inbox.put(("log", None, {"text": "Someone at %s tried to join with a wrong code."
                                                  % (conn.addr[0] if conn.addr else "?")}, b"", None))
            return _STOP
        conn.name = self.name
        conn.inbox.put(("joined", conn, {"name": self.name}, b"", None))
        return _DONE


class _GuestHandshake(object):
    def __init__(self, code):
        self.code = code

    def __call__(self, conn, header, payload):
        t = header["t"]
        if t == "challenge":
            try:
                nonce = bytes.fromhex(str(header.get("nonce", "")))
            except ValueError:
                raise P.ProtocolError("bad challenge")
            conn.send_msg({"t": "auth", "mac": _mac(self.code, nonce)})
            return _CONTINUE
        if t == "reject":
            conn.inbox.put(("msg", conn, header, b"", None))
            conn.finish("rejected: %s" % header.get("reason", ""), notify=False)
            return _STOP
        if t == "welcome":
            conn.inbox.put(("msg", conn, header, payload, None))
            return _DONE
        raise P.ProtocolError("unexpected %r during handshake" % t)


class Server(object):
    def __init__(self, port, code, inbox):
        self.port = port
        self.code = code
        self.inbox = inbox
        self.sock = None
        self._stopped = False
        self._unproven = []  # connections still in the handshake

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: refuse port sharing
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", self.port))
        s.listen(8)
        self.sock = s
        self.port = s.getsockname()[1]
        threading.Thread(target=self._accept_loop, name="coop-accept", daemon=True).start()

    def _accept_loop(self):
        while not self._stopped:
            try:
                s, addr = self.sock.accept()
            except OSError:
                break
            self._unproven = [c for c in self._unproven if c.handshake is not None and not c.closed]
            if len(self._unproven) >= MAX_PENDING:
                try:
                    s.close()
                except OSError:
                    pass
                continue
            try:
                _setup_socket(s)
                conn = Connection(s, self.inbox, _HostHandshake(self.code))
                conn.addr = addr
                conn.start()
                self._unproven.append(conn)
            except OSError:
                try:
                    s.close()
                except OSError:
                    pass

    def stop(self):
        self._stopped = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


def connect(host, port, code, name, inbox):
    """Connect in the background; posts ``connected`` or ``connect_failed``."""

    def run():
        try:
            sock = socket.create_connection((host, port), timeout=10)
            _setup_socket(sock)
            conn = Connection(sock, inbox, _GuestHandshake(code))
            conn.addr = (host, port)
            conn.start()
            conn.send_msg({"t": "hello", "app": P.APP_ID, "proto": P.PROTO_VERSION, "name": name})
            inbox.put(("connected", conn, {}, b"", None))
        except OSError as e:
            inbox.put(("connect_failed", None, {"reason": str(e)}, b"", None))

    threading.Thread(target=run, name="coop-connect", daemon=True).start()
