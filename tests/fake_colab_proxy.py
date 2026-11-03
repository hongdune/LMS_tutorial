"""Identity provider that makes a local Jupyter Server behave like the Colab runtime proxy.

The Colab proxy authenticates with ``X-Colab-Runtime-Proxy-Token`` (REST) and the
``colab-runtime-proxy-token`` query parameter (WebSocket) instead of Jupyter's own
``Authorization: token`` / ``?token=``. Requests using Jupyter's own scheme are rejected
so tests prove the bridge rewrites authentication.
"""

from jupyter_server.auth.identity import IdentityProvider


class FakeColabProxyIdentityProvider(IdentityProvider):
    def get_token(self, handler):
        return (
            handler.request.headers.get("X-Colab-Runtime-Proxy-Token")
            or handler.get_argument("colab-runtime-proxy-token", None)
        )
