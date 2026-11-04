"""Start the LMS student runtime.

Usage:
    lms-runtime --origin https://lms.example.com [--port 8888]
    lms-runtime --colab <session> --origin https://lms.example.com [--port 8888]

Without ``--colab`` a local Jupyter Server is started. With ``--colab`` a local bridge
relays the same Jupyter API to a Colab runtime allocated by the Colab CLI.
"""

import argparse
import asyncio
import logging
import os
import secrets
import shutil
import subprocess
import sys

DEFAULT_ORIGIN = "http://localhost:3000"
# Contract 2.1. Change the contract first if these defaults change.
DEFAULT_IDLE_TIMEOUT_MINUTES = 0  # local: off
DEFAULT_COLAB_IDLE_TIMEOUT_MINUTES = 60  # colab: return the VM with `colab stop`
CULL_INTERVAL_SECONDS = 300
DEBUG_DROP_REPLY_NOTICE = (
    "[진단] --debug-drop-reply-once: 커널마다 첫 실행의 응답(execute_reply, idle)을 브라우저로 보내지 않습니다.\n"
    "  헬퍼 호출도 실행으로 셉니다. LMS 프론트는 커널에 붙자마자 info()를 부르므로, 첫 셀이 아니라 "
    "이 헬퍼의 응답이 버려집니다."
)


COLAB_NOTICE = (
    "참고: Colab 런타임은 LMS나 이 브리지의 설정과 관계없이 Colab 정책에 따라 예고 없이 종료될 수 있습니다\n"
    "  (컴퓨트 유닛 소진, 최대 사용 시간, 사용량 제한 등). 오래 걸리는 학습은 중간 결과를 저장하세요.\n"
    "  Colab은 커널이 아니라 런타임(VM) 단위로 컴퓨트 유닛을 씁니다. LMS 페이지를 닫거나 커널이 종료돼도 "
    "런타임을 반납하기 전까지는 계속 쓰입니다.\n"
    "  {idle}다 쓰면 `colab stop -s {session}`으로 바로 반납하세요."
)
COLAB_IDLE_ON = "모든 커널이 {minutes}분 동안 쉬면 브리지가 런타임을 자동으로 반납합니다(--idle-timeout, 0이면 끔). "
COLAB_IDLE_OFF = "자동 반납이 꺼져 있습니다(--idle-timeout 0). "
COLAB_IDLE_STOPPED = (
    "\n[lms-runtime] 모든 커널이 {minutes}분 동안 쉬어서 Colab 런타임을 반납했습니다(`colab stop -s {session}`). "
    "이제 컴퓨트 유닛이 쓰이지 않습니다.\n"
    "  다시 쓰려면 `colab new -s {session}`으로 런타임을 새로 받은 뒤 lms-runtime을 다시 실행하세요."
)
ROOT_NOTICE = (
    "참고: root 계정으로 실행 중입니다. LMS에서 실행하는 코드도 root 권한으로 실행됩니다."
)


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def build_command(
    origin: str,
    port: int,
    token: str,
    debug_drop_reply_once: bool = False,
    idle_timeout_minutes: int = DEFAULT_IDLE_TIMEOUT_MINUTES,
    allow_root: bool | None = None,
) -> list[str]:
    cmd = [
        sys.executable, "-m", "lms_kernel.server",
        "--no-browser",
        f"--port={port}",
        "--ServerApp.ip=127.0.0.1",
        f"--ServerApp.allow_origin={origin}",
        f"--IdentityProvider.token={token}",
        "--ServerApp.identity_provider_class=lms_kernel.auth.StrictOriginIdentityProvider",
    ]
    if running_as_root() if allow_root is None else allow_root:
        # Jupyter refuses root by default, a guard meant for shared servers. This runtime only
        # runs the student's own code on their own PC (often a root-only WSL distro), so allow it.
        cmd.append("--ServerApp.allow_root=True")
    if idle_timeout_minutes > 0:
        # Contract 2.1: cull idle kernels even with a tab still connected, never busy ones.
        cmd += [
            f"--MappingKernelManager.cull_idle_timeout={idle_timeout_minutes * 60}",
            f"--MappingKernelManager.cull_interval={CULL_INTERVAL_SECONDS}",
            "--MappingKernelManager.cull_connected=True",
            "--MappingKernelManager.cull_busy=False",
        ]
    if debug_drop_reply_once:
        cmd.append(
            "--ServerApp.kernel_websocket_connection_class=lms_kernel.debug.DropReplyOnceConnection"
        )
    return cmd


