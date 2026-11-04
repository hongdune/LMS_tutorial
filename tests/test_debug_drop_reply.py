"""--debug-drop-reply-once: per kernel, the execute_reply and idle status of the first
non-silent execution never reach the browser (local Jupyter Server and Colab bridge).
Silent helper calls before it are passed through."""

import json
import socket
import subprocess
import time
import uuid

import pytest
import requests
import websocket

from lms_kernel.debug import ReplyDropper
from lms_kernel.launch import build_command

ORIGIN = "http://lms.test:3000"
TOKEN = "drop-reply-test-token"


def _m(msg_type, parent_id, parent_type="execute_request", state=None):
    msg = {"header": {"msg_type": msg_type, "msg_id": uuid.uuid4().hex},
           "parent_header": {"msg_id": parent_id, "msg_type": parent_type}, "content": {}}
    if state:
        msg["content"]["execution_state"] = state
    return msg


def test_reply_dropper_unit():
    d = ReplyDropper()
    # silent execution (no execute_input): passed through
    assert not d.should_drop("k1", _m("status", "s", state="busy"))
    assert not d.should_drop("k1", _m("execute_reply", "s"))
    assert not d.should_drop("k1", _m("status", "s", state="idle"))
    # first cell: execute_input marks it. idle may arrive before the reply;
    # busy, stream and other kernels are untouched
    assert not d.should_drop("k1", _m("status", "a", state="busy"))
    assert not d.should_drop("k1", _m("execute_input", "a"))
    assert not d.should_drop("k1", _m("stream", "a"))
    assert not d.should_drop("k1", _m("status", "x", parent_type="kernel_info_request", state="idle"))
    assert d.should_drop("k1", _m("status", "a", state="idle"))
    assert not d.should_drop("k1", _m("execute_reply", "b"))  # other execution: kept
    assert d.should_drop("k1", _m("execute_reply", "a"))
    # only once per kernel
    assert not d.should_drop("k1", _m("execute_reply", "c"))
    assert not d.should_drop("k1", _m("status", "c", state="idle"))
    # every kernel gets its own drop
    assert not d.should_drop("k2", _m("execute_input", "z"))
    assert d.should_drop("k2", _m("execute_reply", "z"))
    assert d.should_drop("k2", _m("status", "z", state="idle"))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _execute(ws, code, wait_extra=3.0, silent=False):
    """Send code; return message types for it, waiting `wait_extra`s after the stream."""
    mid = uuid.uuid4().hex
    ws.send(json.dumps({
        "header": {"msg_id": mid, "msg_type": "execute_request", "username": "t",
                   "session": uuid.uuid4().hex, "version": "5.3", "date": ""},
        "parent_header": {}, "metadata": {}, "channel": "shell",
        "content": {"code": code, "silent": silent, "store_history": not silent,
                    "user_expressions": {}, "allow_stdin": False},
    }))
    types, deadline = [], time.time() + 60
    while time.time() < deadline:
        try:
            m = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            break
        if m.get("parent_header", {}).get("msg_id") != mid:
            continue
        types.append((m["msg_type"], m["content"].get("execution_state")))
        if m["msg_type"] == "stream":
            ws.settimeout(wait_extra)
        if ("execute_reply", None) in types and ("status", "idle") in types:
            break
    ws.settimeout(60)
    return types


def check_first_reply_dropped(base, ws_base, headers, ws_kw, token=TOKEN):
    r = requests.post(f"{base}/api/kernels", headers=headers, json={"name": "python3"}, timeout=60)
    kid = r.json()["id"]
    try:
        ws = websocket.create_connection(
            f"{ws_base}/api/kernels/{kid}/channels?session_id=s1&token={token}", timeout=60, **ws_kw)
        # The LMS frontend calls info() (silent) right after connecting: not the one dropped.
        helper = _execute(ws, "", silent=True)
        assert ("execute_reply", None) in helper and ("status", "idle") in helper
        first = _execute(ws, "print(1)")
        assert ("stream", None) in first
        assert ("execute_reply", None) not in first and ("status", "idle") not in first
        # The kernel really is idle: what the LMS frontend polls to end the cell.
        st = requests.get(f"{base}/api/kernels/{kid}", headers=headers, timeout=10).json()
        assert st["execution_state"] == "idle"
        second = _execute(ws, "print(2)")
        assert ("execute_reply", None) in second and ("status", "idle") in second
        ws.close()
    finally:
        requests.delete(f"{base}/api/kernels/{kid}", headers=headers, timeout=30)


def test_local_runtime_drops_first_reply_once():
    port = _free_port()
    proc = subprocess.Popen(build_command(ORIGIN, port, TOKEN, debug_drop_reply_once=True),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            try:
                requests.get(f"{base}/api", timeout=1)
                break
            except requests.ConnectionError:
                if time.time() > deadline:
                    pytest.fail("Jupyter Server did not start")
                time.sleep(0.2)
        check_first_reply_dropped(base, f"ws://127.0.0.1:{port}",
                                  {"Authorization": f"token {TOKEN}"}, {"origin": ORIGIN})
    finally:
        proc.terminate()
        proc.wait(timeout=10)
