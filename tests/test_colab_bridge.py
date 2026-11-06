"""Colab bridge against a fake upstream: a real Jupyter Server that authenticates like the
Colab runtime proxy, plus a fake `colab` CLI that rewrites sessions.json."""

import asyncio
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
import websocket

from lms_kernel.bridge import is_colab_only_message, start
from lms_kernel.colab_session import ColabSession

TESTS = Path(__file__).parent
FAKE_CLI = str(TESTS / "fake_colab_cli.py")
ORIGIN = "https://lms.test"
LMS_TOKEN = "bridge-test-lms-token"
PROXY_TOKEN = "bridge-test-proxy-token"
SESSION = "s1"

COLAB_REQUEST_CODE = """
from IPython import get_ipython
_k = get_ipython().kernel
_k.session.send(_k.iopub_socket, "colab_request", {"request": {"authType": "dfs_ephemeral"}},
                parent=_k.get_parent(), ident=b"colab_request")
del _k
print(1 + 1)
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


UPSTREAM_LOG = {}  # base URL -> log file of that fake upstream (for failure diagnostics)


def _start_upstream(root_dir):
    port = _free_port()
    log = Path(root_dir).parent / f"upstream-{port}.log"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(TESTS), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "jupyter_server", "--no-browser",
            f"--port={port}", "--ServerApp.ip=127.0.0.1",
            f"--IdentityProvider.token={PROXY_TOKEN}",
            "--ServerApp.identity_provider_class=fake_colab_proxy.FakeColabProxyIdentityProvider",
            # Fake upstream only; lets the suite run in a root-only WSL like lms-runtime does.
            "--ServerApp.allow_root=True",
            f"--ServerApp.root_dir={root_dir}",  # Contents API writes go here, never the repo
        ],
        stdout=subprocess.DEVNULL,
        stderr=log.open("w"),
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    UPSTREAM_LOG[base] = log
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            requests.get(f"{base}/api", timeout=1)
            return proc, base
        except requests.ConnectionError:
            time.sleep(0.2)
    proc.kill()
    pytest.fail("fake upstream did not start")


def _session_entry(url, token, expires_in=timedelta(hours=1)):
    return {
        SESSION: {
            "name": SESSION,
            "token": token,
            "url": url,
            "endpoint": "fake-endpoint",
            "token_expires_at": (datetime.now(timezone.utc) + expires_in).isoformat().replace("+00:00", "Z"),
            "variant": "GPU",
            "accelerator": "T4",
        }
    }


class Bridge:
    """Runs the bridge on its own event loop thread, like `lms-runtime --colab` does."""

    def __init__(self, sessions_path: Path, min_refresh_interval=0.0, upstream_reconnect=True,
                 debug_drop_upstream_after=None, debug_drop_reply_once=False):
        self.sessions_path = sessions_path
        self.session = ColabSession(
            SESSION, sessions_path=str(sessions_path), colab_cmd=FAKE_CLI,
            min_refresh_interval=min_refresh_interval,
        )
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.ws_base = f"ws://127.0.0.1:{self.port}"
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)

            async def setup():
                self.server = start(
                    ORIGIN, self.port, LMS_TOKEN, self.session, upstream_reconnect=upstream_reconnect,
                    debug_drop_upstream_after=debug_drop_upstream_after,
                    debug_drop_reply_once=debug_drop_reply_once,
                )

            self.loop.run_until_complete(setup())
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(10)

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(60)

    def calls(self) -> int:
        p = Path(str(self.sessions_path) + ".calls")
        return len(p.read_text().splitlines()) if p.exists() else 0

    def stop(self):
        self.loop.call_soon_threadsafe(self.server.stop)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


def _write(path: Path, data):
    path.write_text(json.dumps(data))


@pytest.fixture(scope="module")
def upstream_root(tmp_path_factory):
    return tmp_path_factory.mktemp("colab-vm")


@pytest.fixture(scope="module")
def upstream(upstream_root):
    proc, base = _start_upstream(upstream_root)
    yield base
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def make_bridge(tmp_path):
    bridges = []

    def factory(entry, next_entry=None, **kw):
        path = tmp_path / f"sessions-{len(bridges)}.json"
        _write(path, entry)
        if next_entry is not None:
            _write(Path(str(path) + ".next"), next_entry)
        b = Bridge(path, **kw)
        bridges.append(b)
        return b

    yield factory
    for b in bridges:
        b.stop()


@pytest.fixture
def bridge(make_bridge, upstream):
    b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
    b.call(b.session.refresh())
    return b


def _auth(**extra):
    return {"Authorization": f"token {LMS_TOKEN}", **extra}


def _msg(msg_type, content):
    return {
        "header": {"msg_id": uuid.uuid4().hex, "msg_type": msg_type, "username": "test",
                   "session": uuid.uuid4().hex, "version": "5.3", "date": ""},
        "parent_header": {}, "metadata": {}, "content": content, "channel": "shell",
    }


def _upstream_diagnostics(upstream, kid) -> str:
    """Kernel state and recent log lines of the fake upstream, for a failing CI run
    (job logs need sign-in; pytest's E lines become public annotations)."""
    try:
        r = requests.get(f"{upstream}/api/kernels/{kid}",
                         headers={"X-Colab-Runtime-Proxy-Token": PROXY_TOKEN}, timeout=5)
        state = f"{r.status_code} {r.text[:300]}"
    except requests.RequestException as e:
        state = type(e).__name__
    log = UPSTREAM_LOG.get(upstream)
    lines = log.read_text(errors="replace").splitlines() if log and log.exists() else []
    interesting = [l for l in lines if kid in l or any(k in l for k in ("[W ", "[E ", "[C ", "Error", "Traceback"))]
    return "upstream kernel: " + state + "\nupstream log:\n" + "\n".join(interesting[-20:])


def _wait_ready(ws, limit=60, diagnostics=None):
    """kernel_info_request until answered, like @jupyterlab/services. On a slow runner a
    fresh kernel may not answer a request sent right after it starts (see test_idle_cull)."""
    deadline = time.time() + limit
    try:
        while time.time() < deadline:
            req = _msg("kernel_info_request", {})
            ws.send(json.dumps(req))
            ws.settimeout(2)
            try:
                while True:
                    m = json.loads(ws.recv())
                    if (m.get("parent_header", {}).get("msg_id") == req["header"]["msg_id"]
                            and m["msg_type"] == "kernel_info_reply"):
                        return
            except websocket.WebSocketTimeoutException:
                continue
    finally:
        ws.settimeout(30)
    pytest.fail("kernel never answered kernel_info_request"
                + ("\n" + diagnostics() if diagnostics else ""))


def _execute(ws, code):
    req = _msg("execute_request", {"code": code, "silent": False, "store_history": True,
                                   "user_expressions": {}, "allow_stdin": False})
    ws.send(json.dumps(req))
    seen = []
    while True:
        m = json.loads(ws.recv())
        if m.get("parent_header", {}).get("msg_id") != req["header"]["msg_id"]:
            continue
        seen.append(m)
        if m["msg_type"] == "status" and m["content"]["execution_state"] == "idle":
            return seen


def _new_kernel(base):
    r = requests.post(f"{base}/api/kernels", headers=_auth(), json={"name": "python3"}, timeout=60)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- auth, origin, CORS -------------------------------------------------------------

def test_rejects_missing_or_wrong_token(bridge):
    assert requests.get(f"{bridge.base}/api/kernels", timeout=5).status_code == 403
    r = requests.get(f"{bridge.base}/api/kernels", headers={"Authorization": "token nope"}, timeout=5)
    assert r.status_code == 403


def test_cors_header_is_fixed_to_allowed_origin(bridge):
    ok = requests.get(f"{bridge.base}/api/kernels", headers=_auth(Origin=ORIGIN), timeout=10)
    assert ok.status_code == 200
    assert ok.headers["Access-Control-Allow-Origin"] == ORIGIN
    bad = requests.get(f"{bridge.base}/api/kernels", headers=_auth(Origin="https://evil.test"), timeout=10)
    assert bad.headers["Access-Control-Allow-Origin"] == ORIGIN


def test_rest_rejects_foreign_origin_even_with_token(bridge):
    r = requests.post(f"{bridge.base}/api/kernels", headers=_auth(Origin="https://evil.test"),
                      json={"name": "python3"}, timeout=10)
    assert r.status_code == 404


def test_preflight(bridge):
    r = requests.options(f"{bridge.base}/api/kernels", headers={
        "Origin": ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }, timeout=5)
    assert r.status_code == 204
    assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "authorization" in r.headers["Access-Control-Allow-Headers"]
    assert "DELETE" in r.headers["Access-Control-Allow-Methods"]


def test_non_contract_paths_are_not_exposed(bridge):
    for path in ("/api/contents", "/api/sessions", "/api/terminals", "/tree", "/api/kernels/x/y"):
        assert requests.get(f"{bridge.base}{path}", headers=_auth(), timeout=5).status_code == 404


# --- REST relay -----------------------------------------------------------------------

def test_kernel_lifecycle(bridge):
    specs = requests.get(f"{bridge.base}/api/kernelspecs", headers=_auth(), timeout=10)
    assert specs.status_code == 200 and "python3" in specs.json()["kernelspecs"]

    kid = _new_kernel(bridge.base)
    k = f"{bridge.base}/api/kernels/{kid}"
    assert requests.get(k, headers=_auth(), timeout=10).json()["id"] == kid
    assert kid in [x["id"] for x in requests.get(f"{bridge.base}/api/kernels", headers=_auth(), timeout=10).json()]
    assert requests.post(f"{k}/interrupt", headers=_auth(), timeout=30).status_code == 204
    assert requests.post(f"{k}/restart", headers=_auth(), timeout=60).status_code == 200
    assert requests.delete(k, headers=_auth(), timeout=30).status_code == 204
    assert requests.get(k, headers=_auth(), timeout=10).status_code == 404


# --- WebSocket relay -------------------------------------------------------------------

def test_execute_over_websocket_and_drop_colab_messages(bridge, upstream, caplog):
    caplog.set_level(logging.DEBUG)
    kid = _new_kernel(bridge.base)
    try:
        # Sanity check: the upstream really emits colab_request for this code.
        direct = websocket.create_connection(
            f"{upstream.replace('http', 'ws')}/api/kernels/{kid}/channels?colab-runtime-proxy-token={PROXY_TOKEN}",
            timeout=60)
        assert "colab_request" in [m["msg_type"] for m in _execute(direct, COLAB_REQUEST_CODE)]
        direct.close()

        ws = websocket.create_connection(
            f"{bridge.ws_base}/api/kernels/{kid}/channels?session_id=abc&token={LMS_TOKEN}",
            origin=ORIGIN, timeout=60)
        seen = _execute(ws, COLAB_REQUEST_CODE)
        types = [m["msg_type"] for m in seen]
        assert "colab_request" not in types
        assert any(m["msg_type"] == "stream" and m["content"]["text"] == "2\n" for m in seen)
        ws.close()
    finally:
        requests.delete(f"{bridge.base}/api/kernels/{kid}", headers=_auth(), timeout=30)
    logs = caplog.text
    assert LMS_TOKEN not in logs and PROXY_TOKEN not in logs


def test_websocket_rejects_foreign_origin_and_missing_token(bridge):
    kid = _new_kernel(bridge.base)
    try:
        with pytest.raises(websocket.WebSocketBadStatusException):
            websocket.create_connection(
                f"{bridge.ws_base}/api/kernels/{kid}/channels?token={LMS_TOKEN}",
                origin="https://evil.test", timeout=10)
        with pytest.raises(websocket.WebSocketBadStatusException):
            websocket.create_connection(f"{bridge.ws_base}/api/kernels/{kid}/channels", origin=ORIGIN, timeout=10)
    finally:
        requests.delete(f"{bridge.base}/api/kernels/{kid}", headers=_auth(), timeout=30)


# --- token refresh through the CLI ---------------------------------------------------------

def test_expired_token_is_refreshed_via_cli(make_bridge, upstream):
    b = make_bridge(
        _session_entry(upstream, "stale-token", expires_in=timedelta(minutes=-1)),
        next_entry=_session_entry(upstream, PROXY_TOKEN),
    )
    assert requests.get(f"{b.base}/api/kernels", headers=_auth(), timeout=30).status_code == 200
    assert b.calls() == 1
    # Fresh token: no further CLI calls.
    assert requests.get(f"{b.base}/api/kernels", headers=_auth(), timeout=30).status_code == 200
    assert b.calls() == 1


def test_upstream_auth_failure_triggers_one_refresh(make_bridge, upstream):
    # The bridge holds a token the proxy no longer accepts (rotated early); the CLI has a new one.
    b = make_bridge(
        _session_entry(upstream, "rotated-away"),
        next_entry=_session_entry(upstream, PROXY_TOKEN),
    )
    b.call(_load_without_cli(b.session))
    assert requests.get(f"{b.base}/api/kernels", headers=_auth(), timeout=30).status_code == 200
    assert b.calls() == 1


async def _load_without_cli(session):
    from lms_kernel.colab_session import read_session
    session._info = read_session(session.sessions_path, session.name)


def _wait_closed(ws, limit=30):
    """Read until the server closes the socket; return seconds waited."""
    t0 = time.time()
    ws.settimeout(limit)
    try:
        while ws.recv():
            pass
    except websocket.WebSocketConnectionClosedException:
        pass
    return time.time() - t0


# --- Colab runtime gone / unreachable -------------------------------------------------------

def test_session_gone_gives_503_and_closes_websocket(make_bridge, upstream):
    b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
    b.call(b.session.refresh())
    kid = _new_kernel(b.base)
    try:
        _write(Path(str(b.sessions_path) + ".next"), {})  # CLI prunes the session
        with pytest.raises(Exception):
            b.call(b.session.refresh())
        r = requests.get(f"{b.base}/api/kernels/{kid}", headers=_auth(Origin=ORIGIN), timeout=10)
        assert r.status_code == 503
        assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert "종료" in r.json()["message"]
        ws = websocket.create_connection(
            f"{b.ws_base}/api/kernels/{kid}/channels?token={LMS_TOKEN}", origin=ORIGIN, timeout=10)
        assert _wait_closed(ws) < 10
    finally:
        requests.delete(f"{upstream}/api/kernels/{kid}",
                        headers={"X-Colab-Runtime-Proxy-Token": PROXY_TOKEN}, timeout=30)


def test_colab_stop_is_noticed_without_cli(make_bridge, upstream):
    from lms_kernel.bridge import keep_fresh

    b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
    b.call(b.session.refresh())
    calls = b.calls()
    _write(b.sessions_path, {})  # what `colab stop` leaves behind
    b.call(asyncio.wait_for(keep_fresh(b.session, interval=0.1), 5))
    assert b.session.gone
    assert b.calls() == calls  # no extra CLI call (each one makes the CLI log secrets)
    assert requests.get(f"{b.base}/api/kernels", headers=_auth(), timeout=10).status_code == 503


# --- idle stop (contract 2.1) --------------------------------------------------------

def _stops(b) -> int:
    p = Path(str(b.sessions_path) + ".stops")
    return len(p.read_text().splitlines()) if p.exists() else 0


def test_idle_stop_returns_vm_and_answers_503(make_bridge, upstream):
    from lms_kernel.bridge import stop_when_idle

    b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
    b.call(b.session.refresh())
    t0 = time.time()
    assert b.call(stop_when_idle(b.session, idle_timeout=1.0, poll_interval=0.2)) is True
    assert 1.0 <= time.time() - t0 < 10
    assert _stops(b) == 1
    assert SESSION not in json.loads(b.sessions_path.read_text())
    r = requests.get(f"{b.base}/api/kernels", headers=_auth(Origin=ORIGIN), timeout=10)
    assert r.status_code == 503


def test_idle_stop_waits_for_busy_kernel_and_open_websocket_does_not_count(make_bridge, upstream):
    from lms_kernel.bridge import stop_when_idle

    b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
    b.call(b.session.refresh())
    kid = _new_kernel(b.base)
    ws = websocket.create_connection(
        f"{b.ws_base}/api/kernels/{kid}/channels?token={LMS_TOKEN}&session_id={uuid.uuid4().hex}",
        origin=ORIGIN, timeout=30)
    try:
        _wait_ready(ws, diagnostics=lambda: _upstream_diagnostics(upstream, kid))
        _execute(ws, "import time")
        msg_id = _send_execute(ws, "time.sleep(4)")
        while True:  # wait until the kernel reports busy for this request
            m = json.loads(ws.recv())
            if (m.get("parent_header", {}).get("msg_id") == msg_id and m["msg_type"] == "status"
                    and m["content"]["execution_state"] == "busy"):
                break
        t0 = time.time()
        fut = asyncio.run_coroutine_threadsafe(
            stop_when_idle(b.session, idle_timeout=1.5, poll_interval=0.2), b.loop)
        # Busy for ~4s, then idle for 1.5s with the WebSocket still open.
        assert fut.result(30) is True
        assert time.time() - t0 >= 4.5
        assert _stops(b) == 1
    finally:
        ws.close()
        requests.delete(f"{upstream}/api/kernels/{kid}",
                        headers={"X-Colab-Runtime-Proxy-Token": PROXY_TOKEN}, timeout=30)


def test_idle_stop_timer_restarts_on_kernel_activity(make_bridge, upstream):
    from lms_kernel.bridge import stop_when_idle

    b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
    b.call(b.session.refresh())
    kid = _new_kernel(b.base)
    ws = websocket.create_connection(
        f"{b.ws_base}/api/kernels/{kid}/channels?token={LMS_TOKEN}&session_id={uuid.uuid4().hex}",
        origin=ORIGIN, timeout=30)
    try:
        # Start the timer only once the kernel answers: on a slow runner a kernel that is
        # still starting can outlast the 4s timeout, and the VM is returned mid-test.
        _wait_ready(ws, diagnostics=lambda: _upstream_diagnostics(upstream, kid))
        _execute(ws, "1 + 1")
        fut = asyncio.run_coroutine_threadsafe(
            stop_when_idle(b.session, idle_timeout=4.0, poll_interval=0.2), b.loop)
        t0 = time.time()
        while time.time() - t0 < 6:  # short cells every ~0.5s: far below the 4s timeout,
            _execute(ws, "1 + 1")    # with headroom for slow CI runners
            time.sleep(0.5)
        assert not fut.done() and _stops(b) == 0
        assert fut.result(30) is True
        assert time.time() - t0 >= 6 + 4.0 - 0.5
    finally:
        ws.close()
        requests.delete(f"{upstream}/api/kernels/{kid}",
                        headers={"X-Colab-Runtime-Proxy-Token": PROXY_TOKEN}, timeout=30)


def test_idle_stop_retries_when_cli_fails(make_bridge, upstream, capfd):
    from lms_kernel.bridge import stop_when_idle

    b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
    b.call(b.session.refresh())
    fail = Path(str(b.sessions_path) + ".fail")
    fail.touch()
    fut = asyncio.run_coroutine_threadsafe(
        stop_when_idle(b.session, idle_timeout=0.5, poll_interval=0.2, stop_retry=1.0), b.loop)
    deadline = time.time() + 10
    while _stops(b) < 1 and time.time() < deadline:
        time.sleep(0.1)
    assert _stops(b) == 1 and not b.session.gone
    fail.unlink()
    assert fut.result(30) is True
    assert _stops(b) == 2
    assert f"colab stop -s {SESSION}" in capfd.readouterr().err


def test_upstream_down_gives_502_and_closes_websocket(make_bridge, tmp_path):
    proc, base = _start_upstream(tmp_path)
    try:
        b = make_bridge(_session_entry(base, PROXY_TOKEN), min_refresh_interval=3600)
        b.call(b.session.refresh())
        kid = _new_kernel(b.base)
        ws = websocket.create_connection(
            f"{b.ws_base}/api/kernels/{kid}/channels?token={LMS_TOKEN}", origin=ORIGIN, timeout=30)
        proc.kill()
        proc.wait(timeout=10)
        assert _wait_closed(ws) < 30
        r = requests.get(f"{b.base}/api/kernels/{kid}", headers=_auth(), timeout=30)
        assert r.status_code == 502
        assert r.json()["message"]
    finally:
        if proc.poll() is None:
            proc.kill()


def test_hung_upstream_closes_websocket_via_heartbeat(make_bridge, monkeypatch, tmp_path):
    import signal
    import lms_kernel.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "HEARTBEAT_INTERVAL", 1.0)
    monkeypatch.setattr(bridge_mod, "HEARTBEAT_TIMEOUT", 1.0)
    proc, base = _start_upstream(tmp_path)
    try:
        b = make_bridge(_session_entry(base, PROXY_TOKEN), min_refresh_interval=3600)
        b.call(b.session.refresh())
        kid = _new_kernel(b.base)
        ws = websocket.create_connection(
            f"{b.ws_base}/api/kernels/{kid}/channels?token={LMS_TOKEN}", origin=ORIGIN, timeout=30)
        # Healthy but idle: heartbeats succeed and the socket stays open.
        ws.settimeout(4)
        with pytest.raises(websocket.WebSocketTimeoutException):
            while True:
                ws.recv()
        assert ws.connected
        # Frozen runtime: TCP stays up but nothing answers.
        proc.send_signal(signal.SIGSTOP)
        assert _wait_closed(ws) < 15
    finally:
        proc.send_signal(signal.SIGCONT)
        proc.kill()