def print_registration(port: int, token: str) -> None:
    print("LMS 런타임 등록 정보 (LMS 사이트의 런타임 설정에 입력하세요)", flush=True)
    print(f"  URL  : http://127.0.0.1:{port}", flush=True)
    print(f"  Token: {token}", flush=True)


def run_colab_bridge(args, token: str) -> int:
    from .bridge import keep_fresh, probe, start, stop_when_idle
    from .colab_session import (
        PINNED_CLI_VERSION, ColabSession, SessionFormatError, SessionGone, cli_version, loose_config_dir,
    )

    if shutil.which(args.colab_bin) is None:
        print(
            f"`{args.colab_bin}` 명령을 찾을 수 없습니다. Colab CLI를 설치하고 PATH에 추가하세요.",
            file=sys.stderr,
        )
        return 1

    logging.basicConfig(level=logging.WARNING, format="[lms-runtime] %(message)s")
    version = cli_version(args.colab_bin)
    if version != PINNED_CLI_VERSION:
        found = f"설치된 버전은 {version}입니다" if version else "설치된 버전을 확인하지 못했습니다"
        print(
            f"경고: lms-runtime은 Colab CLI {PINNED_CLI_VERSION}에서 검증했습니다({found}). "
            "세션 파일 형식이 다르면 브리지가 동작하지 않을 수 있습니다.\n"
            f"  `pip install google-colab-cli=={PINNED_CLI_VERSION}`로 맞추는 것을 권합니다.",
            file=sys.stderr,
            flush=True,
        )
    loose = loose_config_dir(args.colab_config)
    if loose:
        directory, mode = loose
        print(
            f"경고: {directory} 권한이 {mode:o}입니다. Colab CLI는 이 폴더의 로그와 파일에 "
            "Google 로그인 자격증명과 런타임 토큰을 평문으로 남깁니다.\n"
            f"  다른 사용자가 읽지 못하도록 `chmod 700 {directory}`를 실행하세요.",
            file=sys.stderr,
            flush=True,
        )
    session = ColabSession(args.colab, sessions_path=args.colab_config, colab_cmd=args.colab_bin)

    async def main() -> int:
        print(f"Colab 세션 '{args.colab}' 확인 중...", flush=True)
        try:
            info = await session.refresh()
        except SessionFormatError as e:
            print(e, file=sys.stderr, flush=True)
            return 1
        except SessionGone:
            print(
                "  `colab sessions`로 세션 이름을 확인하거나 `colab new -s <이름>`으로 새로 만드세요.",
                file=sys.stderr,
            )
            return 1
        hw = "CPU" if info.accelerator == "NONE" else info.accelerator
        if not await probe(info):
            print(
                "Colab 런타임이 응답하지 않습니다. `colab status`로 세션 상태를 확인하세요. "
                "브리지는 계속 실행하며, 런타임이 응답하면 바로 사용할 수 있습니다.",
                file=sys.stderr,
                flush=True,
            )
        try:
            server = start(
                args.origin, args.port, token, session,
                upstream_reconnect=not args.no_upstream_reconnect,
                debug_drop_upstream_after=args.debug_drop_upstream_after,
                debug_drop_reply_once=args.debug_drop_reply_once,
            )
        except OSError as e:
            print(f"포트 {args.port}를 열 수 없습니다: {e.strerror}", file=sys.stderr)
            return 1
        print(f"Colab 런타임에 연결했습니다 (세션 '{args.colab}', {hw}).", flush=True)
        idle = args.idle_timeout
        idle_text = COLAB_IDLE_ON.format(minutes=idle) if idle > 0 else COLAB_IDLE_OFF
        print(COLAB_NOTICE.format(session=args.colab, idle=idle_text), flush=True)
        if args.debug_drop_reply_once:
            print(DEBUG_DROP_REPLY_NOTICE, flush=True)
        print_registration(args.port, token)
        print("종료하려면 Ctrl+C를 누르세요. Colab 런타임은 `colab stop`으로 따로 종료합니다.", flush=True)
        async def idle_stop():
            if await stop_when_idle(session, idle * 60):
                print(COLAB_IDLE_STOPPED.format(minutes=idle, session=args.colab), file=sys.stderr, flush=True)

        idle_task = asyncio.ensure_future(idle_stop()) if idle > 0 else None
        try:
            await keep_fresh(session)
            # Session is gone: keep answering 503 so the browser gets a clear error.
            await asyncio.Event().wait()
        finally:
            if idle_task is not None:
                idle_task.cancel()
            server.stop()
        return 0

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the LMS student runtime")
    parser.add_argument("--origin", default=DEFAULT_ORIGIN, help="LMS site origin allowed to connect")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument(
        "--idle-timeout",
        type=int,
        metavar="MINUTES",
        default=None,
        help=(
            "auto shutdown after MINUTES with every kernel idle; busy kernels are kept, 0 disables. "
            f"Local: shut down idle kernels (default: {DEFAULT_IDLE_TIMEOUT_MINUTES}, off). "
            f"Colab: return the VM with `colab stop` (default: {DEFAULT_COLAB_IDLE_TIMEOUT_MINUTES})"
        ),
    )
    parser.add_argument(
        "--colab",
        metavar="SESSION",
        help="relay a Colab runtime allocated with `colab new -s SESSION` instead of starting a local kernel",
    )
    parser.add_argument(
        "--colab-config",
        metavar="PATH",
        default=None,
        help="Colab CLI session file (default: ~/.config/colab-cli/sessions.json)",
    )
    parser.add_argument("--colab-bin", default="colab", help="Colab CLI executable (default: colab)")
    parser.add_argument(
        "--no-upstream-reconnect",
        action="store_true",
        help=argparse.SUPPRESS,  # diagnostics: close the browser WebSocket on every upstream drop
    )
    parser.add_argument(
        "--debug-drop-upstream-after",
        type=float,
        metavar="SECONDS",
        default=None,
        help=argparse.SUPPRESS,  # diagnostics: close each Colab-side WebSocket once after N s
    )
    parser.add_argument(
        "--debug-drop-reply-once",
        action="store_true",
        help=(
            "diagnostics: per kernel, do not send the first execute_reply and its idle status "
            "to the browser. This counts every execution, including silent helper calls: the "
            "LMS frontend calls lms_kernel.frontend.info() right after connecting, so that "
            "helper reply is the one dropped, not the first cell's"
        ),
    )
    args = parser.parse_args()
    if args.idle_timeout is not None and args.idle_timeout < 0:
        parser.error("--idle-timeout must be 0 or greater")

    token = secrets.token_urlsafe(32)
    if args.colab:
        if args.idle_timeout is None:
            args.idle_timeout = DEFAULT_COLAB_IDLE_TIMEOUT_MINUTES
        sys.exit(run_colab_bridge(args, token))

    print_registration(args.port, token)
    if running_as_root():
        print(ROOT_NOTICE, flush=True)
    if args.debug_drop_reply_once:
        print(DEBUG_DROP_REPLY_NOTICE, flush=True)
    idle = DEFAULT_IDLE_TIMEOUT_MINUTES if args.idle_timeout is None else args.idle_timeout
    if idle > 0:
        print(f"{idle}분 동안 쓰지 않은 커널은 자동으로 종료됩니다 (--idle-timeout, 0이면 끔).", flush=True)
    subprocess.run(
        build_command(
            args.origin, args.port, token,
            debug_drop_reply_once=args.debug_drop_reply_once,
            idle_timeout_minutes=idle,
        ),
        check=False,
    )


if __name__ == "__main__":
    main()
