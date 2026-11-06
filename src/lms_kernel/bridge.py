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
  (contract 2.1; Colab bills the VM assignment, not kernels),
- copies the kernel helpers (``lms_kernel.frontend``/``viz``, ``lms_viz``, contract 4)
  onto the Colab VM through the standard Contents API, so they match this bridge's version.

Neither the LMS token nor the Colab proxy token is ever logged.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import struct
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlsplit

from tornado import httpclient, web, websocket

from .colab_diag import snapshot
from .colab_session import CLIError, ColabSession, SessionFormatError, SessionGone
from .colab_usage import UsageCache, scrub
from .runtime_info import runtime_info
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
# Upstream connect/reconnect: exponential backoff, at most len(RECONNECT_DELAYS) attempts.
# Reconnects keep the browser WebSocket and reopen only the Colab side with the same
# session_id so the Colab Jupyter Server replays what it buffered meanwhile. Attempts stop
# as soon as the Colab Jupyter Server says the kernel does not exist.
RECONNECT_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0)
# A failure that may mean the Colab session is gone runs `colab status` at most this often
# (each CLI run writes credentials to colab.log; measured 2026-11-07: a burst of failures
# ran it every 30 s for 7 minutes).
LIVENESS_INTERVAL = 120.0
# Diagnostics read from the Colab VM when a kernel disappears (colab_diag), at most this often.
DIAG_INTERVAL = 30.0
BODY_HEAD = 200
# Idle stop (contract 2.1). A failed `colab stop` is retried after STOP_RETRY seconds.
IDLE_POLL_INTERVAL = 60.0
STOP_RETRY = 300.0

MSG_GONE = "Colab 런타임이 종료되었습니다. `colab new`로 세션을 새로 만들고 lms-runtime을 다시 실행하세요."
MSG_UNREACHABLE = "Colab 런타임에 연결할 수 없습니다. 런타임이 종료되었거나 네트워크에 문제가 있습니다."

ID = r"[A-Za-z0-9_-]+"

# Contract 4 helpers copied to the Colab VM. Only these files: they need nothing but the
# standard library, packaging and IPython (all on Colab); the rest of lms_kernel (server,
# bridge) never runs in the kernel. /env/python is on the Colab kernel's sys.path ahead of
# site-packages regardless of the working directory; "" (the Contents API root) is the
# fallback for images without it.
HELPER_FILES = (
    ("lms_kernel", "__init__.py"),
    ("lms_kernel", "frontend.py"),
    ("lms_kernel", "viz.py"),
    ("lms_viz", "__init__.py"),
)
HELPER_TARGETS = ("env/python", "")


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


def jupyter_error(body) -> Optional[str]:
    """The ``message`` of a Jupyter Server JSON error body, or None.

    The Colab proxy answers an unknown/invalid proxy token (and requests with an Origin
    header) with 404 and an empty body (measured 2026-11-07), while the Colab Jupyter
    Server answers an unknown kernel with 404 and ``{"message": "Kernel does not exist: ..."}``.
    """
    try:
        data = json.loads(body)
    except Exception:
        return None
    message = data.get("message") if isinstance(data, dict) else None
    return message if isinstance(message, str) else None


def from_jupyter(body) -> bool:
    """True if an error response came from the Colab Jupyter Server rather than the proxy:
    a JSON error (REST) or Jupyter's HTML error page (WebSocket handshake). The proxy's own
    rejections have an empty body."""
    return jupyter_error(body) is not None or b"Jupyter Server" in (body or b"")


