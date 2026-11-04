# LMS_tutorial

[LMS](https://github.com/hongdune/LMS)의 코드 블록을 **학생 본인 PC에서** 실행하기 위한 런타임 패키지 `lms-kernel`.
LMS 사이트는 브라우저에서 이 런타임(Jupyter Server)에 "URL + 토큰"으로 직접 접속해 코드를 실행한다.

## 설치 (Linux / macOS / Windows는 WSL)

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## 실행

```bash
.venv/bin/lms-runtime --origin <LMS 사이트 주소> --port 8888
```

출력되는 URL과 Token을 LMS 사이트의 런타임 설정에 입력한다. `--origin`에는 브라우저 주소창에 보이는 LMS 주소(예: `http://123.45.67.89:3000`)를 그대로 넣는다.

기본으로 커널은 자동 종료되지 않는다. `--idle-timeout <분>`을 주면 그 시간 동안 쓰지 않은 커널을 종료한다. 브라우저 탭을 열어 둔 채여도 종료되지만, 학습처럼 실행 중인 커널은 종료하지 않는다. 종료된 뒤 LMS 페이지에서 다시 실행하면 새 커널이 만들어진다. LMS 런타임 설정 화면에서 시간을 고르면 이 옵션을 넣은 명령을 만들어 준다.

WSL에 root 계정만 있어도 그대로 실행된다. 이때 LMS에서 실행하는 코드도 root 권한으로 실행된다는 안내가 한 줄 출력된다.

## Colab 런타임으로 실행

Colab GPU에서 코드를 실행하려면 Google의 [Colab CLI](https://github.com/googlecolab/google-colab-cli)로 런타임을 할당받고, `lms-runtime --colab`으로 로컬 브리지를 연다. 브라우저 입장에서는 위와 똑같은 "URL + Token"이다.

```bash
# 1. Colab CLI 설치 (최초 1회, Python 3.12 이상 필요. 버전 고정)
python3 -m venv ~/.colab-cli
~/.colab-cli/bin/pip install google-colab-cli==0.7.5
mkdir -p ~/.local/bin && ln -sf ~/.colab-cli/bin/colab ~/.local/bin/colab
source ~/.profile   # ~/.local/bin을 처음 만들었다면 PATH에 다시 읽어 들인다
colab version       # Version: 0.7.5 가 나오면 성공
chmod 700 ~/.config/colab-cli   # 자격증명 보호 (아래 경고 참고)

# 2. Colab 런타임 할당 (처음 한 번은 Google 로그인 URL이 뜬다. 승인 후 코드를 붙여넣는다)
colab new -s lms --gpu T4

# 3. 브리지 실행. 출력되는 URL/Token을 LMS에 등록한다
.venv/bin/lms-runtime --colab lms --origin <LMS 사이트 주소> --port 8888

# 4. 다 쓰면 Colab 런타임 반납 (컴퓨트 유닛 절약)
colab stop -s lms
```

Colab은 커널이 아니라 런타임(VM) 단위로 컴퓨트 유닛을 쓴다. 페이지를 닫거나 커널을 종료해도 런타임을 반납하기 전까지는 계속 쓰인다. 그래서 브리지는 모든 커널이 **60분** 동안 쉬면 `colab stop`으로 런타임을 자동 반납한다. 실행 중인 커널이 있으면 반납하지 않는다. 시간은 `--idle-timeout <분>`으로 바꾸고, `0`이면 자동 반납을 끈다. 이 설정과 관계없이 Colab은 자체 정책(컴퓨트 유닛 소진, 최대 사용 시간 등)에 따라 런타임을 예고 없이 종료할 수 있다.

변수 인스펙터·환경 점검에 쓰는 커널 헬퍼(`lms_kernel`, `lms_viz`)는 브리지가 시작할 때 Colab 런타임에 자동으로 복사한다. Colab에서 따로 설치할 것은 없다.

> **경고**: Colab CLI(0.7.5)는 `~/.config/colab-cli/`의 `colab.log`, `token.json`, `sessions.json`에 Google 로그인 자격증명(refresh token 포함)과 런타임 토큰을 평문으로 남기고, 파일을 다른 사용자도 읽을 수 있는 권한(0644)으로 만든다. 그래서 폴더 권한을 `700`으로 막아 둔다. 이 파일들을 남에게 보내거나 저장소에 올리지 않는다. `lms-runtime --colab`은 폴더 권한이 느슨하면 시작할 때 경고를 출력한다.

설계와 개발 규칙은 [CLAUDE.md](CLAUDE.md), LMS와의 인터페이스는 [docs/runtime-contract.md](docs/runtime-contract.md)를 본다.
