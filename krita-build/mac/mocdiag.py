#!/usr/bin/env python3
"""Where a build's time goes, and how long Qt's moc takes per header.

    python3 mocdiag.py <build dir> [target dir in the build tree, default libs/ui] [target name, default kritaui]

Reads ninja's own timing log (.ninja_log) and times moc by hand on a few headers of one target,
with the include paths and definitions CMake would pass (from the target's AutogenInfo.json,
which exists after "configure"). Works on any platform; on Windows only wall time is shown.
"""

import json
import os
import subprocess
import sys
import time

try:
    import resource
except ImportError:  # Windows
    resource = None


def ninja_log(build):
    path = os.path.join(build, ".ninja_log")
    if not os.path.exists(path):
        print("no .ninja_log in", build)
        return
    last = {}
    for ln in open(path, encoding="utf-8", errors="replace"):
        parts = ln.rstrip("\n").split("\t")
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        out = parts[3]
        if os.path.isabs(out):  # autogen steps are listed twice, once with the full path
            continue
        last[out] = (int(parts[1]) - int(parts[0])) / 1000.0
    kinds = {}
    for out, d in last.items():
        if "_autogen/timestamp" in out:
            kind = "moc (autogen)"
        elif out.endswith((".o", ".obj")):
            kind = "compile"
        elif out.endswith((".dylib", ".so", ".dll", ".a", ".exe")) or "/MacOS/" in out:
            kind = "link"
        else:
            kind = "other"
        n, s = kinds.get(kind, (0, 0.0))
        kinds[kind] = (n + 1, s + d)
    print("\n=== ninja log: %d steps; time summed per kind (steps overlap, so this is not wall time)" % len(last))
    for kind, (n, s) in sorted(kinds.items(), key=lambda kv: -kv[1][1]):
        print("%9.0f s %6d  %s" % (s, n, kind))
    print("\n=== the 30 longest steps")
    for out, d in sorted(last.items(), key=lambda kv: -kv[1])[:30]:
        print("%8.1f s  %s" % (d, out))


def timed(cmd):
    before = resource.getrusage(resource.RUSAGE_CHILDREN) if resource else None
    t0 = time.perf_counter()
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    wall = time.perf_counter() - t0
    if resource:
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        return wall, after.ru_utime - before.ru_utime, after.ru_stime - before.ru_stime, r
    return wall, None, None, r


def fmt(wall, user, sys_):
    if user is None:
        return "wall %6.2f s" % wall
    return "wall %6.2f s  user %6.2f s  sys %5.2f s" % (wall, user, sys_)


def moc_times(build, target_dir, target):
    info_path = os.path.join(build, target_dir, "CMakeFiles", target + "_autogen.dir", "AutogenInfo.json")
    if not os.path.exists(info_path):
        print("no", info_path)
        return
    info = json.load(open(info_path, encoding="utf-8"))
    moc = info["QT_MOC_EXECUTABLE"]
    headers = [h[0] for h in info["HEADERS"] if "M" in h[1]]
    print("\n=== moc by hand: target %s, %d headers, %d sources, %d include paths, %d definitions, PARALLEL %s"
          % (target, len(headers), len(info["SOURCES"]), len(info["MOC_INCLUDES"]), len(info["MOC_DEFINITIONS"]),
             info.get("PARALLEL")))
    print("moc:", moc)
    for _ in range(3):
        print("  moc -v:           ", fmt(*timed([moc, "-v"])[:3]))
    args = []
    frameworks = []
    for inc in info["MOC_INCLUDES"]:
        args.append("-I" + inc)
        if ".framework/Headers" in inc:
            parent = os.path.dirname(inc.split(".framework/Headers")[0])
            if parent not in frameworks:
                frameworks.append(parent)
    args += ["-F" + f for f in frameworks]
    args += ["-D" + d for d in info["MOC_DEFINITIONS"]]
    args += info.get("MOC_OPTIONS", [])
    # any target's predefs file will do; they come from the same compiler call
    predefs = info["MOC_PREDEFS_FILE"]
    if not os.path.exists(predefs):
        for root, _dirs, files in os.walk(build):
            if "moc_predefs.h" in files:
                predefs = os.path.join(root, "moc_predefs.h")
                break
    if os.path.exists(predefs):
        args += ["--include", predefs]
        print("predefs:", predefs)
    else:
        print("no moc_predefs.h yet (nothing built); timing without it")
    with_object = [h for h in headers if b"Q_OBJECT" in open(h, "rb").read()]
    print("%d of the headers contain Q_OBJECT; timing 10 of them, spread over the list" % len(with_object))
    step = max(1, len(with_object) // 10)
    total = 0.0
    picked = with_object[::step][:10]
    null = "NUL" if os.name == "nt" else "/dev/null"
    for h in picked:
        wall, user, sys_, r = timed([moc] + args + ["-o", null, h])
        total += wall
        note = "" if r.returncode == 0 else "  (exit %d: %s)" % (r.returncode, r.stdout.decode(errors="replace").strip()[:160])
        print("  %-52s %s%s" % (os.path.basename(h), fmt(wall, user, sys_), note))
    if picked:
        print("average %.2f s per header; all %d would take about %.0f s on one core"
              % (total / len(picked), len(with_object), total / len(picked) * len(with_object)))


def main():
    build = sys.argv[1]
    target_dir = sys.argv[2] if len(sys.argv) > 2 else "libs/ui"
    target = sys.argv[3] if len(sys.argv) > 3 else "kritaui"
    ninja_log(build)
    moc_times(build, target_dir, target)


if __name__ == "__main__":
    main()