# --- WebSocket drops mid-execution (Colab proxy behaviour) ---------------------------------

class CuttableRelay:
    """TCP relay in front of the upstream that can drop every open connection at once,
    the way the Colab runtime proxy drops a WebSocket during a long execution."""

    def __init__(self, target_port: int):
        self.target_port = target_port
        self.conns: list[socket.socket] = []
        self.lock = threading.Lock()
        self.refuse = False  # drop new connections right away (runtime unreachable)
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        self.port = self.listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            if self.refuse:
                client.close()
                continue
            server = socket.create_connection(("127.0.0.1", self.target_port))
            with self.lock:
                self.conns += [client, server]
            for a, b in ((client, server), (server, client)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def _pipe(src, dst):
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def cut_all(self):
        with self.lock:
            conns, self.conns = self.conns, []
        for sock in conns:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def close(self):
        self.listener.close()
        self.cut_all()


SLOW_CODE = "import time\nfor i in range(8):\n    print(i, flush=True)\n    time.sleep(0.5)\n"


def _send_execute(ws, code):
    req = _msg("execute_request", {"code": code, "silent": False, "store_history": True,
                                   "user_expressions": {}, "allow_stdin": False})
    ws.send(json.dumps(req))
    return req["header"]["msg_id"]


def _collect(ws, msg_id, until_first_stream=False, reply_grace=3.0):
    """Messages for msg_id until idle and execute_reply (True) or the socket closes (False).

    idle (iopub) and execute_reply (shell) travel on different channels, so either may
    come first, especially in a replay after reconnecting; once idle is in, wait up to
    `reply_grace` seconds for the reply."""
    seen, idle_at = [], None
    try:
        while True:
            if idle_at is not None:
                left = reply_grace - (time.time() - idle_at)
                if left <= 0:
                    return seen, True
                ws.settimeout(left)
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                if idle_at is not None:
                    return seen, True
                raise
            except websocket.WebSocketConnectionClosedException:
                return seen, idle_at is not None
            if not raw:
                return seen, idle_at is not None
            m = json.loads(raw)
            if m.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            seen.append(m)
            if until_first_stream and m["msg_type"] == "stream":
                return seen, True
            if m["msg_type"] == "status" and m["content"]["execution_state"] == "idle":
                idle_at = time.time()
            if idle_at is not None and any(x["msg_type"] == "execute_reply" for x in seen):
                return seen, True
    finally:
        ws.settimeout(30)


def _printed(msgs):
    return "".join(m["content"]["text"] for m in msgs if m["msg_type"] == "stream")


@pytest.fixture
def relay(upstream):
    r = CuttableRelay(int(upstream.rsplit(":", 1)[1]))
    yield r
    r.close()


def test_browser_reconnect_with_same_session_id_replays_buffered_output(make_bridge, relay):
    """Experiment A: the bridge closes the browser WebSocket; the browser reconnects with the
    same session_id (what @jupyterlab/services does) and the upstream replays its buffer."""
    b = make_bridge(_session_entry(relay.url, PROXY_TOKEN), upstream_reconnect=False)
    b.call(b.session.refresh())
    kid = _new_kernel(b.base)
    url = f"{b.ws_base}/api/kernels/{kid}/channels?session_id=client-1&token={LMS_TOKEN}"
    try:
        ws = websocket.create_connection(url, origin=ORIGIN, timeout=30)
        mid = _send_execute(ws, SLOW_CODE)
        first, _ = _collect(ws, mid, until_first_stream=True)
        relay.cut_all()
        rest, idle = _collect(ws, mid)
        assert not idle  # the browser side was closed, not left hanging

        ws = websocket.create_connection(url, origin=ORIGIN, timeout=30)
        more, idle = _collect(ws, mid)
        assert idle
        msgs = first + rest + more
        assert _printed(msgs) == "".join(f"{i}\n" for i in range(8))
        assert any(m["msg_type"] == "execute_reply" and m["content"]["status"] == "ok" for m in msgs)
        ws.close()
    finally:
        requests.delete(f"{b.base}/api/kernels/{kid}", headers=_auth(), timeout=30)


def test_bridge_reconnects_upstream_and_keeps_browser_websocket(make_bridge, relay):
    """Experiment B: only the Colab side is reopened (same session_id); the browser keeps
    its WebSocket and still gets every output and the execute_reply."""
    b = make_bridge(_session_entry(relay.url, PROXY_TOKEN), upstream_reconnect=True)
    b.call(b.session.refresh())
    kid = _new_kernel(b.base)
    url = f"{b.ws_base}/api/kernels/{kid}/channels?session_id=client-2&token={LMS_TOKEN}"
    try:
        ws = websocket.create_connection(url, origin=ORIGIN, timeout=30)
        mid = _send_execute(ws, SLOW_CODE)
        first, _ = _collect(ws, mid, until_first_stream=True)
        relay.cut_all()
        rest, idle = _collect(ws, mid)
        assert idle and ws.connected
        msgs = first + rest
        assert _printed(msgs) == "".join(f"{i}\n" for i in range(8))
        assert any(m["msg_type"] == "execute_reply" and m["content"]["status"] == "ok" for m in msgs)

        # A request sent right after another drop is queued and delivered after reconnect.
        relay.cut_all()
        assert _printed(_execute(ws, "print('after')")) == "after\n"
        ws.close()
    finally:
        requests.delete(f"{b.base}/api/kernels/{kid}", headers=_auth(), timeout=30)


def test_failed_upstream_reconnect_closes_browser_websocket_with_1011(make_bridge, relay, monkeypatch):
    """Contract 3장: if the Colab side cannot be reopened, the browser WebSocket is closed
    with 1011 so the frontend reconnects or shows disconnected (never hangs)."""
    import lms_kernel.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "RECONNECT_DELAYS", (0.0, 0.2, 0.2))
    # Bound every attempt so a slow runner cannot stretch three attempts past the wait below.
    monkeypatch.setattr(bridge_mod, "CONNECT_TIMEOUT", 3.0)
    monkeypatch.setattr(bridge_mod, "HEARTBEAT_TIMEOUT", 3.0)
    b = make_bridge(_session_entry(relay.url, PROXY_TOKEN), upstream_reconnect=True,
                    min_refresh_interval=3600)
    b.call(b.session.refresh())
    kid = _new_kernel(b.base)
    ws = websocket.create_connection(
        f"{b.ws_base}/api/kernels/{kid}/channels?session_id=client-3&token={LMS_TOKEN}",
        origin=ORIGIN, timeout=60)
    try:
        # The browser handshake completes before the bridge's open() connects upstream.
        # Wait for a round trip so the upstream connection exists (and is in relay.conns);
        # otherwise it can slip past cut_all() and only the 60 s heartbeat closes the socket.
        req = _msg("kernel_info_request", {})
        ws.send(json.dumps(req))
        while json.loads(ws.recv()).get("parent_header", {}).get("msg_id") != req["header"]["msg_id"]:
            pass
        relay.refuse = True
        t0 = time.time()
        relay.cut_all()
        while True:
            opcode, data = ws.recv_data(control_frame=True)
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                break
        assert struct.unpack("!H", data[:2])[0] == 1011
        assert time.time() - t0 < 30
    finally:
        relay.refuse = False
        requests.delete(f"{b.base}/api/kernels/{kid}", headers=_auth(), timeout=30)


