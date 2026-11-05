"""Body of ``GET /lms/colab/usage`` (contract 2.3): Colab compute units, from the CLI.

Runs `colab usage` and `colab sessions` (google-colab-cli 0.7.5) and parses their text
output. Formats, from the CLI source (consumption.py, commands/session.py) and measured
2026-11-06 on a T4:

    $ colab usage
    Current balance: 576.13 compute units
    Usage rate: 1.15/hr
    Active assignments: 1

    $ colab sessions
    [fmt] gpu-t4-s-<region>-<id> | Hardware: T4 | Shape: Standard | Variant: GPU
    (or "[colab] No active sessions found on server."; "[?]" is a server-side
    assignment with no local session)

Values that do not parse become ``null`` and that command's output (stripped, at most
RAW_LIMIT characters) goes in ``raw.usage`` / ``raw.sessions``; ``raw`` is ``null`` when
everything parsed. The CLI writes credentials to its log file, not to stdout; ``raw`` is
still scrubbed of anything token-shaped.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Optional

SCHEMA_VERSION = 1
CACHE_SECONDS = 30.0
RAW_LIMIT = 4000
CLI_TIMEOUT = 30.0

_BALANCE = re.compile(r"^\s*Current balance:\s*(-?\d+(?:\.\d+)?)\s*compute units\s*$", re.M)
_RATE = re.compile(r"^\s*Usage rate:\s*(-?\d+(?:\.\d+)?)\s*/\s*hr\s*$", re.M)
_SESSION = re.compile(r"^\[(?P<name>[^\]]*)\]\s+\S+\s*\|\s*Hardware:\s*(?P<hw>[^|]+?)\s*(?:\||$)")
_NO_SESSIONS = "No active sessions found"
# Google OAuth access/refresh tokens, JWT-like strings and long opaque tokens.
_SECRETS = re.compile(r"ya29\.[\w.-]+|1//[\w.-]+|eyJ[\w.-]{20,}|[A-Za-z0-9_\-]{40,}")


def scrub(text: str) -> str:
    return _SECRETS.sub("<redacted>", text)


def parse_usage(text: str) -> tuple[Optional[float], Optional[float]]:
    balance = _BALANCE.search(text)
    rate = _RATE.search(text)
    return (
        float(balance.group(1)) if balance else None,
        float(rate.group(1)) if rate else None,
    )


def parse_sessions(text: str, current: str) -> Optional[list]:
    """Sessions, or None if any non-empty line is not understood."""
    sessions = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _NO_SESSIONS in line:
            continue
        m = _SESSION.match(line)
        if m is None:
            return None
        name = m.group("name")
        sessions.append({
            "name": None if name == "?" else name,
            "machine": m.group("hw"),
            "current": name == current,
        })
    if not sessions and _NO_SESSIONS not in text:
        return None  # empty or unrecognised output
    return sessions


def _raw(text: str) -> str:
    return scrub(text.strip())[:RAW_LIMIT]


def build(usage_text: str, sessions_text: str, current: str, checked_at: Optional[str] = None) -> dict:
    balance, rate = parse_usage(usage_text)
    sessions = parse_sessions(sessions_text, current)
    raw_usage = _raw(usage_text) if balance is None or rate is None else None
    raw_sessions = _raw(sessions_text) if sessions is None else None
    raw = None
    if raw_usage is not None or raw_sessions is not None:
        raw = {"usage": raw_usage, "sessions": raw_sessions}
    return {
        "version": SCHEMA_VERSION,
        "checked_at": checked_at or _now(),
        "balance": balance,
        "rate_per_hour": rate,
        "sessions": sessions,
        "raw": raw,
    }


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class UsageCache:
    """Runs the two CLI commands at most once per CACHE_SECONDS (failures are not cached)."""

    def __init__(self, colab_session, cache_seconds: float = CACHE_SECONDS):
        self.colab = colab_session
        self.cache_seconds = cache_seconds
        self._value: Optional[dict] = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> dict:
        async with self._lock:  # concurrent requests share one CLI run
            if self._value is not None and time.monotonic() - self._at < self.cache_seconds:
                return self._value
            usage = await self.colab.cli_output(["usage"], CLI_TIMEOUT)
            sessions = await self.colab.cli_output(["sessions"], CLI_TIMEOUT)
            self._value = build(usage, sessions, self.colab.name)
            self._at = time.monotonic()
            return self._value