def body_head(body, info=None) -> str:
    """First BODY_HEAD characters of an upstream response body, safe to log."""
    text = (body or b"").decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    if info is not None and info.token:
        text = text.replace(info.token, "***")
    if text.lstrip().startswith("<"):  # HTML error page: keep the visible text
        text = re.sub(r"(?is)<(head|script|style)\b.*?</\1>|<[^>]+>", " ", text)
    text = " ".join(scrub(text).split())
    return repr(text[:BODY_HEAD] + ("…" if len(text) > BODY_HEAD else ""))


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

    @property
    def watch(self) -> "RuntimeWatch":
        return self._settings()["runtime_watch"]

    def _schedule_liveness_check(self):
        """Let the CLI tell us whether the session still exists (at most every LIVENESS_INTERVAL)."""
        colab = self.colab
        settings = self._settings()
        now = time.monotonic()
        last = settings.get("_last_liveness_check")
        if last is not None and now - last < settings.get("liveness_interval", LIVENESS_INTERVAL):
            return
        settings["_last_liveness_check"] = now

        async def check():
            try:
                await colab.refresh()
            except SessionGone:
                pass
            except Exception:
                log.exception("liveness check failed")

        asyncio.ensure_future(check())


    def _schedule_token_refresh(self):
        """Get the current proxy URL/token from the CLI (throttled by ColabSession)."""
        colab = self.colab

        async def refresh():
            try:
                await colab.refresh()
            except (SessionGone, SessionFormatError):
                pass
            except Exception:
                log.exception("token refresh failed")

        asyncio.ensure_future(refresh())


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
        if self.request.method == "POST" and self.request.path == "/api/kernels":
            installer = self._settings().get("helper_installer")
            if installer is not None:
                await installer.ensure()  # retry if it failed at startup; no-op once done
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
                log.warning("upstream request failed: %s %s: %s", method, self.request.path, type(e).__name__)
                self._schedule_liveness_check()
                raise BridgeError(502, MSG_UNREACHABLE)
            if resp.code == 599:
                log.warning("upstream unreachable: %s %s: %s", method, self.request.path, type(resp.error).__name__)
                self._schedule_liveness_check()
                raise BridgeError(502, MSG_UNREACHABLE)
            proxy_404 = resp.code == 404 and not from_jupyter(resp.body)
            if (resp.code in (401, 403) or proxy_404) and attempt == 0:
                # The proxy rejected the token (it answers 404 with an empty body, not 401):
                # get the current proxy URL/token from the CLI and retry once.
                log.warning(
                    "upstream %s %s: proxy answered %d body=%s; refreshing the proxy token and retrying",
                    method, self.request.path, resp.code, body_head(resp.body, info),
                )
                try:
                    info = await self.colab.refresh()
                except SessionGone:
                    raise BridgeError(503, MSG_GONE)
                except SessionFormatError as e:
                    raise BridgeError(503, str(e))
                continue
            break

        answered_by_jupyter = from_jupyter(resp.body)
        if resp.code >= 400:
            log.warning("upstream %s %s -> %d body=%s", method, self.request.path, resp.code, body_head(resp.body, info))
        if 300 <= resp.code < 400 or resp.code in (401, 403) or (resp.code == 404 and not answered_by_jupyter):
            # Redirects (e.g. to a Google login page) or auth failures mean the
            # runtime is no longer usable through this session.
            self._schedule_liveness_check()
            raise BridgeError(502, MSG_UNREACHABLE)
        if resp.code >= 500 and not answered_by_jupyter:
            self._schedule_liveness_check()  # the proxy, not the Jupyter Server, failed
        kernel_id = self.request.path.split("/")[3] if self.request.path.startswith("/api/kernels/") else None
        if resp.code == 404 and kernel_id:
            self.watch.kernel_lost(kernel_id, f"{method} {resp.code}")
        elif method == "POST" and self.request.path == "/api/kernels" and resp.code == 201:
            try:
                new_id = json.loads(resp.body).get("id")
            except Exception:
                new_id = None
            if isinstance(new_id, str):
                self.watch.known.add(new_id)

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


MACHINE_LABELS = {"NONE": "CPU"}


