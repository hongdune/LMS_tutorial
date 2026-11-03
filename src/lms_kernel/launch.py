"""Start a Jupyter Server that only accepts requests from the LMS origin.

Usage:
    lms-runtime --origin https://lms.example.com [--port 8888]
"""

import argparse
import secrets
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the LMS student runtime")
    parser.add_argument("--origin", default=DEFAULT_ORIGIN, help="LMS site origin allowed to connect")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()

    token = secrets.token_urlsafe(32)
    print("LMS 런타임 등록 정보 (LMS 사이트의 런타임 설정에 입력하세요)", flush=True)
    print(f"  URL  : http://127.0.0.1:{args.port}", flush=True)
    print(f"  Token: {token}", flush=True)
    subprocess.run(build_command(args.origin, args.port, token), check=False)


if __name__ == "__main__":
    main()