@pytest.mark.parametrize("reconnect", [False, True])
def test_debug_drop_upstream_after(make_bridge, upstream, reconnect):
    """--debug-drop-upstream-after closes the Colab side once; recovery is lossless either way."""
    b = make_bridge(_session_entry(upstream, PROXY_TOKEN), upstream_reconnect=reconnect,
                    debug_drop_upstream_after=1.0)
    b.call(b.session.refresh())
    kid = _new_kernel(b.base)
    url = f"{b.ws_base}/api/kernels/{kid}/channels?session_id=client-d&token={LMS_TOKEN}"
    try:
        ws = websocket.create_connection(url, origin=ORIGIN, timeout=30)
        mid = _send_execute(ws, SLOW_CODE)
        msgs, idle = _collect(ws, mid)
        if not reconnect:
            assert not idle  # browser side closed (1011); reconnect like the frontend does
            ws = websocket.create_connection(url, origin=ORIGIN, timeout=30)
            more, idle = _collect(ws, mid)
            msgs += more
        assert idle
        assert _printed(msgs) == "".join(f"{i}\n" for i in range(8))
        ws.close()
    finally:
        requests.delete(f"{b.base}/api/kernels/{kid}", headers=_auth(), timeout=30)


# --- helpers ---------------------------------------------------------------------------

