"""Identity provider that enforces the Origin check even for token-authenticated requests.

Jupyter Server skips the Origin check whenever a request carries a valid token, so
``allow_origin`` alone only shapes CORS headers. Without this class, any web page that
learned the token could open a kernel WebSocket from its own origin. Requests without an
Origin header (scripts, CLI tools) are still allowed by Jupyter's own check_origin.
"""

from jupyter_server.auth.identity import PasswordIdentityProvider


class StrictOriginIdentityProvider(PasswordIdentityProvider):
    def should_check_origin(self, handler) -> bool:
        return True