class RuntimeInfoHandler(RestHandler):
    """GET /lms/runtime (contract 2.2): answered from memory, no CLI or upstream call."""

    def get(self):
        colab = self.colab
        info = colab.info
        accelerator = info.accelerator if info is not None else None
        machine = MACHINE_LABELS.get(accelerator, accelerator) if accelerator else None
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(runtime_info("colab", colab.name, machine)))

    SUPPORTED_METHODS = ("GET", "OPTIONS")


class ColabUsageHandler(RestHandler):
    """GET /lms/colab/usage (contract 2.3): `colab usage` + `colab sessions`, cached 30s."""

    async def get(self):
        cache = self._settings()["usage_cache"]
        try:
            body = await cache.get()
        except CLIError as e:
            raise BridgeError(502, str(e))
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(body, ensure_ascii=False))

    SUPPORTED_METHODS = ("GET", "OPTIONS")


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
        if kernel_id in self.watch.gone:
            # Already known to be gone (Colab said so): do not hammer the proxy again.
            self.close(1011, "kernel not found")
            return
        try:
            await self.colab.current()
        except SessionGone:
            self.close(1011, "colab runtime terminated")
            return
        # tornado calls on_message only after open() returns, so nothing is lost while retrying.
        self.upstream = await self._connect_with_retry()
        if self.upstream is None:
            if not self._client_closed:
                self.close(1011, "kernel not found" if kernel_id in self.watch.gone else "colab runtime unreachable")
            return
        self.watch.known.add(kernel_id)
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

    async def _connect_once(self, info):
        """(connection, status, kernel_gone). status is the HTTP status of a refused
        handshake, None for timeouts and network errors."""
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
            conn = await websocket.websocket_connect(
                req,
                ping_interval=None,
                max_message_size=MAX_MESSAGE_SIZE,
            )
            return conn, None, False
        except httpclient.HTTPClientError as e:
            # Never log str(e.response) or its effective_url: the query carries the proxy token.
            body = e.response.body if e.response is not None else b""
            gone = e.code == 404 and from_jupyter(body)
            log.warning("upstream websocket connect failed: HTTP %d body=%s", e.code, body_head(body, info))
            return None, e.code, gone
        except Exception as e:
            # Other exceptions may carry the upstream URL (with the proxy token); log the type only.
            log.warning("upstream websocket connect failed: %s", type(e).__name__)
            return None, None, False

    async def _connect_with_retry(self) -> Optional[websocket.WebSocketClientConnection]:
        """Open the Colab side with backoff (RECONNECT_DELAYS); None if it cannot be opened.

        Each attempt first asks ``GET /api/kernels/<id>`` and opens the WebSocket only if the
        kernel exists. Measured 2026-11-07 (T4, 3 of 3): a WebSocket to a kernel that does not
        exist passes the Colab proxy handshake (101), and some 20-80 s later the whole Colab
        runtime container restarts, taking every kernel and file with it. A WebSocket to an
        existing kernel does not. Attempts stop once the Colab Jupyter Server says the kernel
        does not exist. A request the proxy itself rejected (empty-body 404, 401/403) gets
        the current proxy URL/token from the CLI before the next attempt.
        """
        attempts = len(RECONNECT_DELAYS)
        for n, delay in enumerate(RECONNECT_DELAYS, 1):
            await asyncio.sleep(delay)
            if self._client_closed or self.colab.gone:
                return None
            try:
                info = await self.colab.current()
            except (SessionGone, SessionFormatError):
                return None
            code, body = await upstream_get(info, f"/api/kernels/{self._kernel_id}", HEARTBEAT_TIMEOUT)
            if code == 404 and from_jupyter(body):
                log.warning("kernel %s does not exist on Colab; not opening a websocket", self._kernel_id[:8])
                self.watch.kernel_lost(self._kernel_id, "kernel 404 on connect")
                return None
            if code == 200:
                upstream, status, gone = await self._connect_once(info)
                if upstream is not None:
                    if self._client_closed:
                        upstream.close()
                        return None
                    return upstream
                if gone:
                    self.watch.kernel_lost(self._kernel_id, "websocket 404")
                    return None
            else:
                status = code
                log.warning("upstream GET kernel before connecting -> %s body=%s", code, body_head(body, info))
            log.warning("upstream websocket attempt %d/%d failed", n, attempts)
            if status in (401, 403, 404):
                try:
                    await self.colab.refresh()
                except (SessionGone, SessionFormatError):
                    return None
            else:
                self._schedule_liveness_check()
        return None

    async def _reconnect_upstream(self) -> bool:
        """Reopen the Colab side after an unexpected close; False if it cannot be restored."""
        upstream = await self._connect_with_retry()
        if upstream is None:
            return False
        self.upstream = upstream
        self.reconnects += 1
        pending, self._pending = self._pending, []
        for message in pending:
            await upstream.write_message(message, binary=isinstance(message, bytes))
        return True

    async def _heartbeat(self, kernel_id):
        """Close the relay when the Colab runtime stops answering, so the browser never hangs."""
        failures = 0
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if self._client_closed:
                return
            if self.upstream is None:
                continue  # reconnecting
            body = b""
            try:
                info = await self.colab.current()
                code, body = await upstream_get(info, f"/api/kernels/{kernel_id}", HEARTBEAT_TIMEOUT)
            except (SessionGone, SessionFormatError):
                info, code, failures = None, None, HEARTBEAT_FAILURES
            if code == 200:
                failures = 0
                continue
            gone = code == 404 and from_jupyter(body)
            failures += 1
            if code is not None:
                log.warning("heartbeat: upstream GET kernel -> %d body=%s", code, body_head(body, info))
            if code in (401, 403, 404) and not gone:
                self._schedule_token_refresh()  # the proxy rejected the token (①)
            if gone:
                self.watch.kernel_lost(kernel_id, "heartbeat 404")
            if gone or failures >= HEARTBEAT_FAILURES:
                if gone:
                    log.warning("kernel %s is gone on Colab; closing websocket", kernel_id[:8])
                else:
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
        if self._kernel_id not in self.watch.gone:  # a gone kernel says nothing about the session
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