def test_is_colab_only_message():
    colab = json.dumps(_msg("colab_request", {}) | {"msg_type": "colab_request"})
    normal = json.dumps(_msg("stream", {"text": "colab_request in output"}) | {"msg_type": "stream"})
    assert is_colab_only_message(colab)
    assert not is_colab_only_message(normal)
    assert not is_colab_only_message("not json colab_")

    body = colab.encode()
    # Legacy binary frame: nbufs=2, offsets [12 (msg start), end of msg], msg, one buffer.
    frame = struct.pack("!III", 2, 12, 12 + len(body)) + body + b"buf"
    assert is_colab_only_message(frame)


def test_cli_prints_registration_and_rejects_unknown_session(tmp_path, upstream):
    path = tmp_path / "sessions.json"
    _write(path, _session_entry(upstream, PROXY_TOKEN))
    exe = [sys.executable, "-m", "lms_kernel.launch", "--origin", ORIGIN,
           "--colab-config", str(path), "--colab-bin", FAKE_CLI]

    missing = subprocess.run(exe + ["--colab", "nope", "--port", str(_free_port())],
                             capture_output=True, text=True, timeout=60)
    assert missing.returncode == 1
    assert "nope" in missing.stderr

    port = _free_port()
    proc = subprocess.Popen(exe + ["--colab", SESSION, "--port", str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        lines = []
        while not any(l.startswith("  Token:") for l in lines):
            line = proc.stdout.readline()
            assert line, proc.stderr.read()
            lines.append(line)
        assert f"  URL  : http://127.0.0.1:{port}\n" in lines
        notice = "".join(lines)
        assert "예고 없이 종료될 수 있습니다" in notice
        assert f"colab stop -s {SESSION}" in notice
        token = [l for l in lines if l.startswith("  Token:")][0].split(":", 1)[1].strip()
        proc.stdout.readline()  # connect link heading (contract 2.4)
        link = proc.stdout.readline()
        assert link == (f"  {ORIGIN}/runtime/connect#url=http%3A%2F%2F127.0.0.1%3A{port}"
                        f"&token={token}\n")
        r = requests.get(f"http://127.0.0.1:{port}/api/kernels",
                         headers={"Authorization": f"token {token}"}, timeout=30)
        assert r.status_code == 200
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_cli_survives_one_ctrl_c_and_stops_on_two(tmp_path, upstream):
    from test_interrupt import ctrl_c, spawn_runtime, wait_ready

    path = tmp_path / "sessions.json"
    _write(path, _session_entry(upstream, PROXY_TOKEN))
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    err = tmp_path / "stderr.txt"
    proc, token = spawn_runtime(
        ["--origin", ORIGIN, "--colab-config", str(path), "--colab-bin", FAKE_CLI,
         "--colab", SESSION, "--port", str(port)], err)
    try:
        wait_ready(base, token)
        ctrl_c(proc)
        time.sleep(0.5)
        assert proc.poll() is None
        assert requests.get(f"{base}/api/kernels", headers={"Authorization": f"token {token}"},
                            timeout=30).status_code == 200
        ctrl_c(proc)
        assert proc.wait(timeout=30) == 0
        assert "한 번 더" in err.read_text() and "종료합니다" in err.read_text()
        with pytest.raises(requests.ConnectionError):
            requests.get(f"{base}/api/kernels", timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# --- kernel helpers on the Colab VM (contract 4) -------------------------------------------

def test_helpers_are_copied_and_importable_in_upstream_kernel(make_bridge, upstream, upstream_root):
    import shutil
    from lms_kernel import __version__
    from lms_kernel.bridge import HELPER_FILES, HelperInstaller

    for pkg in ("lms_kernel", "lms_viz", "env"):
        shutil.rmtree(upstream_root / pkg, ignore_errors=True)
    b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
    b.call(b.session.refresh())
    inst = HelperInstaller(b.session)
    assert b.call(inst.ensure()) is True and inst.target == "/"  # no env/python: root fallback
    src = Path(__import__("lms_kernel").__file__).resolve().parent.parent
    for pkg, name in HELPER_FILES:
        assert (upstream_root / pkg / name).read_text() == (src / pkg / name).read_text()
    assert not (upstream_root / "lms_kernel" / "bridge.py").exists()  # helpers only

    # The copied files alone are enough: no site-packages (-S), only the copy on sys.path.
    # (A kernel here would import this test environment's editable install instead.)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import lms_kernel.frontend as f, lms_viz; "
            "print(f.__file__); print(f.info().data)")
    out = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(upstream_root)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[0] == str(upstream_root / "lms_kernel" / "frontend.py")
    assert f"'lms_kernel': '{__version__}'" in out.stdout
    for pkg in ("lms_kernel", "lms_viz"):
        shutil.rmtree(upstream_root / pkg, ignore_errors=True)


def test_helpers_go_to_env_python_and_retry_on_kernel_create(make_bridge, upstream, upstream_root):
    import shutil
    from lms_kernel.bridge import HelperInstaller

    (upstream_root / "env" / "python").mkdir(parents=True, exist_ok=True)
    try:
        b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
        b.call(b.session.refresh())
        inst = HelperInstaller(b.session)
        b.server.stop()  # restart the bridge app with the installer wired in
        b.server = b.call(_start_with_installer(b, inst))
        assert not inst.done
        kid = _new_kernel(b.base)  # POST /api/kernels triggers the copy
        assert inst.done and inst.target == "/env/python"
        assert (upstream_root / "env" / "python" / "lms_viz" / "__init__.py").exists()
        assert not (upstream_root / "lms_viz").exists()
        requests.delete(f"{b.base}/api/kernels/{kid}", headers=_auth(), timeout=30)
    finally:
        shutil.rmtree(upstream_root / "env", ignore_errors=True)


async def _start_with_installer(b, inst):
    return start(ORIGIN, b.port, LMS_TOKEN, b.session, helper_installer=inst)


# --- Colab CLI version / session file format ---------------------------------------------

def _fake_cli_venv(tmp_path, version):
    """A venv-like layout whose bin/colab is the fake CLI and whose dist-info says `version`."""
    root = tmp_path / "colab-venv"
    (root / "bin").mkdir(parents=True)
    exe = root / "bin" / "colab"
    exe.write_text(Path(FAKE_CLI).read_text())
    exe.chmod(0o755)
    meta = root / "lib" / "python3.12" / "site-packages" / f"google_colab_cli-{version}.dist-info"
    meta.mkdir(parents=True)
    (meta / "METADATA").write_text(f"Metadata-Version: 2.1\nName: google-colab-cli\nVersion: {version}\n\nbody\n")
    return str(exe)


def test_cli_version_is_read_without_running_the_cli(tmp_path):
    from lms_kernel.colab_session import cli_version

    exe = _fake_cli_venv(tmp_path, "0.7.5")
    link = tmp_path / "colab-link"
    link.symlink_to(exe)  # like ~/.local/bin/colab -> ~/.colab-cli/bin/colab
    assert cli_version(exe) == "0.7.5"
    assert cli_version(str(link)) == "0.7.5"
    assert cli_version(FAKE_CLI) is None
    assert cli_version("no-such-colab-binary") is None


@pytest.mark.parametrize("version, warned", [("0.7.5", False), ("0.8.0", True)])
def test_warns_when_cli_version_differs(tmp_path, upstream, version, warned):
    path = tmp_path / "sessions.json"
    _write(path, _session_entry(upstream, PROXY_TOKEN))
    exe = [sys.executable, "-m", "lms_kernel.launch", "--origin", ORIGIN, "--colab-config", str(path),
           "--colab-bin", _fake_cli_venv(tmp_path, version), "--colab", "nope", "--port", str(_free_port())]
    out = subprocess.run(exe, capture_output=True, text=True, timeout=60)
    assert out.returncode == 1  # unknown session; the version check comes first
    assert ("Colab CLI 0.7.5에서 검증" in out.stderr) is warned
    if warned:
        assert "0.8.0" in out.stderr


def test_unknown_session_file_format_is_reported(tmp_path, upstream):
    from lms_kernel.colab_session import SessionFormatError, read_session

    path = tmp_path / "sessions.json"
    _write(path, {SESSION: {"name": SESSION, "endpoint": "x", "proxy": {"url": upstream}}})
    with pytest.raises(SessionFormatError):
        read_session(str(path), SESSION)
    _write(path, [1, 2])
    with pytest.raises(SessionFormatError):
        read_session(str(path), SESSION)
    _write(path, {})
    assert read_session(str(path), SESSION) is None  # absent is still "gone", not a format error

    _write(path, {SESSION: {"name": SESSION, "endpoint": "x", "proxy": {"url": upstream}}})
    exe = [sys.executable, "-m", "lms_kernel.launch", "--origin", ORIGIN, "--colab-config", str(path),
           "--colab-bin", FAKE_CLI, "--colab", SESSION, "--port", str(_free_port())]
    out = subprocess.run(exe, capture_output=True, text=True, timeout=60)
    assert out.returncode == 1
    assert "url·token이 없습니다" in out.stderr and "Traceback" not in out.stderr


def test_session_file_format_change_while_running_gives_503(make_bridge, upstream):
    b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
    b.call(b.session.refresh())
    _write(Path(str(b.sessions_path) + ".next"), {SESSION: {"name": SESSION, "proxy": {}}})
    b.session._info = None  # force a refresh on the next request
    r = requests.get(f"{b.base}/api/kernels", headers=_auth(Origin=ORIGIN), timeout=30)
    assert r.status_code == 503
    assert "0.7.5" in r.json()["message"]
    assert r.headers["Access-Control-Allow-Origin"] == ORIGIN


def test_warns_about_readable_colab_config_dir(tmp_path, upstream):
    from lms_kernel.colab_session import loose_config_dir

    config = tmp_path / "colab-cli"
    config.mkdir()
    path = config / "sessions.json"
    _write(path, _session_entry(upstream, PROXY_TOKEN))
    exe = [sys.executable, "-m", "lms_kernel.launch", "--origin", ORIGIN,
           "--colab-config", str(path), "--colab-bin", FAKE_CLI, "--colab", "nope",
           "--port", str(_free_port())]

    config.chmod(0o755)
    assert loose_config_dir(str(path)) == (str(config), 0o755)
    out = subprocess.run(exe, capture_output=True, text=True, timeout=60)
    assert f"chmod 700 {config}" in out.stderr
    assert config.stat().st_mode & 0o777 == 0o755  # only a warning, nothing is changed

    config.chmod(0o700)
    assert loose_config_dir(str(path)) is None
    out = subprocess.run(exe, capture_output=True, text=True, timeout=60)
    assert "chmod 700" not in out.stderr



def test_bridge_drops_first_reply_once(make_bridge, upstream):
    from test_debug_drop_reply import check_first_reply_dropped

    b = make_bridge(_session_entry(upstream, PROXY_TOKEN), debug_drop_reply_once=True)
    b.call(b.session.refresh())
    check_first_reply_dropped(b.base, b.ws_base, _auth(), {"origin": ORIGIN}, token=LMS_TOKEN)


# --- /lms/runtime, /lms/colab/usage (contract 2.2, 2.3) ----------------------------------

def _reads(b) -> list:
    p = Path(str(b.sessions_path) + ".reads")
    return p.read_text().split() if p.exists() else []


def test_runtime_info_answers_from_memory(bridge):
    calls = bridge.calls()
    r = requests.get(f"{bridge.base}/lms/runtime", headers=_auth(Origin=ORIGIN), timeout=5)
    assert r.status_code == 200
    assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert r.json() == {
        "version": 1, "kind": "colab", "lms_runtime": "0.6.0", "contract": "0.12",
        "colab": {"session": SESSION, "machine": "T4"},
    }
    assert r.elapsed.total_seconds() < 1
    assert bridge.calls() == calls and _reads(bridge) == []  # no CLI


def test_lms_paths_have_the_same_auth_origin_and_preflight(bridge):
    for path in ("/lms/runtime", "/lms/colab/usage"):
        assert requests.get(f"{bridge.base}{path}", timeout=5).status_code == 403
        r = requests.get(f"{bridge.base}{path}", headers=_auth(Origin="https://evil.test"), timeout=5)
        assert r.status_code == 404
        r = requests.options(f"{bridge.base}{path}", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization"}, timeout=5)
        assert r.status_code == 204 and r.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert requests.post(f"{bridge.base}{path}", headers=_auth(), timeout=5).status_code == 405
    assert _reads(bridge) == []


def test_colab_usage_runs_cli_and_caches(bridge):
    r = requests.get(f"{bridge.base}/lms/colab/usage", headers=_auth(Origin=ORIGIN), timeout=30)
    assert r.status_code == 200
    assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
    body = r.json()
    assert body.pop("checked_at").endswith("Z")
    assert body == {
        "version": 1, "balance": 576.13, "rate_per_hour": 1.15,
        "sessions": [{"name": SESSION, "machine": "T4", "current": True}], "raw": None,
    }
    assert _reads(bridge) == ["usage", "sessions"]
    again = requests.get(f"{bridge.base}/lms/colab/usage", headers=_auth(), timeout=30)
    assert again.json() == r.json()
    assert _reads(bridge) == ["usage", "sessions"]  # 30s cache


def test_colab_usage_unknown_output_gives_raw(make_bridge, upstream):
    b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
    Path(str(b.sessions_path) + ".usage").write_text("Saldo: 12 CU\n")
    r = requests.get(f"{b.base}/lms/colab/usage", headers=_auth(), timeout=30)
    assert r.status_code == 200
    body = r.json()
    assert body["balance"] is None and body["rate_per_hour"] is None
    assert body["sessions"] == [{"name": SESSION, "machine": "T4", "current": True}]
    assert body["raw"] == {"usage": "Saldo: 12 CU", "sessions": None}


def test_colab_usage_cli_failure_gives_502_and_is_not_cached(make_bridge, upstream):
    b = make_bridge(_session_entry(upstream, PROXY_TOKEN))
    fail = Path(str(b.sessions_path) + ".fail")
    fail.write_text("")
    r = requests.get(f"{b.base}/lms/colab/usage", headers=_auth(Origin=ORIGIN), timeout=30)
    assert r.status_code == 502
    assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "colab usage" in r.json()["message"]
    fail.unlink()
    assert requests.get(f"{b.base}/lms/colab/usage", headers=_auth(), timeout=30).status_code == 200
