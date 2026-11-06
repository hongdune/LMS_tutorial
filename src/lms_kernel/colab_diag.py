"""Diagnostics read from the Colab VM when a kernel disappears (CLAUDE.md 3장).

Measured 2026-11-07 (T4): kernels vanished because the Colab runtime *container* was
restarted (8 times in 8 minutes) while the VM and the assignment stayed. Each restart
starts a new Jupyter Server (every kernel is gone) on a fresh filesystem (downloaded
models and the copied helpers are gone). The memory cgroup showed no OOM.

Everything here is read through the standard Jupyter Contents API (the Colab Contents
root is ``/``) and only to tell those cases apart:

- container start: ``/proc/stat`` btime + start time of PID 1 (``/proc/1/stat``)
- OOM kills and memory peak/limit of the kernels' cgroup (``/var/colab/cgroup/jupyter-children``)
- ``/proc/meminfo`` (MemTotal, MemAvailable)

These paths are Colab image internals, not an API: every value is optional and a
failure only means less detail in the log. The files hold no secrets (unlike
``/var/colab/app.log``, which logs proxy tokens and is never read).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from tornado import httpclient

log = logging.getLogger("lms_kernel.colab")

PROXY_TOKEN_HEADER = "X-Colab-Runtime-Proxy-Token"
CGROUP = "var/colab/cgroup/jupyter-children"
CLK_TCK = 100  # USER_HZ on Linux x86-64
TIMEOUT = 15.0


async def read_text(info, path: str, timeout: float = TIMEOUT) -> Optional[str]:
    """Text of a VM file via ``GET /api/contents/<path>``, or None."""
    req = httpclient.HTTPRequest(
        f"{info.url}/api/contents/{path}?content=1&format=text&type=file",
        headers={PROXY_TOKEN_HEADER: info.token, "Accept": "application/json"},
        follow_redirects=False,
        connect_timeout=timeout,
        request_timeout=timeout,
    )
    try:
        resp = await httpclient.AsyncHTTPClient().fetch(req, raise_error=False)
        if resp.code != 200:
            return None
        content = json.loads(resp.body).get("content")
        return content if isinstance(content, str) else None
    except Exception:
        return None


def parse_container_start(proc_stat: Optional[str], pid1_stat: Optional[str]) -> Optional[datetime]:
    """Start time of PID 1 (the runtime container's init) from /proc/stat and /proc/1/stat."""
    try:
        btime = next(int(l.split()[1]) for l in proc_stat.splitlines() if l.startswith("btime "))
        fields = pid1_stat[pid1_stat.rindex(")") + 2:].split()
        start_ticks = int(fields[19])  # field 22 of /proc/<pid>/stat; fields[0] is field 3
    except (AttributeError, StopIteration, ValueError, IndexError):
        return None
    return datetime.fromtimestamp(btime + start_ticks / CLK_TCK, timezone.utc)


def parse_flat_keyed(text: Optional[str]) -> dict:
    """``key value`` lines (cgroup memory.events, /proc/meminfo with ``key: value kB``)."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2:
            try:
                value = int(parts[1])
            except ValueError:
                continue
            out[parts[0]] = value * 1024 if parts[2:3] == ["kB"] else value
    return out


def _int(text: Optional[str]) -> Optional[int]:
    try:
        return int((text or "").strip())
    except ValueError:
        return None  # includes "max" (no limit)


@dataclass
class Snapshot:
    container_start: Optional[datetime] = None
    oom_kill: Optional[int] = None
    memory_peak: Optional[int] = None
    memory_max: Optional[int] = None
    mem_total: Optional[int] = None
    mem_available: Optional[int] = None

    def summary(self) -> str:
        def gib(v):
            return "?" if v is None else f"{v / 2**30:.1f}GiB"

        start = self.container_start.strftime("%H:%M:%SZ") if self.container_start else "?"
        oom = "?" if self.oom_kill is None else str(self.oom_kill)
        return (
            f"container_start={start} oom_kill={oom} kernels_mem_peak={gib(self.memory_peak)}"
            f"/{gib(self.memory_max) if self.memory_max else 'max'} "
            f"vm_available={gib(self.mem_available)}/{gib(self.mem_total)}"
        )


async def snapshot(info) -> Snapshot:
    import asyncio

    paths = ("proc/stat", "proc/1/stat", f"{CGROUP}/memory.events", f"{CGROUP}/memory.peak",
             f"{CGROUP}/memory.max", "proc/meminfo")
    proc_stat, pid1, events, peak, mmax, meminfo = await asyncio.gather(*(read_text(info, p) for p in paths))
    ev = parse_flat_keyed(events)
    mi = parse_flat_keyed(meminfo)
    return Snapshot(
        container_start=parse_container_start(proc_stat, pid1),
        oom_kill=ev.get("oom_kill"),
        memory_peak=_int(peak),
        memory_max=_int(mmax),
        mem_total=mi.get("MemTotal"),
        mem_available=mi.get("MemAvailable"),
    )