class HelperInstaller:
    """Copies the contract 4 helpers to the Colab VM once (standard ``PUT /api/contents``).

    Not exposed to the browser: the bridge relays only the contract paths.
    """

    def __init__(self, colab_session: ColabSession):
        self.colab = colab_session
        self.done = False
        self.target: Optional[str] = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _sources() -> list[tuple[str, str]]:
        import lms_kernel

        src = Path(lms_kernel.__file__).resolve().parent.parent
        return [(f"{pkg}/{name}", (src / pkg / name).read_text(encoding="utf-8")) for pkg, name in HELPER_FILES]

    async def _contents(self, info, method: str, path: str, body: Optional[dict] = None) -> Optional[int]:
        req = httpclient.HTTPRequest(
            f"{info.url}/api/contents/{path}".rstrip("/"),
            method=method,
            headers={PROXY_TOKEN_HEADER: info.token, "Accept": "application/json",
                     **({"Content-Type": "application/json"} if body is not None else {})},
            body=json.dumps(body) if body is not None else None,
            follow_redirects=False,
            connect_timeout=CONNECT_TIMEOUT,
            request_timeout=REQUEST_TIMEOUT,
        )
        try:
            resp = await httpclient.AsyncHTTPClient().fetch(req, raise_error=False)
        except Exception:
            return None
        return None if resp.code == 599 else resp.code

    async def _missing(self) -> bool:
        """True if the copied helpers are gone, i.e. the Colab runtime container was restarted
        with a fresh filesystem (measured 2026-11-07). One small request per kernel start."""
        try:
            info = await self.colab.current()
        except Exception:
            return False
        prefix = self.target.strip("/") + "/" if self.target and self.target != "/" else ""
        if await self._contents(info, "GET", f"{prefix}lms_kernel/frontend.py?content=0") == 404:
            log.warning("kernel helpers are missing on the Colab runtime; copying them again")
            self.done = False
            return True
        return False

    async def ensure(self) -> bool:
        if self.done and not await self._missing():
            return True
        async with self._lock:
            if self.done:
                return True
            try:
                info = await self.colab.current()
            except Exception:
                return False
            for target in HELPER_TARGETS:
                if target and await self._contents(info, "GET", f"{target}?content=0") != 200:
                    continue
                prefix = f"{target}/" if target else ""
                ok = True
                for pkg in ("lms_kernel", "lms_viz"):
                    if await self._contents(info, "PUT", prefix + pkg, {"type": "directory"}) not in (200, 201):
                        ok = False
                for path, text in self._sources() if ok else []:
                    if await self._contents(info, "PUT", prefix + path,
                                            {"type": "file", "format": "text", "content": text}) not in (200, 201):
                        ok = False
                        break
                if ok:
                    self.done, self.target = True, "/" + target
                    from lms_kernel import __version__

                    print(f"Colab 런타임에 LMS 헬퍼(lms_kernel {__version__})를 설치했습니다 ({self.target}).",
                          flush=True)
                    return True
            log.warning("could not copy the kernel helpers to the Colab runtime")
            print(
                "[lms-runtime] Colab 런타임에 LMS 헬퍼를 설치하지 못했습니다. 변수 인스펙터·환경 점검이 동작하지 않을 수 "
                "있습니다. 새 커널을 만들 때 다시 시도합니다.",
                file=sys.stderr,
                flush=True,
            )
            return False


