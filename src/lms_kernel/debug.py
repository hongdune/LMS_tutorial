"""Diagnostics for checking how the LMS frontend recovers from lost kernel replies.

``--debug-drop-reply-once`` (local and Colab mode): for each kernel, the
``execute_reply`` and the ``status: idle`` of the first non-silent execution (the
student's first cell) are not sent to the browser. This reproduces the case where a
WebSocket drop loses the reply, which a real drop does only occasionally. The kernel
itself is untouched.

Silent executions (helper calls, contract 4.1, e.g. ``info()`` right after connecting)
are skipped. Only outgoing messages are seen, so the target is the first execution that
publishes ``execute_input``: kernels publish it for non-silent requests only, before the
reply.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("lms_kernel.debug")


class ReplyDropper:
    """Decides, per kernel, which messages to drop. Order-independent: the idle status
    can arrive before or after the execute_reply (different channels)."""

    def __init__(self):
        self._state: dict[str, dict] = {}

    def should_drop(self, kernel_id: str, msg: dict) -> bool:
        st = self._state.setdefault(kernel_id, {"target": None, "left": {"reply", "idle"}})
        if not st["left"]:
            return False
        header = msg.get("header") or {}
        parent = msg.get("parent_header") or {}
        msg_type = header.get("msg_type") or msg.get("msg_type")
        if msg_type == "execute_input":
            if st["target"] is None:
                st["target"] = parent.get("msg_id")
            return False
        if msg_type == "execute_reply":
            kind = "reply"
        elif (
            msg_type == "status"
            and (msg.get("content") or {}).get("execution_state") == "idle"
            and parent.get("msg_type") == "execute_request"
        ):
            kind = "idle"
        else:
            return False
        parent_id = parent.get("msg_id")
        if st["target"] is None or parent_id != st["target"] or kind not in st["left"]:
            return False
        st["left"].discard(kind)
        log.warning("debug: dropped %s for kernel %s", msg_type, kernel_id)
        return True


# Local mode: plugged into Jupyter Server with
# --ServerApp.kernel_websocket_connection_class=lms_kernel.debug.DropReplyOnceConnection
try:
    from jupyter_server.services.kernels.connection.channels import (
        ZMQChannelsWebsocketConnection,
    )
except ImportError:  # pragma: no cover - jupyter_server is a dependency
    ZMQChannelsWebsocketConnection = None


if ZMQChannelsWebsocketConnection is not None:

    class DropReplyOnceConnection(ZMQChannelsWebsocketConnection):
        """Standard kernel WebSocket connection that applies ReplyDropper to outgoing messages."""

        _dropper = ReplyDropper()

        def _peek(self, outgoing_msg) -> Optional[dict]:
            try:
                _, fed = self.session.feed_identities(outgoing_msg)
                # fed = [signature, header, parent_header, metadata, content, *buffers]
                return {
                    "header": self.session.unpack(fed[1]),
                    "parent_header": self.session.unpack(fed[2]),
                    "content": self.session.unpack(fed[4]),
                }
            except Exception:
                return None

        def handle_outgoing_message(self, stream, outgoing_msg):
            msg = self._peek(outgoing_msg)
            if msg is not None and self._dropper.should_drop(self.kernel_id, msg):
                return
            super().handle_outgoing_message(stream, outgoing_msg)
