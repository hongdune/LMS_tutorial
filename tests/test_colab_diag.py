"""Parsing of the Colab VM files read when a kernel disappears (lms_kernel.colab_diag)
and the log helpers of the bridge. Samples are from a T4 runtime (2026-11-07)."""

from datetime import datetime, timezone

from lms_kernel.bridge import body_head, from_jupyter, jupyter_error
from lms_kernel.colab_diag import Snapshot, parse_container_start, parse_flat_keyed
from lms_kernel.colab_session import ProxyInfo

PROC_STAT = "cpu  4255 0 2167 238734 236 0 81 0 0 0\nintr 0\nctxt 1\nbtime 1794000000\nprocesses 900\n"
PID1_STAT = ("1 (docker-init) S 0 1 1 0 -1 4194560 211 0 0 0 0 0 0 0 20 0 1 0 "
             "70827 1028096 1 18446744073709551615 1 1 0 0 0 0 0 4096 0 0 0 0 17 0 0 0 0 0 0\n")


def test_container_start_from_procfs():
    assert parse_container_start(PROC_STAT, PID1_STAT) == datetime.fromtimestamp(1794000708.27, timezone.utc)
    assert parse_container_start(None, PID1_STAT) is None
    assert parse_container_start(PROC_STAT, "garbage") is None


def test_flat_keyed_files():
    events = parse_flat_keyed("low 0\nhigh 0\nmax 0\noom 2\noom_kill 1\noom_group_kill 0\n")
    assert events["oom_kill"] == 1 and events["oom"] == 2
    meminfo = parse_flat_keyed("MemTotal:       13286932 kB\nMemAvailable:   12143736 kB\nHugePages_Total:       0\n")
    assert meminfo == {"MemTotal": 13286932 * 1024, "MemAvailable": 12143736 * 1024, "HugePages_Total": 0}
    assert parse_flat_keyed(None) == {}


def test_snapshot_summary_handles_missing_values():
    assert Snapshot().summary() == "container_start=? oom_kill=? kernels_mem_peak=?/max vm_available=?/?"


def test_jupyter_and_proxy_404_are_told_apart():
    assert jupyter_error(b'{"message": "Kernel does not exist: x", "reason": null}') == "Kernel does not exist: x"
    assert from_jupyter(b'{"message": "Kernel does not exist: x", "reason": null}')
    assert from_jupyter(b"<!DOCTYPE HTML><html><head><title>Jupyter Server</title></head><body>404</body></html>")
    assert not from_jupyter(b"")  # the Colab proxy rejecting the token
    assert not from_jupyter(b"<html><body>Error 404 (Not Found)!!1</body></html>")


def test_body_head_masks_tokens_and_strips_html():
    info = ProxyInfo(url="https://x", token="proxy-secret-token", expires_at=None)
    assert "proxy-secret-token" not in body_head(b"bad proxy-secret-token here", info)
    assert "ya29" not in body_head(b"access ya29.a0AfB_byC-secret-value", None)
    html = b"<html><head><title>Jupyter Server</title><script>x()</script></head><body><h1>404 : Not Found</h1></body></html>"
    assert body_head(html) == "'404 : Not Found'"
    assert body_head(b"word " * 100).endswith("…'")
