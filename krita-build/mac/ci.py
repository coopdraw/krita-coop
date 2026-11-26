#!/usr/bin/env python3
"""Start and watch the macOS build on GitHub from the command line.

    python krita-build/mac/ci.py start [universal|arm64|x86_64]
    python krita-build/mac/ci.py status            latest run: state and its steps
    python krita-build/mac/ci.py wait              block until the latest run has finished
    python krita-build/mac/ci.py logs [step text]  log of the failed step (or of the step whose name contains the text)

The repository is taken from "git remote get-url origin" and the token from git's credential
helper for this repository (it needs "Actions: read and write"). The token is never printed.
"""

import io
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

WORKFLOW = "macos.yml"


def git(*args, stdin=None):
    return subprocess.run(["git", *args], input=stdin, capture_output=True, text=True, check=True).stdout


def repo():
    url = git("remote", "get-url", "origin").strip()
    m = re.search(r"github\.com[:/]([^/]+)/(.+?)(\.git)?$", url)
    if not m:
        sys.exit("origin is not a GitHub repository: %s" % url)
    return "%s/%s" % (m.group(1), m.group(2))


def token():
    out = git("credential", "fill", stdin="protocol=https\nhost=github.com\n\n")
    m = re.search(r"^password=(.+)$", out, re.M)
    if not m:
        sys.exit("no GitHub credential stored for this repository")
    return m.group(1)


def api(path, method="GET", body=None, raw=False):
    req = urllib.request.Request("https://api.github.com/repos/%s/%s" % (repo(), path), method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + token())
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "krita-coop-ci")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        sys.exit("GitHub answered %s for %s: %s" % (e.code, path, e.read().decode(errors="replace")[:300]))
    if raw:
        return data
    return json.loads(data) if data else None


def latest_run():
    runs = api("actions/workflows/%s/runs?per_page=1" % WORKFLOW)["workflow_runs"]
    if not runs:
        sys.exit("no runs yet")
    return runs[0]


def jobs(run):
    return api("actions/runs/%d/jobs" % run["id"])["jobs"]


def show(run):
    print("run #%d  %s / %s  started %s  %s" % (run["run_number"], run["status"], run["conclusion"],
                                               run["run_started_at"], run["html_url"]))
    for job in jobs(run):
        for s in job["steps"]:
            dur = ""
            if s.get("started_at") and s.get("completed_at"):
                t0 = time.mktime(time.strptime(s["started_at"][:19], "%Y-%m-%dT%H:%M:%S"))
                t1 = time.mktime(time.strptime(s["completed_at"][:19], "%Y-%m-%dT%H:%M:%S"))
                dur = "%4d s" % (t1 - t0)
            print("  %-11s %-10s %7s  %s" % (s["status"], s["conclusion"] or "", dur, s["name"]))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        arch = sys.argv[2] if len(sys.argv) > 2 else "universal"
        api("actions/workflows/%s/dispatches" % WORKFLOW, "POST", {"ref": "main", "inputs": {"arch": arch}})
        print("started a %s build" % arch)
    elif cmd == "status":
        show(latest_run())
    elif cmd == "wait":
        while True:
            run = latest_run()
            if run["status"] == "completed":
                break
            time.sleep(60)
        show(run)
    elif cmd == "logs":
        want = sys.argv[2].lower() if len(sys.argv) > 2 else None
        tail = int(sys.argv[3]) if len(sys.argv) > 3 else 150
        run = latest_run()
        for job in jobs(run):
            steps = [s for s in job["steps"] if (want in s["name"].lower() if want else s["conclusion"] == "failure")]
            if not steps:
                continue
            text = api("actions/jobs/%d/logs" % job["id"], raw=True).decode(errors="replace")
            for s in steps:
                # the job log marks each step with a "##[group]Run ..." line; cut by the step's time span
                start, end = s.get("started_at", "")[:19], (s.get("completed_at") or "9999")[:19]
                lines = [ln for ln in text.splitlines() if start <= ln[:19] <= end]
                print("=== %s (%s): last %d of %d lines" % (s["name"], s["conclusion"], min(tail, len(lines)), len(lines)))
                for ln in lines[-tail:]:
                    print(re.sub(r"^\S+Z ", "", ln))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
