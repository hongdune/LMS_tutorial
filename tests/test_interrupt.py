"""Ctrl+C handling of `lms-runtime`: one Ctrl+C only warns, a second one within the
window stops the runtime (students press Ctrl+C to copy the token from the terminal)."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from lms_kernel.launch import InterruptGate

ORIGIN = "http://lms.test:3000"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_gate_needs_a_second_press_within_the_window(capsys):
    now = [100.0]
    gate = InterruptGate(window=5, clock=lambda: now[0])
    assert gate.press() is False
    assert "한 번 더" in capsys.readouterr().err
    now[0] += 6  # too late: counts as a new first press
    assert gate.press() is False
    now[0] += 4.9
    assert gate.press() is True
    assert "종료합니다" in capsys.readouterr().err


def spawn_runtime(args, stderr_path: Path):
    """Start lms-runtime in its own process group (like a terminal's foreground job)
    and return (proc, token)."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "lms_kernel.launch", *args],
        stdout=subprocess.PIPE, stderr=stderr_path.open("w"), text=True,
        start_new_session=True,
    )
    lines = []
    while not any(l.startswith("  Token:") for l in lines):
        line = proc.stdout.readline()
        if not line:
            proc.wait(timeout=10)
            pytest.fail(stderr_path.read_text())
        lines.append(line)
    token = [l for l in lines if l.startswith("  Token:")][0].split(":", 1)[1].strip()
    return proc, token


def wait_ready(base, token, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{base}/api/kernels", headers={"Authorization": f"token {token}"}, timeout=2)
            if r.status_code == 200:
                return
        except requests.ConnectionError:
            pass
        time.sleep(0.2)
    pytest.fail("runtime did not start")


def ctrl_c(proc):
    """What the terminal does on Ctrl+C: SIGINT to the whole foreground process group."""
    os.killpg(proc.pid, signal.SIGINT)


def _kernel_alive(kernel_id) -> bool:
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if f"kernel-{kernel_id}.json".encode() in cmdline:
            return True
    return False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_local_runtime_survives_one_ctrl_c_and_stops_on_two(tmp_path):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    err = tmp_path / "stderr.txt"
    proc, token = spawn_runtime(["--origin", ORIGIN, "--port", str(port)], err)
    auth = {"Authorization": f"token {token}"}
    try:
        wait_ready(base, token)
        kernel_id = requests.post(f"{base}/api/kernels", headers=auth, json={"name": "python3"},
                                  timeout=30).json()["id"]

        ctrl_c(proc)
        time.sleep(1)
        assert proc.poll() is None
        assert requests.get(f"{base}/api/kernels/{kernel_id}", headers=auth, timeout=5).status_code == 200
        assert "한 번 더" in err.read_text()

        time.sleep(5)  # the window has passed: this is a new first press
        ctrl_c(proc)
        time.sleep(0.5)
        assert proc.poll() is None
        ctrl_c(proc)
        assert proc.wait(timeout=30) == 0
        assert "종료합니다" in err.read_text()
        with pytest.raises(requests.ConnectionError):
            requests.get(f"{base}/api", timeout=2)
        deadline = time.time() + 10
        while _kernel_alive(kernel_id) and time.time() < deadline:
            time.sleep(0.2)
        assert not _kernel_alive(kernel_id), "Jupyter Server did not shut its kernel down"
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
