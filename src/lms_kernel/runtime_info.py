"""Body of ``GET /lms/runtime`` (contract 2.2), shared by the local server and the bridge.

Answers from memory only: no Colab CLI call, no upstream request.
"""

from __future__ import annotations

from typing import Optional

from . import __version__
from .frontend import CONTRACT_VERSION

SCHEMA_VERSION = 1


def runtime_info(kind: str, colab_session: Optional[str] = None, machine: Optional[str] = None) -> dict:
    return {
        "version": SCHEMA_VERSION,
        "kind": kind,
        "lms_runtime": __version__,
        "contract": CONTRACT_VERSION,
        "colab": {"session": colab_session, "machine": machine} if kind == "colab" else None,
    }
