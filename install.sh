#!/usr/bin/env bash
# LMS student runtime installer (Linux, macOS, Windows via WSL).
#
#   curl -fsSL https://raw.githubusercontent.com/hongdune/LMS_tutorial/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/hongdune/LMS_tutorial/main/install.sh | bash -s -- --colab --origin https://lms.example
#
# Installs into the home directory only (no sudo, no git clone):
#   ~/.lms-runtime/venv   lms-kernel (lms-runtime command)
#   ~/.colab-cli          google-colab-cli, pinned (with --colab; needs Python 3.12+)
#   ~/.local/bin          links to lms-runtime and colab, put on PATH via ~/.bashrc / ~/.zshrc
# Safe to run again: it upgrades lms-kernel and leaves everything else as is.
#
# Options:
#   --colab           also install the Colab CLI
#   --origin URL      LMS site address, used only to print the exact next command
#   --ref REF         branch or tag of LMS_tutorial to install (default: main)
#   --source SPEC     pip requirement to install instead of the GitHub archive (tests)
# Environment: LMS_RUNTIME_HOME (default ~/.lms-runtime), COLAB_CLI_HOME (default ~/.colab-cli)

# Everything runs inside main(), so a partially downloaded script does nothing.
main() {
    set -euo pipefail

    local REPO="hongdune/LMS_tutorial"
    local COLAB_CLI_VERSION="0.7.5"   # keep in sync with colab_session.PINNED_CLI_VERSION
    local want_colab=0 origin="" ref="main" source=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --colab) want_colab=1 ;;
            --origin) origin="${2:?--origin 뒤에 LMS 주소를 적으세요}"; shift ;;
            --ref) ref="${2:?--ref 뒤에 브랜치나 태그를 적으세요}"; shift ;;
            --source) source="${2:?--source 뒤에 pip 설치 대상을 적으세요}"; shift ;;
            -h|--help) sed -n '2,20p' "$0" 2>/dev/null || true; return 0 ;;
            *) fail "알 수 없는 옵션입니다: $1" ;;
        esac
        shift
    done

    local runtime_home="${LMS_RUNTIME_HOME:-$HOME/.lms-runtime}"
    local colab_home="${COLAB_CLI_HOME:-$HOME/.colab-cli}"
    local bin_dir="$HOME/.local/bin"
    [ -n "$source" ] || source="https://github.com/$REPO/archive/$ref.tar.gz"

    case "$(uname -s)" in
        Linux|Darwin) ;;
        *) fail "Linux, macOS, WSL에서만 설치할 수 있습니다. Windows에서는 WSL(Ubuntu) 터미널에서 실행하세요." ;;
    esac

    step "1/4" "Python 확인"
    local py
    find_python 10 || fail_python "3.10"
    py="$FOUND_PY"
    ok "$("$py" --version 2>&1) ($py)"

    step "2/4" "LMS 런타임 설치 ($runtime_home)"
    [ -x "$runtime_home/venv/bin/python" ] || run "$py" -m venv "$runtime_home/venv" \
        || fail "가상환경을 만들지 못했습니다. 위 오류를 확인하세요."
    run "$runtime_home/venv/bin/python" -m pip install --quiet --upgrade pip \
        || fail "pip을 업그레이드하지 못했습니다. 인터넷 연결을 확인하세요."
    run "$runtime_home/venv/bin/python" -m pip install --quiet --upgrade "$source" \
        || fail "lms-kernel을 설치하지 못했습니다. 인터넷 연결을 확인하고 다시 실행하세요."
    mkdir -p "$bin_dir"
    ln -sf "$runtime_home/venv/bin/lms-runtime" "$bin_dir/lms-runtime"
    ok "lms-runtime $("$runtime_home/venv/bin/python" -c 'import lms_kernel; print(lms_kernel.__version__)')"

    if [ "$want_colab" = 1 ]; then
        step "3/4" "Colab CLI $COLAB_CLI_VERSION 설치 ($colab_home)"
        local cpy
        if [ -x "$colab_home/bin/python" ] && "$colab_home/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
            cpy="$colab_home/bin/python"
        else
            find_python 12 || fail_python "3.12" "Colab CLI"
            cpy="$FOUND_PY"
            run "$cpy" -m venv "$colab_home" || fail "Colab CLI용 가상환경을 만들지 못했습니다."
            cpy="$colab_home/bin/python"
        fi
        run "$cpy" -m pip install --quiet --upgrade pip \
            || fail "pip을 업그레이드하지 못했습니다. 인터넷 연결을 확인하세요."
        run "$cpy" -m pip install --quiet "google-colab-cli==$COLAB_CLI_VERSION" \
            || fail "Colab CLI를 설치하지 못했습니다. 인터넷 연결을 확인하고 다시 실행하세요."
        ln -sf "$colab_home/bin/colab" "$bin_dir/colab"
        # The CLI writes credentials to this folder with mode 0644 (see README).
        mkdir -p "$HOME/.config/colab-cli"
        chmod 700 "$HOME/.config/colab-cli"
        ok "colab $COLAB_CLI_VERSION, ~/.config/colab-cli 권한 700"
    else
        step "3/4" "Colab CLI: 건너뜀 (Colab을 쓰려면 --colab을 붙여 다시 실행)"
    fi

    step "4/4" "PATH 설정 ($bin_dir)"
    # Root's ~/.profile on Ubuntu does not add ~/.local/bin, so write it to the rc file.
    local line='export PATH="$HOME/.local/bin:$PATH"' rc added=""
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [ "$rc" = "$HOME/.bashrc" ] || [ -f "$rc" ] || [ "${SHELL##*/}" = zsh ]; then
            if ! grep -qs 'HOME/.local/bin' "$rc"; then
                printf '\n# Added by the LMS runtime installer\n%s\n' "$line" >> "$rc"
                added="$added ~/${rc##*/}"
            fi
        fi
    done
    if [ -n "$added" ]; then ok "추가함:$added"; else ok "이미 설정돼 있음"; fi

    local site="${origin:-<LMS 사이트 주소>}"
    printf '\n\033[1m설치를 마쳤습니다.\033[0m\n'
    case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *) printf '지금 열린 터미널에는 아직 적용되지 않았습니다. 터미널을 새로 열거나 다음을 실행하세요:\n  export PATH="$HOME/.local/bin:$PATH"\n' ;;
    esac
    printf '\n다음 단계:\n'
    if [ "$want_colab" = 1 ]; then
        printf '  1. Colab 런타임 받기 (처음 한 번은 Google 로그인 주소가 나옵니다):\n     colab new -s lms --gpu T4\n'
        printf '  2. 런타임 연결:\n     lms-runtime --colab lms --origin %s\n' "$site"
        printf '  3. 다 쓰면 반납 (컴퓨트 유닛 절약):\n     colab stop -s lms\n'
    else
        printf '  lms-runtime --origin %s\n' "$site"
    fi
    printf '  출력되는 URL과 Token을 LMS 사이트의 런타임 설정에 입력하세요.\n'
}

