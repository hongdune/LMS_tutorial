"""Contract 2.1: a real Jupyter Server started via build_command culls idle kernels.

The timeout and interval are shortened by replacing those two arguments in the built
command (traitlets rejects repeated values); cull_connected/cull_busy stay as built.
"""

import json
import socket
import subprocess
import time
import uuid

import pytest
import requests
import websocket

from lms_kernel.launch import build_command

ORIGIN = "http://lms.test:3000"
TOKEN = "idle-cull-test-token"
IDLE_SECONDS = 3


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def runtime():
    port = _free_port()
    shortened = {
        "--MappingKernelManager.cull_idle_timeout": IDLE_SECONDS,
        "--MappingKernelManager.cull_interval": 1,
    }
    cmd = build_command(ORIGIN, port, TOKEN, idle_timeout_minutes=120)
    for key, value in shortened.items():
        (i,) = [n for n, arg in enumerate(cmd) if arg.startswith(key + "=")]
        cmd[i] = f"{key}={value}"
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            requests.get(f"{base}/api", timeout=1)
            break
        except requests.ConnectionError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("Jupyter Server did not start")
    yield base, port
    proc.terminate()
    proc.wait(timeout=10)


def _auth():
    return {"Authorization": f"token {TOKEN}"}


def _new_kernel(base):
    r = requests.post(f"{base}/api/kernels", headers=_auth(), json={"name": "python3"}, timeout=30)
    assert r.status_code == 201
    return r.json()["id"]


def _exists(base, kid):
    r = requests.get(f"{base}/api/kernels/{kid}", headers=_auth(), timeout=5)
    assert r.status_code in (200, 404)
    return r.status_code == 200


def _connect(port, kid):
    ws = websocket.create_connection(
        f"ws://127.0.0.1:{port}/api/kernels/{kid}/channels?session_id={uuid.uuid4().hex}",
        header=[f"Authorization: token {TOKEN}"],
        origin=ORIGIN,
        timeout=30,
    )
    return ws


def _execute(ws, code):
    msg_id = uuid.uuid4().hex
    ws.send(json.dumps({
        "header": {"msg_id": msg_id, "msg_type": "execute_request", "username": "t",
                   "session": uuid.uuid4().hex, "version": "5.3"},
        "parent_header": {}, "metadata": {}, "channel": "shell",
        "content": {"code": code, "silent": False, "store_history": False,
                    "user_expressions": {}, "allow_stdin": False},
    }))
    return msg_id


def _wait_busy(ws, msg_id):
    while True:
        m = json.loads(ws.recv())
        if (m.get("parent_header", {}).get("msg_id") == msg_id
                and m["msg_type"] == "status"
                and m["content"]["execution_state"] == "busy"):
            return


def _wait_gone(base, kid, limit):
    deadline = time.time() + limit
    while time.time() < deadline:
        if not _exists(base, kid):
            return True
        time.sleep(0.5)
    return False


def test_idle_kernel_is_culled(runtime):
    base, _ = runtime
    kid = _new_kernel(base)
    assert _wait_gone(base, kid, IDLE_SECONDS + 15)


def test_idle_kernel_is_culled_even_while_connected(runtime):
    base, port = runtime
    kid = _new_kernel(base)
    ws = _connect(port, kid)
    try:
        assert _wait_gone(base, kid, IDLE_SECONDS + 15)
    finally:
        ws.close()


def test_busy_kernel_is_not_culled(runtime):
    base, port = runtime
    kid = _new_kernel(base)
    ws = _connect(port, kid)
    try:
        _wait_busy(ws, _execute(ws, "import time; time.sleep(12)"))
        # Several cull rounds pass well beyond the idle timeout while it runs.
        time.sleep(IDLE_SECONDS + 5)
        assert _exists(base, kid)
    finally:
        ws.close()
        requests.delete(f"{base}/api/kernels/{kid}", headers=_auth(), timeout=10)