class RuntimeWatch:
    """Explains lost kernels: logs what the Colab VM shows and tells the student when the
    runtime container restarted (CLAUDE.md 3장, measured 2026-11-07).

    ``known`` holds kernels seen alive through this bridge, ``gone`` those the Colab
    Jupyter Server reported as missing (WebSockets to them are closed without contacting
    Colab again). Diagnostics are read at most every DIAG_INTERVAL seconds.
    """

    MAX_IDS = 512

    def __init__(self, colab_session: ColabSession, helper_installer: Optional["HelperInstaller"] = None,
                 diag_interval: float = DIAG_INTERVAL):
        self.colab = colab_session
        self.installer = helper_installer
        self.diag_interval = diag_interval
        self.known: set[str] = set()
        self.gone: set[str] = set()
        self.container_start = None
        self.oom_kill: Optional[int] = None
        self.restarts = 0
        self._last_diag: Optional[float] = None
        self._lock = asyncio.Lock()

    async def baseline(self) -> None:
        """Remember the current container start and OOM count (called at bridge start)."""
        info = self.colab.info  # never runs the CLI
        if info is None:
            return
        snap = await snapshot(info)
        self.container_start = snap.container_start or self.container_start
        self.oom_kill = snap.oom_kill if snap.oom_kill is not None else self.oom_kill

    def kernel_lost(self, kernel_id: str, reason: str) -> None:
        """Record that Colab no longer has `kernel_id`; diagnose if the bridge knew it."""
        if kernel_id in self.gone:
            return
        if len(self.gone) >= self.MAX_IDS:
            self.gone.clear()
        self.gone.add(kernel_id)
        if kernel_id not in self.known:
            return  # e.g. a kernel id the browser kept from an earlier runtime
        self.known.discard(kernel_id)
        asyncio.ensure_future(self._diagnose(kernel_id, reason))

    async def _diagnose(self, kernel_id: str, reason: str) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._last_diag is not None and now - self._last_diag < self.diag_interval:
                log.warning("kernel %s is gone on Colab (%s)", kernel_id[:8], reason)
                return
            self._last_diag = now
            info = self.colab.info  # never runs the CLI
            if info is None:
                log.warning("kernel %s is gone on Colab (%s); no diagnostics", kernel_id[:8], reason)
                return
            snap = await snapshot(info)
            restarted = (
                snap.container_start is not None and self.container_start is not None
                and abs((snap.container_start - self.container_start).total_seconds()) > 1
            )
            new_ooms = (
                snap.oom_kill - self.oom_kill
                if snap.oom_kill is not None and self.oom_kill is not None else 0
            )
            log.warning(
                "kernel %s is gone on Colab (%s): %s%s", kernel_id[:8], reason, snap.summary(),
                " runtime_restarted=yes" if restarted else "",
            )
            if snap.container_start is not None:
                self.container_start = snap.container_start
            if snap.oom_kill is not None:
                self.oom_kill = snap.oom_kill
            if restarted:
                self.restarts += 1
                self.known.clear()  # every kernel of the old container is gone
                if self.installer is not None:
                    self.installer.done = False  # the container filesystem was reset too
                local = snap.container_start.astimezone().strftime("%H:%M:%S")
                print(
                    f"\n[lms-runtime] Colab 런타임이 {local}에 재시작되었습니다(VM 할당은 그대로입니다). "
                    "실행 중이던 커널과 런타임에 저장한 파일(내려받은 모델 포함)이 지워졌습니다.\n"
                    "  LMS에서 새 커널로 이어서 실행하세요. 재시작은 Colab 쪽에서 일어났습니다"
                    + (f"(메모리 부족 종료 {new_ooms}회)." if new_ooms > 0 else "(메모리 부족 기록 없음)."),
                    file=sys.stderr,
                    flush=True,
                )
            elif new_ooms > 0:
                print(
                    f"\n[lms-runtime] Colab 런타임에서 메모리 부족으로 프로세스가 {new_ooms}회 종료되었습니다. "
                    "모델이나 배치 크기를 줄여 보세요.",
                    file=sys.stderr,
                    flush=True,
                )


