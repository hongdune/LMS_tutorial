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
import secrets
import shutil
import subprocess
import sys

DEFAULT_ORIGIN = "http://localhost:3000"


def build_command(origin: str, port: int, token: str) -> list[str]:
    return [
        sys.executable, "-m", "jupyter_server",
        "--no-browser",
        f"--port={port}",
        "--ServerApp.ip=127.0.0.1",
        f"--ServerApp.allow_origin={origin}",
        f"--IdentityProvider.token={token}",
        "--ServerApp.identity_provider_class=lms_kernel.auth.StrictOriginIdentityProvider",
    ]


def print_registration(port: int, token: str) -> None:
    print("LMS 런타임 등록 정보 (LMS 사이트의 런타임 설정에 입력하세요)", flush=True)
    print(f"  URL  : http://127.0.0.1:{port}", flush=True)
    print(f"  Token: {token}", flush=True)


def run_colab_bridge(args, token: str) -> int:
    from .bridge import keep_fresh, probe, start
    from .colab_session import ColabSession, SessionGone, loose_config_dir

    if shutil.which(args.colab_bin) is None:
        print(
            f"`{args.colab_bin}` 명령을 찾을 수 없습니다. Colab CLI를 설치하고 PATH에 추가하세요.",
            file=sys.stderr,
        )
        return 1

    logging.basicConfig(level=logging.WARNING, format="[lms-runtime] %(message)s")
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
            )
        except OSError as e:
            print(f"포트 {args.port}를 열 수 없습니다: {e.strerror}", file=sys.stderr)
            return 1
        print(f"Colab 런타임에 연결했습니다 (세션 '{args.colab}', {hw}).", flush=True)
        print_registration(args.port, token)
        print("종료하려면 Ctrl+C를 누르세요. Colab 런타임은 `colab stop`으로 따로 종료합니다.", flush=True)
        try:
            await keep_fresh(session)
            # Session is gone: keep answering 503 so the browser gets a clear error.
            await asyncio.Event().wait()
        finally:
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
    args = parser.parse_args()

    token = secrets.token_urlsafe(32)
    if args.colab:
        sys.exit(run_colab_bridge(args, token))

    print_registration(args.port, token)
    subprocess.run(build_command(args.origin, args.port, token), check=False)


if __name__ == "__main__":
    main()
