#!/usr/bin/env python3
"""Stand-in for the `colab` CLI used by the bridge tests.

Supports only `[--config PATH] status -s NAME`. On each call it records the invocation
and, if `<PATH>.next` exists, replaces the session file with it (simulating the CLI
refreshing the proxy token or pruning a session that is gone server-side).
"""

import os
import shutil
import sys

args = sys.argv[1:]
path = None
if args[:1] == ["--config"]:
    path, args = args[1], args[2:]
if path is None or args[:2] != ["status", "-s"] or len(args) != 3:
    sys.exit(2)

with open(path + ".calls", "a") as f:
    f.write(args[2] + "\n")
if os.path.exists(path + ".fail"):
    sys.exit(1)
if os.path.exists(path + ".next"):
    shutil.copyfile(path + ".next", path)
