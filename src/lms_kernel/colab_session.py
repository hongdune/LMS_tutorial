"""Read Colab CLI session state and refresh it through the CLI itself.

We only touch the Colab CLI through stable-ish boundaries (CLAUDE.md 3장):
- ``~/.config/colab-cli/sessions.json`` for the runtime proxy URL and token
- ``colab status -s <name>`` to make the CLI refresh an expiring proxy token or
  prune a session whose assignment is gone server-side.

Nothing here calls Colab's ``/tun/m/*`` API or uses the CLI's Python modules.
Tokens are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("lms_kernel.colab")

DEFAULT_SESSIONS_PATH = os.path.expanduser("~/.config/colab-cli/sessions.json")
# Same headroom the CLI uses before it refreshes a token itself.
REFRESH_MARGIN = timedelta(minutes=5)
# Each `colab` call hits the backend (and the CLI writes a debug log), so throttle.
MIN_REFRESH_INTERVAL = 30.0
CLI_TIMEOUT = 120.0


def loose_config_dir(sessions_path: Optional[str] = None) -> Optional[tuple[str, int]]:
    """(dir, mode) if the Colab CLI config directory is readable by other users, else None.

    The CLI writes OAuth credentials (including the refresh token) and proxy tokens to
    ``colab.log``, ``token.json`` and ``sessions.json`` in that directory with mode 0644,
    so the directory itself must be 0700. We only warn; we never change or delete files.
    """
    directory = os.path.dirname(os.path.abspath(sessions_path or DEFAULT_SESSIONS_PATH))
    try:
        mode = stat.S_IMODE(os.stat(directory).st_mode)
    except OSError:
        return None
    return (directory, mode) if mode & 0o077 else None


class SessionGone(Exception):
    """The Colab session no longer exists (stopped, reclaimed, or never created)."""


@dataclass(frozen=True)
class ProxyInfo:
    url: str
    token: str
    expires_at: Optional[datetime]
    accelerator: str = "NONE"

    def __repr__(self) -> str:  # never show the token
        return f"ProxyInfo(url={self.url!r}, expires_at={self.expires_at!r})"


def _parse_time(value) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def read_session(path: str, name: str) -> Optional[ProxyInfo]:
    """Return the session's proxy info, or None if the session is not in the file."""
    for attempt in range(5):
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            return None
        if not content.strip():
            return None
        try:
            data = json.loads(content)
            break
        except json.JSONDecodeError:
            # The CLI truncates and rewrites the file; we may have caught it mid-write.
            time.sleep(0.1 * (attempt + 1))
    else:
        raise RuntimeError(f"Colab CLI 세션 파일을 읽을 수 없습니다: {path}")

    entry = data.get(name)
    if not entry or not entry.get("url") or not entry.get("token"):
        return None
    return ProxyInfo(
        url=entry["url"].rstrip("/"),
        token=entry["token"],
        expires_at=_parse_time(entry.get("token_expires_at")),
        accelerator=entry.get("accelerator") or "NONE",
    )


class ColabSession:
    """Current proxy URL/token for one Colab CLI session, refreshed via the CLI."""

    def __init__(
        self,
        name: str,
        sessions_path: Optional[str] = None,
        colab_cmd: str = "colab",
        min_refresh_interval: float = MIN_REFRESH_INTERVAL,
        refresh_margin: timedelta = REFRESH_MARGIN,
    ):
        self.name = name
        self.sessions_path = sessions_path or DEFAULT_SESSIONS_PATH
        self._custom_path = sessions_path is not None
        self.colab_cmd = colab_cmd
        self.min_refresh_interval = min_refresh_interval
        self.refresh_margin = refresh_margin
        self.gone = False
        self._info: Optional[ProxyInfo] = None
        self._last_refresh: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def info(self) -> Optional[ProxyInfo]:
        return self._info

    def _needs_refresh(self) -> bool:
        if self._info is None:
            return True
        exp = self._info.expires_at
        return exp is None or exp - datetime.now(timezone.utc) <= self.refresh_margin

    async def current(self) -> ProxyInfo:
        if self.gone:
            raise SessionGone(self.name)
        if self._needs_refresh():
            return await self.refresh()
        return self._info

    def check_local(self) -> bool:
        """Re-read sessions.json without calling the CLI; False once the session is gone.

        `colab stop` removes the session from the file immediately, while the Colab
        proxy keeps answering for a minute or more (measured 2026-11-03: ~85s REST 200,
        WebSocket closed after ~100s).
        """
        if self.gone:
            return False
        info = read_session(self.sessions_path, self.name)
        if info is None:
            self._mark_gone()
            return False
        if self._info is None or info.token != self._info.token or info.url != self._info.url:
            self._info = info  # the CLI refreshed it on its own (e.g. a `colab` command)
        return True

    def _mark_gone(self) -> None:
        self.gone = True
        self._info = None
        print(
            f"\n[lms-runtime] Colab 세션 '{self.name}'을(를) 찾을 수 없습니다. "
            "런타임이 종료되었거나 회수되었습니다.\n"
            "  `colab new`로 세션을 새로 만든 뒤 lms-runtime을 다시 실행하세요.",
            file=sys.stderr,
            flush=True,
        )

    async def refresh(self) -> ProxyInfo:
        """Ask the CLI to refresh the session, then re-read sessions.json.

        Throttled to one CLI call per ``min_refresh_interval``. Raises
        SessionGone if the CLI pruned the session.
        """
        async with self._lock:
            if self.gone:
                raise SessionGone(self.name)
            now = time.monotonic()
            throttled = (
                self._last_refresh is not None
                and now - self._last_refresh < self.min_refresh_interval
            )
            if throttled and self._info is not None:
                return self._info
            self._last_refresh = now
            await self._run_cli()

            info = read_session(self.sessions_path, self.name)
            if info is None:
                self._mark_gone()
                raise SessionGone(self.name)
            self._info = info
            return info

    async def _run_cli(self) -> bool:
        cmd = [self.colab_cmd]
        if self._custom_path:
            cmd += ["--config", self.sessions_path]
        cmd += ["status", "-s", self.name]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            print(f"[lms-runtime] `{self.colab_cmd}` 명령을 찾을 수 없습니다.", file=sys.stderr, flush=True)
            return False
        try:
            code = await asyncio.wait_for(proc.wait(), CLI_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            code = None
        if code != 0:
            print(
                "[lms-runtime] Colab CLI로 세션 정보를 갱신하지 못했습니다. "
                f"터미널에서 `colab status -s {self.name}`을 실행해 로그인 상태를 확인하세요.",
                file=sys.stderr,
                flush=True,
            )
            return False
        return True
