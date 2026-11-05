#!/usr/bin/env python3
"""Stand-in for the `colab` CLI used by the bridge tests.

Supports `[--config PATH] status -s NAME`, `[--config PATH] stop -s NAME`,
`[--config PATH] usage` and `[--config PATH] sessions`.

status: records the call in `<PATH>.calls` and, if `<PATH>.next` exists, replaces the
session file with it (simulating the CLI refreshing the proxy token or pruning a session
that is gone server-side).
stop: records the call in `<PATH>.stops` and removes the session from the file, like
the real CLI. `<PATH>.fail` makes any command exit 1.
usage/sessions: record the call in `<PATH>.reads` and print `<PATH>.usage` /
`<PATH>.sessions` if present, else output in the format of google-colab-cli 0.7.5.
"""

import json
import os
import shutil
import sys

args = sys.argv[1:]
path = None
if args[:1] == ["--config"]:
    path, args = args[1], args[2:]
if path is not None and args in (["usage"], ["sessions"]):
    with open(path + ".reads", "a") as f:
        f.write(args[0] + "\n")
    if os.path.exists(path + ".fail"):
        sys.exit(1)
    if os.path.exists(path + "." + args[0]):
        with open(path + "." + args[0]) as f:
            sys.stdout.write(f.read())
    elif args[0] == "usage":
        print("Current balance: 576.13 compute units\nUsage rate: 1.15/hr\nActive assignments: 1")
    else:
        with open(path) as f:
            data = json.load(f)
        if not data:
            print("[colab] No active sessions found on server.")
        for name, e in data.items():
            hw = "CPU" if e.get("accelerator") == "NONE" else e.get("accelerator", "T4")
            print(f"[{name}] {e.get('endpoint', 'fake-endpoint')} | Hardware: {hw} | Shape: Standard | Variant: GPU")
    sys.exit(0)
if path is None or args[:1] not in (["status"], ["stop"]) or args[1:2] != ["-s"] or len(args) != 3:
    sys.exit(2)

if args[0] == "stop":
    with open(path + ".stops", "a") as f:
        f.write(args[2] + "\n")
    if os.path.exists(path + ".fail"):
        sys.exit(1)
    with open(path) as f:
        data = json.load(f)
    data.pop(args[2], None)
    with open(path, "w") as f:
        json.dump(data, f)
    sys.exit(0)

with open(path + ".calls", "a") as f:
    f.write(args[2] + "\n")
if os.path.exists(path + ".fail"):
    sys.exit(1)
if os.path.exists(path + ".next"):
    shutil.copyfile(path + ".next", path)
