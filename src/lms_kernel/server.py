"""Jupyter Server entry point for the local runtime.

Identical to ``python -m jupyter_server`` except that
- ``Access-Control-Allow-Origin`` is pinned on *every* response, including errors;
- ``GET /lms/runtime`` (contract 2.2) tells the LMS which runtime and contract it talks to;
- with ``--LMSServerApp.ignore_sigint=True`` (set by ``lms-runtime``) Ctrl+C in the terminal
  is left to the parent ``lms-runtime`` process, which asks for a second Ctrl+C and then
  stops this server with SIGTERM.

Why: Jupyter Server sets CORS headers in ``prepare()`` (after authentication). When a
handler raises (404 for an unknown kernel, 403 for a bad token, ...), tornado's
``send_error()`` calls ``clear()`` and only ``set_default_headers()`` runs again, so
error responses carry no ACAO. The browser then reports a CORS failure instead of the
status, and the LMS frontend cannot tell "kernel gone (404)" from "runtime unreachable"
(measured 2026-11-03: a kernel id saved from an earlier runtime → 404 without ACAO →
the frontend showed "연결 실패" instead of creating a new kernel).

``settings["headers"]`` is applied in ``set_default_headers()``, which survives
``clear()``. The value is the single allowed origin (contract 2장), so it does not
widen access: other origins still get 404 / WebSocket rejection from
``StrictOriginIdentityProvider``, and their browsers reject the mismatching ACAO.
"""

import json
import signal

from jupyter_server.base.handlers import APIHandler
from jupyter_server.serverapp import ServerApp
from jupyter_server.utils import url_path_join
from tornado import web
from traitlets import Bool

from .runtime_info import runtime_info


class RuntimeInfoHandler(APIHandler):
    """GET /lms/runtime (contract 2.2). APIHandler gives the same token, Origin
    (StrictOriginIdentityProvider) and CORS handling as /api/kernels."""

    SUPPORTED_METHODS = ("GET", "OPTIONS")

    @web.authenticated
    def get(self):
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(runtime_info("local")))


class LMSServerApp(ServerApp):
    ignore_sigint = Bool(
        False, config=True, help="Ignore SIGINT; the parent lms-runtime handles Ctrl+C."
    )

    def init_signal(self):
        super().init_signal()
        if self.ignore_sigint:
            signal.signal(signal.SIGINT, signal.SIG_IGN)

    def init_webapp(self):
        if self.allow_origin:
            headers = dict(self.tornado_settings.get("headers") or {})
            headers["Access-Control-Allow-Origin"] = self.allow_origin
            self.tornado_settings = {**self.tornado_settings, "headers": headers}
        super().init_webapp()
        self.web_app.add_handlers(
            ".*$", [(url_path_join(self.base_url, "/lms/runtime"), RuntimeInfoHandler)]
        )


main = launch_new_instance = LMSServerApp.launch_instance

if __name__ == "__main__":
    main()
