# LMS_tutorial

[LMS](https://github.com/hongdune/LMS)의 코드 블록을 **학생 본인 PC에서** 실행하기 위한 런타임 패키지 `lms-kernel`.
LMS 사이트는 브라우저에서 이 런타임(Jupyter Server)에 "URL + 토큰"으로 직접 접속해 코드를 실행한다.

## 설치 (Linux / macOS / Windows는 WSL)

터미널에 한 줄을 붙여넣는다. 저장소를 내려받을 필요가 없고, 홈 폴더에만 설치한다(sudo 불필요).

```bash
# local 런타임
curl -fsSL https://raw.githubusercontent.com/hongdune/LMS_tutorial/main/install.sh | bash
# Colab 런타임도 쓸 때 (Colab CLI까지 설치)
curl -fsSL https://raw.githubusercontent.com/hongdune/LMS_tutorial/main/install.sh | bash -s -- --colab
```

- 설치 위치: `~/.lms-runtime/venv`(lms-kernel), `~/.colab-cli`(Colab CLI 0.7.5, `--colab`일 때), 명령 링크는 `~/.local/bin`. `~/.bashrc`(zsh면 `~/.zshrc`)에 PATH 한 줄을 추가한다. root 계정의 `~/.profile`은 `~/.local/bin`을 PATH에 넣지 않기 때문이다
- `--colab`이면 `~/.config/colab-cli` 권한을 `700`으로 만든다(아래 경고 참고)
- `--origin <LMS 주소>`를 주면 마지막에 그 주소를 넣은 실행 명령을 보여 준다
- 다시 실행하면 lms-kernel을 최신으로 올린다. 나머지는 그대로 둔다
- Python이 없거나 venv 모듈이 없으면 설치할 패키지를 안내하고 멈춘다. Colab CLI에는 Python 3.12 이상이 필요하다

개발할 때는 저장소에서 직접 설치한다:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## 실행

```bash
lms-runtime --origin <LMS 사이트 주소> --port 8888
```

출력되는 URL과 Token을 LMS 사이트의 런타임 설정에 입력한다. `--origin`에는 브라우저 주소창에 보이는 LMS 주소(예: `http://123.45.67.89:3000`)를 그대로 넣는다.

기본으로 커널은 자동 종료되지 않는다. `--idle-timeout <분>`을 주면 그 시간 동안 쓰지 않은 커널을 종료한다. 브라우저 탭을 열어 둔 채여도 종료되지만, 학습처럼 실행 중인 커널은 종료하지 않는다. 종료된 뒤 LMS 페이지에서 다시 실행하면 새 커널이 만들어진다. LMS 런타임 설정 화면에서 시간을 고르면 이 옵션을 넣은 명령을 만들어 준다.

종료하려면 **Ctrl+C를 두 번**(5초 안에) 누른다. 한 번만 누르면 안내만 나오고 계속 실행된다. 터미널에서 Token을 드래그해 복사하다가 Ctrl+C가 종료 신호로 가는 일이 잦아서다. 복사는 드래그한 뒤 Ctrl+Shift+C나 마우스 오른쪽 버튼으로 하는 것이 안전하다. `--colab` 모드도 같다. 지정한 포트를 이미 쓰고 있으면 다른 포트로 옮기지 않고 오류로 종료한다(출력한 URL과 실제 포트가 달라지지 않도록).

WSL에 root 계정만 있어도 그대로 실행된다. 이때 LMS에서 실행하는 코드도 root 권한으로 실행된다는 안내가 한 줄 출력된다.

## Colab 런타임으로 실행

Colab GPU에서 코드를 실행하려면 Google의 [Colab CLI](https://github.com/googlecolab/google-colab-cli)로 런타임을 할당받고, `lms-runtime --colab`으로 로컬 브리지를 연다. 브라우저 입장에서는 위와 똑같은 "URL + Token"이다.

```bash
# 1. 설치 (최초 1회, 위 "설치"의 --colab 명령)
curl -fsSL https://raw.githubusercontent.com/hongdune/LMS_tutorial/main/install.sh | bash -s -- --colab

# 2. Colab 런타임 할당 (처음 한 번은 Google 로그인 URL이 뜬다. 승인 후 코드를 붙여넣는다)
colab new -s lms --gpu T4

# 3. 브리지 실행. 출력되는 URL/Token을 LMS에 등록한다
lms-runtime --colab lms --origin <LMS 사이트 주소> --port 8888

# 4. 다 쓰면 Colab 런타임 반납 (컴퓨트 유닛 절약)
colab stop -s lms
```

<details>
<summary>설치 스크립트 없이 Colab CLI를 직접 설치하기</summary>

```bash
python3 -m venv ~/.colab-cli                         # Python 3.12 이상
~/.colab-cli/bin/pip install google-colab-cli==0.7.5  # 버전 고정
mkdir -p ~/.local/bin && ln -sf ~/.colab-cli/bin/colab ~/.local/bin/colab
grep -qs 'HOME/.local/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
export PATH="$HOME/.local/bin:$PATH"
colab version                                        # Version: 0.7.5
mkdir -p ~/.config/colab-cli && chmod 700 ~/.config/colab-cli
```

</details>

Colab은 커널이 아니라 런타임(VM) 단위로 컴퓨트 유닛을 쓴다. 페이지를 닫거나 커널을 종료해도 런타임을 반납하기 전까지는 계속 쓰인다. 그래서 브리지는 모든 커널이 **60분** 동안 쉬면 `colab stop`으로 런타임을 자동 반납한다. 실행 중인 커널이 있으면 반납하지 않는다. 시간은 `--idle-timeout <분>`으로 바꾸고, `0`이면 자동 반납을 끈다. 이 설정과 관계없이 Colab은 자체 정책(컴퓨트 유닛 소진, 최대 사용 시간 등)에 따라 런타임을 예고 없이 종료할 수 있다.

LMS의 런타임 상태 화면에서 Colab 컴퓨트 유닛 잔액, 시간당 사용량, 켜진 세션을 "조회"할 수 있다. 브리지가 이때만 `colab usage`와 `colab sessions`를 실행한다(30초 캐시).

변수 인스펙터·환경 점검·리소스 확인에 쓰는 커널 헬퍼(`lms_kernel`, `lms_viz`)는 브리지가 시작할 때 Colab 런타임에 자동으로 복사한다. Colab에서 따로 설치할 것은 없다.

> **경고**: Colab CLI(0.7.5)는 `~/.config/colab-cli/`의 `colab.log`, `token.json`, `sessions.json`에 Google 로그인 자격증명(refresh token 포함)과 런타임 토큰을 평문으로 남기고, 파일을 다른 사용자도 읽을 수 있는 권한(0644)으로 만든다. 그래서 폴더 권한을 `700`으로 막아 둔다. 이 파일들을 남에게 보내거나 저장소에 올리지 않는다. `lms-runtime --colab`은 폴더 권한이 느슨하면 시작할 때 경고를 출력한다.

설계와 개발 규칙은 [CLAUDE.md](CLAUDE.md), LMS와의 인터페이스는 [docs/runtime-contract.md](docs/runtime-contract.md)를 본다.
