"""Local bridge that exposes a Colab runtime with the same contract as a local runtime.

The browser sees ``http://127.0.0.1:<port>`` + LMS token, exactly like ``lms-runtime``
without ``--colab``. The bridge:

- binds to 127.0.0.1 only,
- accepts only the contract paths (docs/runtime-contract.md 2장),
- authenticates with ``Authorization: token`` (REST) or ``?token=`` (WebSocket),
- checks Origin even for token-authenticated requests (REST 404, WebSocket 403),
- pins ``Access-Control-Allow-Origin`` to the LMS origin and answers preflights,
- forwards to the Colab runtime proxy with the proxy token attached,
- drops Colab-only kernel messages (``colab_request`` ...),
- turns an unreachable or terminated Colab runtime into REST errors / WebSocket close,
- returns the Colab VM with `colab stop` once every kernel has been idle long enough
  (contract 2.1; Colab bills the VM assignment, not kernels).

Neither the LMS token nor the Colab proxy token is ever logged.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import struct
import time
from typing import Optional
from urllib.parse import urlencode, urlsplit

from tornado import httpclient, web, websocket

from .colab_session import ColabSession, SessionFormatError, SessionGone
from .debug import ReplyDropper

log = logging.getLogger("lms_kernel.bridge")

PROXY_TOKEN_HEADER = "X-Colab-Runtime-Proxy-Token"
PROXY_TOKEN_QUERY = "colab-runtime-proxy-token"

ALLOW_HEADERS = "accept, content-type, authorization, x-xsrftoken"
ALLOW_METHODS = "GET, PUT, POST, PATCH, DELETE, OPTIONS"
# Response headers copied from the upstream; everything else (cookies, CORS, ...) is dropped.
PASS_RESPONSE_HEADERS = ("Content-Type", "Location", "Cache-Control")

CONNECT_TIMEOUT = 15.0
REQUEST_TIMEOUT = 120.0  # kernel start/restart on Colab can be slow
# The Colab proxy does not answer WebSocket pings (measured 2026-11-03: a pinged
# connection drops after ~25s, an unpinged one stays open). Liveness of an open
# WebSocket is therefore checked over REST instead.
HEARTBEAT_INTERVAL = 30.0
HEARTBEAT_TIMEOUT = 10.0
HEARTBEAT_FAILURES = 2
MAX_MESSAGE_SIZE = 256 * 1024 * 1024
# Upstream reconnect (keep the browser WebSocket, reopen only the Colab side with the
# same session_id so the Colab Jupyter Server replays what it buffered meanwhile).
RECONNECT_DELAYS = (0.0, 1.0, 2.0, 4.0)
# Idle stop (contract 2.1). A failed `colab stop` is retried after STOP_RETRY seconds.
IDLE_POLL_INTERVAL = 60.0
STOP_RETRY = 300.0

MSG_GONE = "Colab 런타임이 종료되었습니다. `colab new`로 세션을 새로 만들고 lms-runtime을 다시 실행하세요."
MSG_UNREACHABLE = "Colab 런타임에 연결할 수 없습니다. 런타임이 종료되었거나 네트워크에 문제가 있습니다."

ID = r"[A-Za-z0-9_-]+"


class BridgeError(web.HTTPError):
    """HTTP error whose student-facing (Korean) message goes in the JSON body.

    The reason phrase stays ASCII because it is written into the status line.
    """

    def __init__(self, status_code: int, message: str):
        super().__init__(status_code)
        self.message = message


def _strip_token(query_arguments: dict) -> list[tuple[str, str]]:
    return [
        (k, v.decode("utf-8", "replace"))
        for k, values in query_arguments.items()
        if k != "token"
        for v in values
    ]


def parse_frame(msg) -> Optional[dict]:
    """The Jupyter message in a legacy-protocol frame (JSON text or binary), or None."""
    try:
        if isinstance(msg, bytes):
            # Legacy Jupyter binary frame: uint32 nbufs, nbufs uint32 offsets, JSON msg, buffers.
            (nbufs,) = struct.unpack_from("!I", msg, 0)
            offsets = struct.unpack_from(f"!{nbufs}I", msg, 4)
            end = offsets[1] if nbufs > 1 else len(msg)
            return json.loads(msg[offsets[0]:end])
        return json.loads(msg)
    except Exception:
        return None


def is_colab_only_message(msg) -> bool:
    """True for Colab-specific kernel messages (msg_type ``colab_*``) the LMS does not handle."""
    if (b"colab_" if isinstance(msg, bytes) else "colab_") not in msg:
        return False
    data = parse_frame(msg)
    if not isinstance(data, dict):
        return False
    msg_type = data.get("msg_type") or data.get("header", {}).get("msg_type") or ""
    return msg_type.startswith("colab_")


class _Auth:
    """Shared token/origin checks for REST and WebSocket handlers."""

    def _settings(self):
        return self.application.settings

    def _token_ok(self) -> bool:
        expected = self._settings()["lms_token"]
        auth = self.request.headers.get("Authorization", "")
        scheme, _, value = auth.partition(" ")
        if scheme.lower() in ("token", "bearer") and value.strip():
            supplied = value.strip()
        else:
            supplied = self.get_query_argument("token", "")
        return bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())

    def _origin_ok(self) -> bool:
        origin = self.request.headers.get("Origin")
        # No Origin header: scripts and CLI tools (same rule as the local runtime).
        return origin is None or origin == self._settings()["lms_origin"]

    @property
    def colab(self) -> ColabSession:
        return self._settings()["colab_session"]

    def _schedule_liveness_check(self):
        """Let the CLI tell us whether the session still exists (throttled)."""
        colab = self.colab

        async def check():
            try:
                await colab.refresh()
            except SessionGone:
                pass
            except Exception:
                log.exception("liveness check failed")

        asyncio.ensure_future(check())


class RestHandler(_Auth, web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", self.application.settings["lms_origin"])
        self.set_header("Vary", "Origin")

    def check_xsrf_cookie(self):
        # Token in the Authorization header; no cookies are ever issued.
        return

    def write_error(self, status_code, **kwargs):
        self.set_header("Content-Type", "application/json")
        exc = kwargs.get("exc_info", (None, None, None))[1]
        message = getattr(exc, "message", None) or self._reason
        self.finish(json.dumps({"message": message, "reason": None}, ensure_ascii=False))

    def prepare(self):
        if self.request.method == "OPTIONS":
            return
        if not self._origin_ok():
            raise web.HTTPError(404)
        if not self._token_ok():
            raise web.HTTPError(403)

    def options(self, *args):
        self.set_header("Access-Control-Allow-Headers", ALLOW_HEADERS)
        self.set_header("Access-Control-Allow-Methods", ALLOW_METHODS)
        if self.request.headers.get("Access-Control-Request-Private-Network") == "true":
            self.set_header("Access-Control-Allow-Private-Network", "true")
        self.set_status(204)
        self.finish()

    async def _forward(self, *args):
        try:
            info = await self.colab.current()
        except SessionGone:
            raise BridgeError(503, MSG_GONE)
        except SessionFormatError as e:
            raise BridgeError(503, str(e))

        query = _strip_token(self.request.query_arguments)
        method = self.request.method
        body = self.request.body if method in ("POST", "PUT", "PATCH") else None
        for attempt in range(2):
            url = info.url + self.request.path + (("?" + urlencode(query)) if query else "")
            headers = {PROXY_TOKEN_HEADER: info.token, "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = self.request.headers.get("Content-Type", "application/json")
            req = httpclient.HTTPRequest(
                url,
                method=method,
                headers=headers,
                body=body if body is not None else None,
                allow_nonstandard_methods=True,
                follow_redirects=False,
                connect_timeout=CONNECT_TIMEOUT,
                request_timeout=REQUEST_TIMEOUT,
            )
            try:
                resp = await httpclient.AsyncHTTPClient().fetch(req, raise_error=False)
            except Exception as e:  # DNS errors etc.; log the type only
                log.warning("upstream request failed: %s", type(e).__name__)
                self._schedule_liveness_check()
                raise BridgeError(502, MSG_UNREACHABLE)
            if resp.code == 599:
                log.warning("upstream unreachable: %s", type(resp.error).__name__)
                self._schedule_liveness_check()
                raise BridgeError(502, MSG_UNREACHABLE)
            if resp.code in (401, 403) and attempt == 0:
                # Proxy token may have been rotated or expired early; refresh once and retry.
                try:
                    info = await self.colab.refresh()
                except SessionGone:
                    raise BridgeError(503, MSG_GONE)
                except SessionFormatError as e:
                    raise BridgeError(503, str(e))
                continue
            break

        if 300 <= resp.code < 400 or resp.code in (401, 403):
            # Redirects (e.g. to a Google login page) or auth failures mean the
            # runtime is no longer usable through this session.
            self._schedule_liveness_check()
            raise BridgeError(502, MSG_UNREACHABLE)
        if resp.code >= 500:
            self._schedule_liveness_check()

        self.set_status(resp.code, reason=resp.reason or "Upstream")
        for name in PASS_RESPONSE_HEADERS:
            value = resp.headers.get(name)
            if value is None:
                continue
            if name == "Location":
                # Keep redirects on the bridge: drop scheme/host of the upstream.
                parts = urlsplit(value)
                value = parts.path + (("?" + parts.query) if parts.query else "")
            self.set_header(name, value)
        if resp.body and resp.code != 204:
            self.write(resp.body)
        self.finish()

    get = post = delete = _forward


class ChannelsHandler(_Auth, websocket.WebSocketHandler):
    upstream: Optional[websocket.WebSocketClientConnection] = None

    async def get(self, kernel_id):
        if not self._origin_ok():
            raise web.HTTPError(403)
        if not self._token_ok():
            raise web.HTTPError(403)
        await super().get(kernel_id)

    def check_origin(self, origin):
        return origin == self.application.settings["lms_origin"]

    def select_subprotocol(self, subprotocols):
        # Use the legacy JSON protocol so Colab-only messages can be inspected and
        # dropped. @jupyterlab/services falls back to it when no subprotocol is chosen.
        return None

    async def open(self, kernel_id):
        self._client_closed = False
        self._kernel_id = kernel_id
        self._pending: list = []  # browser messages received while reconnecting upstream
        self.reconnects = 0
        try:
            await self.colab.current()
        except SessionGone:
            self.close(1011, "colab runtime terminated")
            return
        self.upstream = await self._connect_upstream()
        if self.upstream is None:
            self.close(1011, "colab runtime unreachable")
            return
        self._pump_task = asyncio.ensure_future(self._pump())
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat(kernel_id))
        self._schedule_debug_drop()

    def _schedule_debug_drop(self):
        """Diagnostics only (--debug-drop-upstream-after): close the Colab-side WebSocket
        once per (kernel, session_id) after N seconds, with a normal close frame, to see
        how a dropped connection is recovered. Nothing else about the requests changes."""
        after = self._settings().get("debug_drop_upstream_after")
        if not after:
            return
        key = (self._kernel_id, self.get_query_argument("session_id", ""))
        dropped = self._settings().setdefault("_debug_dropped", set())
        if key in dropped:
            return
        dropped.add(key)

        async def drop():
            await asyncio.sleep(after)
            if not self._client_closed and self.upstream is not None:
                log.warning("debug: closing upstream websocket after %.0fs", after)
                self.upstream.close()  # treated like an unexpected drop by _pump

        asyncio.ensure_future(drop())

    async def _connect_upstream(self) -> Optional[websocket.WebSocketClientConnection]:
        try:
            info = await self.colab.current()
        except SessionGone:
            return None
        # Everything but the LMS token is passed through, in particular `session_id`:
        # the Jupyter Server keys its reconnect buffer on (kernel_id, session_id).
        params = _strip_token(self.request.query_arguments) + [(PROXY_TOKEN_QUERY, info.token)]
        base = info.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        req = httpclient.HTTPRequest(
            f"{base}/api/kernels/{self._kernel_id}/channels?{urlencode(params)}",
            headers={PROXY_TOKEN_HEADER: info.token},
            connect_timeout=CONNECT_TIMEOUT,
            request_timeout=CONNECT_TIMEOUT,
        )
        try:
            return await websocket.websocket_connect(
                req,
                ping_interval=None,
                max_message_size=MAX_MESSAGE_SIZE,
            )
        except Exception as e:
            # Exceptions may carry the upstream URL (with the proxy token); log the type only.
            log.warning("upstream websocket connect failed: %s", type(e).__name__)
            self._schedule_liveness_check()
            return None

    async def _reconnect_upstream(self) -> bool:
        """Reopen the Colab side after an unexpected close; False if it cannot be restored."""
        for delay in RECONNECT_DELAYS:
            await asyncio.sleep(delay)
            if self._client_closed or self.colab.gone:
                return False
            try:
                info = await self.colab.current()
            except SessionGone:
                return False
            if await upstream_status(info, f"/api/kernels/{self._kernel_id}", HEARTBEAT_TIMEOUT) == 404:
                return False  # kernel is gone; reconnecting would only create confusion
            upstream = await self._connect_upstream()
            if upstream is None:
                continue
            if self._client_closed:
                upstream.close()
                return False
            self.upstream = upstream
            self.reconnects += 1
            pending, self._pending = self._pending, []
            for message in pending:
                await upstream.write_message(message, binary=isinstance(message, bytes))
            return True
        return False

    async def _heartbeat(self, kernel_id):
        """Close the relay when the Colab runtime stops answering, so the browser never hangs."""
        failures = 0
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if self._client_closed:
                return
            if self.upstream is None:
                continue  # reconnecting
            try:
                info = await self.colab.current()
                code = await upstream_status(info, f"/api/kernels/{kernel_id}", HEARTBEAT_TIMEOUT)
            except SessionGone:
                code, failures = None, HEARTBEAT_FAILURES
            if code == 200:
                failures = 0
                continue
            failures += 1
            if code == 404 or failures >= HEARTBEAT_FAILURES:
                log.warning("colab runtime not responding (status=%s); closing websocket", code)
                self._schedule_liveness_check()
                if self.upstream is not None:
                    self.upstream._lms_closed_by_bridge = True
                    self.upstream.close()  # _pump then closes the browser side with 1011
                return

    async def _pump(self):
        while True:
            upstream = self.upstream
            try:
                while True:
                    msg = await upstream.read_message()
                    if msg is None:
                        break
                    if is_colab_only_message(msg):
                        continue
                    dropper = self._settings().get("reply_dropper")
                    if dropper is not None:
                        data = parse_frame(msg)
                        if isinstance(data, dict) and dropper.should_drop(self._kernel_id, data):
                            continue
                    try:
                        await self.write_message(msg, binary=isinstance(msg, bytes))
                    except websocket.WebSocketClosedError:
                        break
            except Exception:
                log.exception("websocket relay failed")
            if self._client_closed:
                return
            log.warning("upstream websocket closed (code=%s)", upstream.close_code)
            if (
                self._settings().get("upstream_reconnect")
                and not getattr(upstream, "_lms_closed_by_bridge", False)
            ):
                self.upstream = None  # on_message queues into _pending meanwhile
                started = time.monotonic()
                if await self._reconnect_upstream():
                    log.warning("upstream websocket reconnected in %.1fs", time.monotonic() - started)
                    continue
            break
        self._schedule_liveness_check()
        if not self._client_closed:
            self.close(1011, "colab runtime disconnected")

    async def on_message(self, message):
        if self.upstream is None:
            if not self._client_closed and hasattr(self, "_pending"):
                self._pending.append(message)
            return
        try:
            await self.upstream.write_message(message, binary=isinstance(message, bytes))
        except websocket.WebSocketClosedError:
            # _pump notices the close and reconnects (or closes the browser side);
            # keep the message for the new upstream connection.
            self._pending.append(message)

    def on_close(self):
        self._client_closed = True
        task = getattr(self, "_heartbeat_task", None)
        if task is not None:
            task.cancel()
        if self.upstream is not None:
            self.upstream.close()


def _log_request(handler: web.RequestHandler):
    # Path only: the query string of WebSocket requests carries the LMS token.
    req = handler.request
    status = handler.get_status()
    level = logging.INFO if status < 400 else logging.WARNING
    log.log(level, "%s %s %d %.0fms", req.method, req.path, status, 1000.0 * req.request_time())


def make_app(
    origin: str,
    token: str,
    colab_session: ColabSession,
    upstream_reconnect: bool = True,
    debug_drop_upstream_after: Optional[float] = None,
    debug_drop_reply_once: bool = False,
) -> web.Application:
    kernel = rf"/api/kernels/(?:{ID})"
    return web.Application(
        [
            (r"/api/kernels", RestHandler),
            (kernel, RestHandler),
            (rf"{kernel}/(?:interrupt|restart)", RestHandler),
            (r"/api/kernelspecs", RestHandler),
            (rf"/api/kernels/({ID})/channels", ChannelsHandler),
        ],
        lms_origin=origin,
        lms_token=token,
        colab_session=colab_session,
        upstream_reconnect=upstream_reconnect,
        debug_drop_upstream_after=debug_drop_upstream_after,
        reply_dropper=ReplyDropper() if debug_drop_reply_once else None,
        log_function=_log_request,
        websocket_max_message_size=MAX_MESSAGE_SIZE,
        websocket_ping_interval=None,
    )


def start(
    origin: str,
    port: int,
    token: str,
    colab_session: ColabSession,
    upstream_reconnect: bool = True,
    debug_drop_upstream_after: Optional[float] = None,
    debug_drop_reply_once: bool = False,
):
    """Bind 127.0.0.1:<port> (raises OSError if taken). Call inside a running event loop."""
    app = make_app(
        origin, token, colab_session, upstream_reconnect, debug_drop_upstream_after,
        debug_drop_reply_once,
    )
    return app.listen(port, address="127.0.0.1")


async def keep_fresh(colab_session: ColabSession, interval: float = 15.0) -> None:
    """Notice `colab stop` and refresh the proxy token before it expires, even while idle.

    Reading sessions.json is cheap and does not call the CLI; the CLI is only run when
    the token is close to expiry.
    """
    while not colab_session.gone:
        await asyncio.sleep(interval)
        try:
            if not colab_session.check_local():
                break
            await colab_session.current()
        except SessionGone:
            break
        except Exception:
            log.exception("token refresh failed")


async def stop_when_idle(
    colab_session: ColabSession,
    idle_timeout: float,
    poll_interval: float = IDLE_POLL_INTERVAL,
    stop_retry: float = STOP_RETRY,
) -> bool:
    """Contract 2.1 (colab): `colab stop` once every kernel has been idle for idle_timeout s.

    Activity is read from the upstream ``GET /api/kernels``: a busy kernel, a change in
    the set of kernels, or a change in any kernel's ``last_activity`` restarts the timer.
    Upstream timestamps are only compared with each other, never with the local clock,
    so clock skew between this PC (WSL) and the Colab VM does not matter. Open
    WebSockets do not count. A poll that fails neither restarts the timer nor stops
    the VM. Returns True if the VM was returned, False if the session went away first.
    """
    last_active = time.monotonic()
    previous = None
    retry_at = 0.0
    while not colab_session.gone:
        await asyncio.sleep(poll_interval)
        try:
            info = await colab_session.current()
        except SessionGone:
            return False
        kernels = await upstream_json(info, "/api/kernels", HEARTBEAT_TIMEOUT)
        if not isinstance(kernels, list):
            continue
        now = time.monotonic()
        snapshot = frozenset((k.get("id"), k.get("last_activity")) for k in kernels if isinstance(k, dict))
        busy = any(isinstance(k, dict) and k.get("execution_state") == "busy" for k in kernels)
        if busy or snapshot != previous:
            last_active = now
        previous = snapshot
        if now - last_active < idle_timeout or now < retry_at:
            continue
        if await colab_session.stop():
            return True
        retry_at = now + stop_retry
    return False


async def upstream_json(info, path: str, timeout: float):
    """Parsed JSON body of GET <path> on the Colab runtime, or None on any failure."""
    req = httpclient.HTTPRequest(
        info.url + path,
        headers={PROXY_TOKEN_HEADER: info.token, "Accept": "application/json"},
        follow_redirects=False,
        connect_timeout=timeout,
        request_timeout=timeout,
    )
    try:
        resp = await httpclient.AsyncHTTPClient().fetch(req, raise_error=False)
        return json.loads(resp.body) if resp.code == 200 else None
    except Exception:
        return None


async def upstream_status(info, path: str, timeout: float) -> Optional[int]:
    """HTTP status of GET <path> on the Colab runtime, or None if unreachable."""
    req = httpclient.HTTPRequest(
        info.url + path,
        headers={PROXY_TOKEN_HEADER: info.token, "Accept": "application/json"},
        follow_redirects=False,
        connect_timeout=timeout,
        request_timeout=timeout,
    )
    try:
        resp = await httpclient.AsyncHTTPClient().fetch(req, raise_error=False)
    except Exception:
        return None
    return None if resp.code == 599 else resp.code


async def probe(info) -> bool:
    """True if the Colab runtime answers the Jupyter API with this proxy token."""
    return await upstream_status(info, "/api/kernelspecs", 30.0) == 200