def make_app(
    origin: str,
    token: str,
    colab_session: ColabSession,
    upstream_reconnect: bool = True,
    debug_drop_upstream_after: Optional[float] = None,
    debug_drop_reply_once: bool = False,
    helper_installer: Optional[HelperInstaller] = None,
) -> web.Application:
    kernel = rf"/api/kernels/(?:{ID})"
    return web.Application(
        [
            (r"/api/kernels", RestHandler),
            (kernel, RestHandler),
            (rf"{kernel}/(?:interrupt|restart)", RestHandler),
            (r"/api/kernelspecs", RestHandler),
            (rf"/api/kernels/({ID})/channels", ChannelsHandler),
            (r"/lms/runtime", RuntimeInfoHandler),
            (r"/lms/colab/usage", ColabUsageHandler),
        ],
        lms_origin=origin,
        lms_token=token,
        colab_session=colab_session,
        usage_cache=UsageCache(colab_session),
        upstream_reconnect=upstream_reconnect,
        debug_drop_upstream_after=debug_drop_upstream_after,
        reply_dropper=ReplyDropper() if debug_drop_reply_once else None,
        helper_installer=helper_installer,
        runtime_watch=RuntimeWatch(colab_session, helper_installer),
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
    helper_installer: Optional[HelperInstaller] = None,
):
    """Bind 127.0.0.1:<port> (raises OSError if taken). Call inside a running event loop."""
    app = make_app(
        origin, token, colab_session, upstream_reconnect, debug_drop_upstream_after,
        debug_drop_reply_once, helper_installer,
    )
    server = app.listen(port, address="127.0.0.1")
    asyncio.ensure_future(app.settings["runtime_watch"].baseline())
    return server


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


async def upstream_get(info, path: str, timeout: float) -> tuple[Optional[int], bytes]:
    """(status, body) of GET <path> on the Colab runtime; status None if unreachable."""
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
        return None, b""
    return (None, b"") if resp.code == 599 else (resp.code, resp.body or b"")


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
