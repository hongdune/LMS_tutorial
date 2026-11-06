"""install.sh into a throwaway HOME (local runtime only; the Colab CLI step is checked by hand).

Installs this checkout with --source, so it needs network access for the dependencies.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "install.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash installer")


def _run(home: Path, *args, path=None):
    env = {"HOME": str(home), "PATH": path or os.environ["PATH"], "TERM": "dumb", "SHELL": "/bin/bash"}
    # Piped like `curl ... | bash -s -- ...`.
    return subprocess.run(["bash", "-s", "--", *args], input=SCRIPT.read_text(), env=env,
                          capture_output=True, text=True, timeout=600)


def test_installs_into_home_and_is_safe_to_rerun(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # The Python running the tests can create venvs; put it first so the script picks it.
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "python3").symlink_to(sys.executable)
    path = f"{shim}:{os.environ['PATH']}"

    first = _run(home, "--source", str(REPO), "--origin", "https://lms.test", path=path)
    assert first.returncode == 0, first.stdout + first.stderr
    link = home / ".local/bin/lms-runtime"
    assert link.resolve() == (home / ".lms-runtime/venv/bin/lms-runtime").resolve()
    assert "lms-runtime --origin https://lms.test" in first.stdout
    assert "건너뜀" in first.stdout  # no --colab
    help_ = subprocess.run([str(link), "--help"], capture_output=True, text=True, timeout=60)
    assert help_.returncode == 0 and "--colab" in help_.stdout

    second = _run(home, "--source", str(REPO), path=path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "이미 설정돼 있음" in second.stdout
    assert (home / ".bashrc").read_text().count(".local/bin") == 1


def test_reports_missing_venv_module(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    real = shutil.which("python3") or sys.executable
    for name in ("python3", "python3.10", "python3.11", "python3.12", "python3.13", "python3.14"):
        script = fake / name
        script.write_text(f'#!/bin/sh\ncase "$*" in *ensurepip*) exit 1;; esac\nexec "{real}" "$@"\n')
        script.chmod(0o755)
    result = _run(tmp_path, path=f"{fake}:{os.environ['PATH']}")
    assert result.returncode == 1
    assert "venv 모듈이 없습니다" in result.stderr and "-venv" in result.stderr
    assert not (tmp_path / ".lms-runtime").exists()


def test_rejects_unknown_option(tmp_path):
    result = _run(tmp_path, "--bogus")
    assert result.returncode == 1 and "--bogus" in result.stderr


def test_colab_cli_version_matches_the_bridge_pin():
    from lms_kernel.colab_session import PINNED_CLI_VERSION

    assert f'local COLAB_CLI_VERSION="{PINNED_CLI_VERSION}"' in SCRIPT.read_text()
