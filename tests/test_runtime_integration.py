"""Start a real Jupyter Server via build_command and exercise the contract paths."""

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
TOKEN = "integration-test-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def runtime():
    port = _free_port()
    proc = subprocess.Popen(
        build_command(ORIGIN, port, TOKEN),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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


def test_rejects_missing_token(runtime):
    base, _ = runtime
    assert requests.get(f"{base}/api/kernels", timeout=5).status_code == 403


def test_cors_header_is_fixed_to_allowed_origin(runtime):
    base, _ = runtime
    ok = requests.get(f"{base}/api/kernels", headers={**_auth(), "Origin": ORIGIN}, timeout=5)
    assert ok.status_code == 200
    assert ok.headers.get("Access-Control-Allow-Origin") == ORIGIN


def test_rest_rejects_foreign_origin_even_with_token(runtime):
    base, _ = runtime
    r = requests.post(
        f"{base}/api/kernels",
        headers={**_auth(), "Origin": "http://evil.test"},
        json={"name": "python3"},
        timeout=10,
    )
    # Jupyter answers blocked cross-origin API requests with 404.
    assert r.status_code == 404


def test_script_without_origin_is_allowed(runtime):
    base, _ = runtime
    assert requests.get(f"{base}/api/kernels", headers=_auth(), timeout=5).status_code == 200


def _msg(msg_type: str, content: dict) -> dict:
    return {
        "header": {
            "msg_id": uuid.uuid4().hex,
            "msg_type": msg_type,
            "username": "test",
            "session": uuid.uuid4().hex,
            "version": "5.3",
            "date": "",
        },
        "parent_header": {},
        "metadata": {},
        "content": content,
        "channel": "shell",
    }


def test_execute_over_websocket(runtime):
    base, port = runtime
    r = requests.post(f"{base}/api/kernels", headers=_auth(), json={"name": "python3"}, timeout=10)
    assert r.status_code == 201
    kernel_id = r.json()["id"]
    try:
        # Browsers cannot set headers on WebSocket, so the token goes in the query string.
        ws = websocket.create_connection(
            f"ws://127.0.0.1:{port}/api/kernels/{kernel_id}/channels?token={TOKEN}",
            origin=ORIGIN,
            timeout=30,
        )
        req = _msg("execute_request", {
            "code": "print('hello')\n1 + 1",
            "silent": False,
            "store_history": True,
            "user_expressions": {},
            "allow_stdin": False,
        })
        ws.send(json.dumps(req))
        seen = {}
        while True:
            m = json.loads(ws.recv())
            if m.get("parent_header", {}).get("msg_id") != req["header"]["msg_id"]:
                continue
            seen[m["msg_type"]] = m["content"]
            if m["msg_type"] == "status" and m["content"]["execution_state"] == "idle":
                break
        ws.close()
        assert seen["stream"]["text"] == "hello\n"
        assert seen["execute_result"]["data"]["text/plain"] == "2"
    finally:
        requests.delete(f"{base}/api/kernels/{kernel_id}", headers=_auth(), timeout=10)


def test_websocket_rejects_foreign_origin(runtime):
    base, port = runtime
    r = requests.post(f"{base}/api/kernels", headers=_auth(), json={"name": "python3"}, timeout=10)
    kernel_id = r.json()["id"]
    try:
        with pytest.raises(websocket.WebSocketBadStatusException):
            websocket.create_connection(
                f"ws://127.0.0.1:{port}/api/kernels/{kernel_id}/channels?token={TOKEN}",
                origin="http://evil.test",
                timeout=10,
            )
    finally:
        requests.delete(f"{base}/api/kernels/{kernel_id}", headers=_auth(), timeout=10)


def test_error_responses_keep_cors_header(runtime):
    """Errors must carry ACAO too, or the browser only sees a CORS failure and the LMS
    cannot tell "kernel gone (404)" from "runtime unreachable" (lms_kernel/server.py)."""
    base, _ = runtime
    missing = requests.get(f"{base}/api/kernels/00000000-0000-0000-0000-000000000000",
                           headers={**_auth(), "Origin": ORIGIN}, timeout=5)
    assert missing.status_code == 404
    assert missing.headers.get("Access-Control-Allow-Origin") == ORIGIN
    bad = requests.get(f"{base}/api/kernels",
                       headers={"Authorization": "token wrong", "Origin": ORIGIN}, timeout=5)
    assert bad.status_code == 403
    assert bad.headers.get("Access-Control-Allow-Origin") == ORIGIN


def test_lms_runtime_info(runtime):
    base, _ = runtime
    r = requests.get(f"{base}/lms/runtime", headers={**_auth(), "Origin": ORIGIN}, timeout=5)
    assert r.status_code == 200
    assert r.headers.get("Access-Control-Allow-Origin") == ORIGIN
    assert r.json() == {"version": 1, "kind": "local", "lms_runtime": "0.6.1", "contract": "0.12",
                        "colab": None}
    assert r.elapsed.total_seconds() < 1


def test_lms_runtime_info_auth_and_origin(runtime):
    base, _ = runtime
    assert requests.get(f"{base}/lms/runtime", timeout=5).status_code == 403
    r = requests.get(f"{base}/lms/runtime", headers={**_auth(), "Origin": "http://evil.test"}, timeout=5)
    assert r.status_code == 404
    r = requests.options(f"{base}/lms/runtime", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization"}, timeout=5)
    assert r.status_code in (200, 204) and r.headers.get("Access-Control-Allow-Origin") == ORIGIN


def test_colab_usage_is_not_served_locally(runtime):
    base, _ = runtime
    r = requests.get(f"{base}/lms/colab/usage", headers={**_auth(), "Origin": ORIGIN}, timeout=5)
    assert r.status_code == 404
    assert r.headers.get("Access-Control-Allow-Origin") == ORIGIN
