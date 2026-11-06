"""Identity provider that makes a local Jupyter Server behave like the Colab runtime proxy.

The Colab proxy authenticates with ``X-Colab-Runtime-Proxy-Token`` (REST) and the
``colab-runtime-proxy-token`` query parameter (WebSocket) instead of Jupyter's own
``Authorization: token`` / ``?token=``. Requests using Jupyter's own scheme are rejected
so tests prove the bridge rewrites authentication.

Like the real proxy (measured 2026-11-07), a missing or wrong proxy token gets 404 with an
empty body, not 401/403; the Jupyter Server's own 404 for an unknown kernel has a JSON body.
"""

from jupyter_server.auth.identity import IdentityProvider
from tornado import web


class FakeColabProxyIdentityProvider(IdentityProvider):
    def get_token(self, handler):
        return (
            handler.request.headers.get("X-Colab-Runtime-Proxy-Token")
            or handler.get_argument("colab-runtime-proxy-token", None)
        )

    async def get_user(self, handler):
        if self.get_token(handler) != self.token:
            handler.clear()
            handler.set_status(404)
            handler.finish()
            raise web.Finish()
        return await super().get_user(handler)
