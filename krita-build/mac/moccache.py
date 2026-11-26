#!/usr/bin/env python3
"""A result cache for Qt's moc, the way ccache is one for the compiler.

    moccache.py <real moc> <moc arguments ...>

Krita's headers pull in so much that moc needs one to several seconds per header, a few thousand
times per build. On a machine with few cores that is most of a rebuild even when every compile
comes out of ccache. CMake is pointed at a wrapper that calls this script instead of moc
(-DCMAKE_AUTOMOC_EXECUTABLE, see build.sh).

How it decides. moc is asked for a dependency file anyway (CMake passes --output-dep-file), which
lists every file moc read. A stored result is reused when
  * the moc program, the working directory and all arguments are the same (the key), and
  * every file that run read still has the same content (compared by hash, not by time).
What this cannot see: a header that did not exist then and would be found now. For that the
caller puts a hash of the *names* of all source and dependency headers into MOCCACHE_SALT, so
adding or removing any file starts a new cache.

Whenever anything is unclear (no -o, no dependency file, an unreadable file, moc failing), moc is
simply run and nothing is stored.

Environment:
  MOCCACHE_DIR     where results are kept; without it moc is run directly
  MOCCACHE_SALT    see above
  MOCCACHE_VERIFY  if set: on a cache hit run moc anyway and compare (a self-test; no time saved)
"""

import hashlib
import json
import os
import subprocess
import sys
import zlib

VERSION = "1"
MAX_VARIANTS = 3        # results kept per key (they differ in the content of the files read)


def content_hash(path, memo):
    if path not in memo:
        try:
            with open(path, "rb") as f:
                memo[path] = hashlib.blake2b(f.read(), digest_size=10).hexdigest()
        except OSError:
            memo[path] = None
    return memo[path]


def option_value(args, name):
    for i, a in enumerate(args[:-1]):
        if a == name:
            return args[i + 1]
    return None


def option_values(args, name):
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == name]


def parse_depfile(data):
    """The prerequisites of a Make-style dependency file as moc writes it."""
    text = data.decode("utf-8", errors="surrogateescape").replace("\\\r\n", " ").replace("\\\n", " ")
    cut = text.find(": ")
    if cut < 0:
        return None
    deps, cur, i, rest = [], [], 0, text[cut + 2:]
    while i < len(rest):
        c = rest[i]
        if c == "\\" and i + 1 < len(rest) and rest[i + 1] in " #":
            cur.append(rest[i + 1])
            i += 2
            continue
        if c == "$" and rest[i + 1:i + 2] == "$":
            cur.append("$")
            i += 2
            continue
        if c in " \t\r\n":
            if cur:
                deps.append("".join(cur))
                cur = []
        else:
            cur.append(c)
        i += 1
    if cur:
        deps.append("".join(cur))
    return deps


def count(cache, letter):
    """One letter per call in stats.log: h = from the cache, m = stored, u = not cacheable,
    X = a stored result failed MOCCACHE_VERIFY."""
    try:
        fd = os.open(os.path.join(cache, "stats.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, letter.encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def write_file(path, data):
    with open(path, "wb") as f:
        f.write(data)


def verified(moc, args, out, rec):
    """MOCCACHE_VERIFY: run moc anyway and compare its output with the stored one."""
    probe = os.path.join(os.path.dirname(out), "moccache_verify_%d_%s" % (os.getpid(), os.path.basename(out)))
    probe_args = list(args)
    probe_args[probe_args.index("-o") + 1] = probe
    if "--dep-file-path" in probe_args:
        probe_args[probe_args.index("--dep-file-path") + 1] = probe + ".d"
    try:
        subprocess.run([moc] + probe_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(probe, "rb") as f:
            same = f.read() == rec["out"].encode("latin-1")
    except OSError:
        same = False
    for leftover in (probe, probe + ".d"):
        try:
            os.remove(leftover)
        except OSError:
            pass
    if not same:
        sys.stderr.write("moccache: STORED RESULT DIFFERS FROM MOC for %s\n" % out)
    return same


def main():
    moc, args = sys.argv[1], sys.argv[2:]
    cache = os.environ.get("MOCCACHE_DIR")
    out = option_value(args, "-o")
    if not cache or not out or "--output-dep-file" not in args:
        if cache:
            count(cache, "u")
        return subprocess.call([moc] + args)
    dep_path = option_value(args, "--dep-file-path") or out + ".d"
    memo = {}

    key = hashlib.sha256()
    for part in [VERSION, os.environ.get("MOCCACHE_SALT", ""), content_hash(moc, memo) or "", os.getcwd()] + args:
        key.update(part.encode("utf-8", errors="surrogateescape") + b"\0")
    for a in args:
        if a.startswith("@"):                     # a response file: its content is part of the command
            key.update((content_hash(a[1:], memo) or "?").encode() + b"\0")
    key = key.hexdigest()
    entry = os.path.join(cache, key[:2], key[2:])

    # a stored result whose inputs all still have the same content?
    try:
        variants = sorted((os.path.join(entry, n) for n in os.listdir(entry) if n.endswith(".z")),
                          key=os.path.getmtime, reverse=True)
    except OSError:
        variants = []
    for path in variants:
        try:
            with open(path, "rb") as f:
                rec = json.loads(zlib.decompress(f.read()).decode("utf-8"))
            if all(content_hash(p, memo) == h for p, h in rec["deps"]):
                if os.environ.get("MOCCACHE_VERIFY") and not verified(moc, args, out, rec):
                    count(cache, "X")
                    break                         # run moc below and store its result instead
                write_file(out, rec["out"].encode("latin-1"))
                write_file(dep_path, rec["dep"].encode("latin-1"))
                sys.stdout.write(rec["stdout"])
                sys.stderr.write(rec["stderr"])
                try:
                    os.utime(path)                # "used now", for the clean-up by age
                except OSError:
                    pass
                count(cache, "h")
                return 0
        except (OSError, ValueError, KeyError, zlib.error):
            continue

    # run moc and store what it made
    for stale in (out, dep_path):
        try:
            os.remove(stale)
        except OSError:
            pass
    proc = subprocess.run([moc] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    if proc.returncode != 0:
        return proc.returncode
    try:
        with open(out, "rb") as f:
            out_data = f.read()
        with open(dep_path, "rb") as f:
            dep_data = f.read()
    except OSError:                               # e.g. "No relevant classes found. No output generated."
        count(cache, "u")
        return 0
    deps = parse_depfile(dep_data)
    if not deps:
        count(cache, "u")
        return 0
    for extra in option_values(args, "--include"):
        if extra not in deps:
            deps.append(extra)
    hashed = [[p, content_hash(p, memo)] for p in deps]
    if any(h is None for _p, h in hashed):
        count(cache, "u")
        return 0
    rec = {"deps": hashed, "out": out_data.decode("latin-1"), "dep": dep_data.decode("latin-1"),
           "stdout": stdout, "stderr": stderr}
    name = hashlib.sha256(json.dumps(hashed).encode("utf-8")).hexdigest()[:16]
    try:
        os.makedirs(entry, exist_ok=True)
        tmp = os.path.join(entry, "%s.%d.tmp" % (name, os.getpid()))
        write_file(tmp, zlib.compress(json.dumps(rec).encode("utf-8"), 6))
        os.replace(tmp, os.path.join(entry, name + ".z"))
        for old in variants[MAX_VARIANTS - 1:]:
            try:
                os.remove(old)
            except OSError:
                pass
        count(cache, "m")
    except OSError:
        count(cache, "u")
    return 0


if __name__ == "__main__":
    sys.exit(main())