step() { printf '\n\033[1m[%s] %s\033[0m\n' "$1" "$2"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '\n\033[31m설치 실패:\033[0m %s\n' "$1" >&2; exit 1; }
run() { "$@" 2>&1 | sed 's/^/    /'; return "${PIPESTATUS[0]}"; }

# Sets FOUND_PY to the first python3 >= 3.<minor> that can create a venv. Sets NO_VENV to
# a new-enough python3 whose venv module is missing (Ubuntu ships it as pythonX.Y-venv).
find_python() {
    local minor="$1" c
    FOUND_PY="" NO_VENV=""
    for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        command -v "$c" >/dev/null 2>&1 || continue
        "$c" -c "import sys; sys.exit(sys.version_info < (3, $minor))" 2>/dev/null || continue
        if "$c" -c "import venv, ensurepip" 2>/dev/null; then
            FOUND_PY="$(command -v "$c")"
            return 0
        fi
        [ -n "$NO_VENV" ] || NO_VENV="$c"
    done
    return 1
}

fail_python() {
    local need="$1" what="${2:-LMS 런타임}" sudo=""
    [ "$(id -u)" = 0 ] || sudo="sudo "
    if [ -n "${NO_VENV:-}" ]; then
        local v
        v="$("$NO_VENV" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
        fail "$NO_VENV 에 venv 모듈이 없습니다. 다음을 실행한 뒤 다시 설치하세요:
  ${sudo}apt update && ${sudo}apt install -y python${v}-venv"
    fi
    if [ "$(uname -s)" = Darwin ]; then
        fail "$what에는 Python $need 이상이 필요합니다. https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요."
    fi
    fail "$what에는 Python $need 이상이 필요합니다. 다음을 실행한 뒤 다시 설치하세요:
  ${sudo}apt update && ${sudo}apt install -y python3.12 python3.12-venv
  (Ubuntu 22.04처럼 python3.12 패키지가 없다는 오류가 나면 먼저:
   ${sudo}apt install -y software-properties-common && ${sudo}add-apt-repository -y ppa:deadsnakes/ppa)
  WSL을 새로 설치한다면 Ubuntu 24.04 이상을 권합니다."
}

main "$@"
