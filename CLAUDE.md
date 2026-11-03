# CLAUDE.md

이 문서는 이 저장소에서 작업하는 Claude Code(및 개발자)를 위한 프로젝트 가이드다.
**결정 대기** 표시가 붙은 항목은 확정 전까지 임의로 구현하지 않는다.

## 1. 이 저장소의 위치

딥러닝을 **직접 코드를 고치고 실행하며 체득하는** 인터랙티브 LMS는 저장소 두 개로 나뉜다. 각 저장소는 별도의 Claude Code 세션에서 작업한다.

| 저장소 | 담당 | 작업 호스트 |
|---|---|---|
| [hongdune/LMS](https://github.com/hongdune/LMS) | 중앙 서버(FastAPI), 프론트엔드(Next.js), 코스 콘텐츠. **제품 요구사항 전체(2장)는 그 저장소의 CLAUDE.md가 원본이다** | OCI 인스턴스 |
| **hongdune/LMS_tutorial** (이 저장소) | 학생 PC에 설치하는 런타임 패키지 `lms_kernel` | 학생 PC (현재는 로컬 WSL) |

이 저장소가 맡는 것:
- `lms-runtime`: LMS origin과 토큰으로 제한한 Jupyter Server 실행
- `lms-runtime --colab <session>`: Colab CLI로 할당받은 Colab 런타임을 중계하는 로컬 브리지 (3장)
- 커널 안 헬퍼: 변수 인스펙터(LMS 2.9), 환경 점검(2.10), 시각화 위젯 출력 `lms_viz`(2.11)

맡지 않는 것: 화면, 계정, 콘텐츠, 진도, 퀴즈 채점. 프론트·서버 쪽 변경이 필요하면 계약 문서에 반영한 뒤 LMS 세션에 작업을 넘긴다.

### 인터페이스 계약
- 두 저장소의 경계는 [docs/runtime-contract.md](docs/runtime-contract.md)에 정의한다. **원본은 LMS 저장소에 있고** 이 파일은 사본이다.
- 계약을 바꿔야 하면 버전을 올리고 두 저장소의 파일을 같은 내용으로 갱신한다. 한쪽만 바뀐 상태로 커밋하지 않는다.
- 핵심 규칙 요약 (상세는 계약 문서):
  - 표준 Jupyter Server REST(`/api/kernels`)와 WebSocket(`/api/kernels/{id}/channels`)만 노출한다. 커스텀 HTTP API를 추가하지 않는다.
  - `127.0.0.1`에만 바인딩하고, `--origin`으로 받은 LMS origin만 허용한다.
  - 토큰은 실행 시마다 새로 만든다. 터미널 출력 외에는 로그·파일에 남기지 않는다.
  - 커널에 남기는 이름은 모두 `_lms_` 접두사를 쓴다.

## 2. 구성 (1단계)

```
OCI 인스턴스 (LMS 저장소)                   학생 PC: Windows + WSL (이 저장소)
 ├─ frontend   Next.js   :3000              └─ lms-runtime  Jupyter Server 127.0.0.1:8888
 └─ server     FastAPI   :8000                   ← local 커널 또는 Colab CLI 브리지
        ▲                                               ▲
        │ HTTP(S)                                       │ HTTP/WS (코드 실행)
        └──────────────── Windows 브라우저 ──────────────┘
```

- Windows 브라우저가 OCI의 LMS 사이트를 열고, 코드 실행은 `http://localhost:8888`로 보낸다. WSL2의 localhost 포워딩으로 WSL 안의 `127.0.0.1:8888`에 도달해야 한다.
- 커널은 Jupyter 표준(`jupyter_server` + `ipykernel`)을 쓴다. `exec()`로 직접 구현하지 않는다(rich output, interrupt, 스트리밍을 그대로 얻기 위해).
- 학생 코드는 학생 본인의 런타임에서만 실행된다. 이 패키지는 학생 자신의 PC에서 학생 자신의 코드를 실행하는 것이 전제이며, 여러 사람이 한 런타임에 붙는 구성은 지원하지 않는다.

## 3. Colab 런타임 — Colab CLI 브리지 (확정)

조사 기준: 2026-11. 외부 앱용 공개 API는 없고, Google(`googlecolab` 조직)이 공식 클라이언트 두 가지를 공개했다: VS Code 확장(colab-vscode)과 **Colab CLI**([google-colab-cli](https://github.com/googlecolab/google-colab-cli), Apache-2.0). 이 프로젝트는 Colab CLI를 사용한다.

동작 흐름:
1. 학생이 런타임 호스트(자기 PC/WSL)에서 `colab new --gpu ...`로 Colab 런타임을 할당받는다. Google OAuth 로그인은 CLI가 직접 처리한다.
2. CLI는 할당 응답의 `runtimeProxyInfo { url, token, tokenExpiresInSeconds }`로 Colab 런타임의 Jupyter API에 접속한다. 세션 정보는 `~/.config/colab-cli/sessions.json`에 저장된다.
   - REST 인증: `X-Colab-Runtime-Proxy-Token` 헤더
   - WebSocket 인증: `colab-runtime-proxy-token` 쿼리 파라미터
   - 참고 소스: `src/colab_cli/client.py`, `runtime.py`, `state.py`
3. `lms-runtime --colab <session>`(구현 예정)이 `127.0.0.1`에 로컬 브리지를 연다. 브리지는 LMS 토큰과 origin을 검사한 뒤 Jupyter REST/WS를 Colab 프록시 URL로 중계하면서 프록시 토큰을 붙인다.
4. 브라우저 입장에서는 `local` 런타임과 똑같이 "URL + 토큰"이다.

설계 근거:
- 브리지를 두는 이유: Colab 프록시가 LMS origin에 CORS를 허용하지 않을 것으로 보여, 브라우저가 직접 REST를 호출할 수 없다. **실측이 필요하다.**
- 약관 측면: Colab VM 안에 터널을 띄우지 않으므로 Colab FAQ의 "connecting to remote proxies"(유료 포함 전 런타임 금지) 조항과 충돌하지 않는다. 접속도 Google 공식 클라이언트 경로를 따른다.

구현 규칙:
- 우리 앱이 자체 OAuth 클라이언트로 `/tun/m/*` API를 직접 호출하지 않는다.
- Colab CLI의 Python 내부 모듈은 공개 API가 아니다. CLI 명령 출력이나 `sessions.json`처럼 덜 깨지는 경계를 통해 사용하고, CLI 버전을 고정한 뒤 업그레이드할 때 검증한다.
- 프록시 토큰에는 만료 시간이 있으므로 브리지가 갱신을 처리해야 한다.
- Colab 전용 메시지(`colab_request`, 예: Drive 인증 요청)는 1단계에서 무시한다.
- README 기준으로 CLI는 Linux/macOS만 지원한다. Windows 학생은 WSL이 필요하다.

위험:
- "notebook UI 우회"·"원격 제어" 제한은 **컴퓨트 유닛 잔액이 0보다 클 때만** 풀린다. Pro+라도 월 할당량을 다 쓰면 제한 대상이 되고, 런타임이 예고 없이 종료될 수 있다.
- 세션 수명은 학생의 컴퓨트 유닛에 달려 있다.
- 비권장 대안: Colab 노트북 안에서 Jupyter Server와 터널(cloudflared 등)을 띄우는 방식. 위 프록시 금지 조항에 해당할 소지가 있어 쓰지 않는다.


## 4. 개발 규칙

- 언어: 학생에게 보이는 출력(터미널 안내 문구 등)은 한국어, 코드·식별자·커밋 메시지는 영어.
- 커널 연동은 실제 ipykernel을 띄우는 통합 테스트로 검증한다. 브리지는 가짜 업스트림(로컬 Jupyter Server)을 두고 REST/WS 중계를 테스트한다.
- 비밀값(LMS 토큰, Colab 프록시 토큰)은 로그에 남기지 않는다. 테스트 출력에도 찍지 않는다.
- `lms_kernel`은 학생 커널 환경에 설치되므로 의존성을 최소로 둔다. torch 같은 무거운 패키지는 필수 의존성에 넣지 않고, 있으면 쓰는 방식으로 다룬다.

### 명령어
주 개발 환경은 **WSL(Ubuntu)** 이다. 명령은 저장소 루트 기준이다.

```bash
# Python 환경 (최초 1회, Python 3.10 이상)
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 테스트
.venv/bin/pytest

# 런타임 실행 (출력되는 URL/Token을 LMS에 등록)
.venv/bin/lms-runtime --origin http://<OCI 주소>:3000 --port 8888
```

- 저장소는 WSL 파일시스템(예: `~/LMS_tutorial`)에 둔다. `/mnt/c/...`에서 작업하지 않는다.
- `.venv`는 커밋하지 않는다. 줄바꿈은 `.gitattributes`로 LF로 고정한다.

## 5. 진행 상황과 다음 작업

이 섹션은 작업 세션이 바뀌어도 이어서 진행할 수 있도록 유지한다. 작업을 끝낼 때마다 갱신한다.

### 완료
- 2026-11-02 (Windows, LMS 저장소의 `runtime/` 시절)
  - `lms-runtime` 실행 명령 (`lms_kernel/launch.py`)
  - 수동 검증 결과 (jupyter_server 2.21):
    - 토큰 없이 요청하면 403
    - 허용 origin에만 `Access-Control-Allow-Origin` 헤더가 붙음
    - `POST /api/kernels`로 커널 생성 201
    - `Authorization: token ...` 헤더로 인증하면 XSRF 검사가 면제되므로 `disable_check_xsrf`는 쓰지 않는다
- 2026-11-03 (WSL)
  - LMS 저장소의 `runtime/`을 git 기록과 함께 이 저장소로 분리. 패키지를 저장소 루트로 올림
  - 인터페이스 계약 사본 `docs/runtime-contract.md` (v0.1)
  - 개발 환경: anaconda Python 3.13.9로 `.venv` 생성, jupyter_server 2.21.1, ipykernel 7.4.0
  - **보안 수정**: Jupyter Server는 토큰 인증된 요청의 Origin 검사를 건너뛰어 `allow_origin`이 CORS 헤더에만 적용되고 있었다(토큰만 알면 다른 사이트에서 WebSocket 연결 가능). `lms_kernel/auth.py`의 `StrictOriginIdentityProvider`로 토큰이 있어도 Origin을 검사하도록 바꿨다
  - 통합 테스트 `tests/test_runtime_integration.py`: 토큰 없음 403, CORS 헤더 고정, 다른 origin REST 404 / WS 거부, 커널 생성 → WS로 실행 → `stream`·`execute_result` 수신
  - WSL2 localhost 포워딩 확인: Windows PowerShell에서 `localhost:<port>`로 REST 200, WebSocket 연결 성공

### 다음 작업 (WSL, 우선순위 순)
1. **브라우저 최종 확인** (LMS 세션의 8장 2번과 함께 진행): OCI의 LMS 주소가 정해지면 `--origin`을 그 주소로 띄우고, Windows Chrome/Edge에서 LMS 페이지를 통해 REST·WebSocket이 되는지, Local Network Access 프롬프트가 어떻게 뜨는지 확인한다
2. **Colab CLI 실측** (Pro+ 계정)
   - `pip install google-colab-cli`로 설치한 뒤 `colab new --gpu T4`로 런타임 할당, `colab exec`로 동작 확인
   - `sessions.json`에서 URL과 프록시 토큰을 얻을 수 있는지 확인. 토큰 만료 시간과 갱신 방식도 확인한다
   - 프록시 URL에 대해 CORS 응답 확인. 브라우저가 직접 붙을 수 있는지에 따라 브리지가 필요한지 확정한다
   - 사용한 CLI 버전을 3장에 기록
3. **Colab 브리지 구현**: `lms-runtime --colab <session>`. REST/WS를 중계하고 토큰을 갱신한다. 통합 테스트도 함께 작성
4. **커널 헬퍼**: 환경 점검 → 변수 인스펙터 → `lms_viz` 순. 각각 결과 형식을 먼저 계약 문서에 정한다(LMS 세션과 합의)
