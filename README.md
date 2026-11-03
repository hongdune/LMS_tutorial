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

설계와 개발 규칙은 [CLAUDE.md](CLAUDE.md), LMS와의 인터페이스는 [docs/runtime-contract.md](docs/runtime-contract.md)를 본다.
