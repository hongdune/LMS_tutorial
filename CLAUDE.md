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
3. `lms-runtime --colab <session>`이 `127.0.0.1`에 로컬 브리지를 연다. 브리지는 LMS 토큰과 origin을 검사한 뒤 Jupyter REST/WS를 Colab 프록시 URL로 중계하면서 프록시 토큰을 붙인다.
4. 브라우저 입장에서는 `local` 런타임과 똑같이 "URL + 토큰"이다.

설계 근거:
- 브리지를 두는 이유: 브라우저는 Colab 프록시에 직접 붙을 수 없다. 2026-11-03에 실측했다. 프록시는 `ACAO: *`를 붙이지만 `Origin` 헤더가 있는 요청과 preflight를 404로 거부한다. 프록시 토큰을 브라우저에 노출하지 않고 LMS 토큰과 origin을 검사해야 하므로 브리지는 어차피 필요하다.
- 약관 측면: Colab VM 안에 터널을 띄우지 않으므로 Colab FAQ의 "connecting to remote proxies"(유료 포함 전 런타임 금지) 조항과 충돌하지 않는다. 접속도 Google 공식 클라이언트 경로를 따른다.

구현 규칙:
- 우리 앱이 자체 OAuth 클라이언트로 `/tun/m/*` API를 직접 호출하지 않는다.
- Colab CLI의 Python 내부 모듈은 공개 API가 아니다. CLI 명령 출력이나 `sessions.json`처럼 덜 깨지는 경계를 통해 사용하고, CLI 버전을 고정한 뒤 업그레이드할 때 검증한다.
- 프록시 토큰에는 만료 시간이 있으므로 브리지가 갱신을 처리해야 한다.
- Colab 전용 메시지(`colab_request`, 예: Drive 인증 요청)는 1단계에서 무시한다.
- README 기준으로 CLI는 Linux/macOS만 지원한다. Windows 학생은 WSL이 필요하다.
- 공식 클라이언트를 사칭하는 헤더(`X-Colab-Client-Agent: colab-cli` 등)를 보내지 않는다. 프록시는 이 헤더 없이도 REST·WS에 응답한다.

### Colab CLI 실측 (google-colab-cli **0.7.5**, 2026-11-03, T4)
- 설치: Python **3.12 이상**이 필요하다. 커널 환경과 섞지 않도록 별도 venv(`~/.colab-cli`)에 설치하고 `~/.local/bin/colab`으로 링크한다. Ubuntu 24.04 시스템 python3.12에는 `venv` 모듈이 없을 수 있다(`python3.12-venv`가 필요하며, 이 PC에서는 anaconda 3.13을 썼다).
- 로그인: 처음 `colab` 명령을 실행할 때 CLI에 내장된 Google 클라이언트로 복사-붙여넣기 OAuth를 진행한다. 결과는 `~/.config/colab-cli/token.json`에 저장된다. README에는 기본값이 `adc`라고 적혀 있지만 코드 기본값은 `oauth2`다.
- 세션 파일 `sessions.json`에는 이름마다 `url`(`https://8080-<endpoint>.<region>.prod.r.colab.dev`), `token`, `token_expires_at`, `accelerator`가 있다. **프록시 토큰 수명은 1시간**이다.
- 갱신: CLI는 백그라운드에서 토큰을 갱신하지 않는다. 명령을 실행할 때 만료가 5분 안으로 남았거나 `status`/`sessions` 명령이면 `/tun/m/assignments`로 갱신한다. 서버에서 사라진 세션은 그때 파일에서 지운다. `colab stop`은 파일에서 바로 지운다.
- 프록시는 **WebSocket ping에 응답하지 않는다.** ping을 켜면 약 25초 만에 끊기고, 끄면 70초 넘게 유지된다.
- `colab stop` 직후에도 프록시는 약 85초 동안 REST에 200으로 응답한다. WS는 약 100초 뒤 닫힌다.
- Colab 커널은 `colab_kernel_launcher`(jupyter-server 2.20)다. 커널 Python은 3.12, torch 2.11.0+cu130이다.
- **CLI가 비밀값을 로그에 남긴다**: 명령마다 `~/.config/colab-cli/colab.log`(권한 0644)에 DEBUG 로그를 쓴다. 이 로그에 프록시 토큰, OAuth access token, **refresh token**이 들어간다. 실측에서 refresh token 2회를 확인했다. 끄는 옵션은 없다. `sessions.json`과 `token.json`도 0644다. 브리지는 CLI 호출을 토큰 만료 직전(약 1시간에 1회)과 장애 확인 때로 제한한다. 업스트림 이슈 제보 대상이다.
- **WS 끊김 (미해결, 완화됨)**: 실행 도중 프록시가 WS를 끊는 현상이 있다. 2026-11-03 오전에는 `import torch` 실행 중에 자주 재현됐지만, 같은 날 오후에는 자연 끊김이 24회 중 0회였다. 간헐적이고 원인은 모른다. 같은 코드를 `colab exec`로 실행하면 정상이고, 남은 차이는 CLI의 클라이언트 식별 헤더뿐이다. 이 헤더는 흉내 내지 않는다(googlecolab에 문의할 예정).
  - 복구: Colab의 jupyter-server는 같은 `session_id`로 다시 붙으면, 끊긴 동안 쌓인 메시지를 재전송한다(강제 끊김 실측: 0.3초 안에 재전송).
  - **한계**: 끊기는 순간 주변의 메시지 약 1개는 재전송되지 않고 사라진다(강제 끊김 15회 중 6회, 5초 뒤 재연결하면 3회 모두). 프록시 쪽 옛 연결로 이미 나간 메시지로 보인다. 사라진 것이 `execute_reply`나 마지막 `status: idle`이면 프론트는 실행이 끝나지 않은 것처럼 보인다.
  - 브리지 기본값은 B: 업스트림만 같은 `session_id`로 다시 연결한다(약 0.5초, 브라우저 WS 유지). 재시도 간격은 0, 1, 2, 4초이고, 모두 실패하거나 커널이 404면 브라우저 WS를 1011로 닫는다. 진단용 숨김 옵션은 `--no-upstream-reconnect`와 `--debug-drop-upstream-after <초>`다. `--debug-drop-reply-once`는 local 모드에서도 쓸 수 있고, 커널마다 첫 셀(silent가 아닌 첫 실행)의 `execute_reply`와 그 실행의 `status: idle`을 브라우저로 보내지 않는다(응답 누락 재현용). `info()` 같은 silent 헬퍼 호출은 세지 않는다.

### 유휴 종료와 컴퓨트 유닛 실측 (2026-11-05, T4, CLI 0.7.5)
- `colab usage`로 잔액·사용률·할당 수를 볼 수 있다. T4 할당 직후 사용률은 1.15/hr이었다
- Colab VM 안 jupyter-server의 `cull_idle_timeout`은 0(기본값)이다. `/root/.jupyter/jupyter_server_config.py`가 모두 주석이다. Colab 쪽 Jupyter는 유휴 커널을 종료하지 않는다
- **커널을 모두 지워도 과금은 계속된다**: 브리지로 커널을 `DELETE`(204)해 0개로 만든 뒤 6분 동안 사용률 1.15/hr, 할당 1개가 그대로였다. 그동안 잔액이 0.10 줄었다. 과금 단위는 커널이 아니라 VM 할당이다
- `colab stop` 직후 사용률 0.00/hr, 할당 0개가 됐다. 크레딧을 멈추는 방법은 VM 반납(`colab stop`)뿐이다
- 확인하지 못한 것: Colab이 오래 방치된 할당을 스스로 회수하는지(수 시간 관찰이 필요하다)
- 자동 반납 실측(브리지 `--idle-timeout 2`, T4 2회): 쉬기 시작한 지 약 3분 뒤(2분 + 60초 조회 간격) `colab stop`이 실행됐다. 이후 REST는 503을 돌려줬고 `colab usage`는 0.00/hr, 서버 쪽 세션은 0개였다
- **API로 만든 커널이 첫 실행 직후 사라짐 (2026-11-05, 미해결)**: 새 T4 세션에서 `POST /api/kernels` → WS로 셀 1개를 실행하면, 출력과 idle은 정상으로 왔지만 곧바로 문제가 생겼다
  - 브리지 경유 2회: 업스트림 WS가 바로 닫혔고, 커널 목록이 `[]`가 됐다
  - 브리지 없이 직접 2회: 1회는 `GET /api/kernels`가 JSON이 아닌 응답을 돌려줬다. 1회는 연결 수 0이었고, 17초 뒤 WS가 닫혀 두 번째 셀이 실패했다
  - 같은 날 `colab exec`로는 20초 간격으로 두 번 실행해도 같은 커널(같은 pid)이 유지됐다. 연결 수는 1이었다
  - 3장 "WS 끊김"과 같은 계열로 보인다(남은 차이는 CLI의 클라이언트 식별 헤더). 11-03보다 훨씬 심하다. 이 상태로는 Colab 모드에서 두 번째 셀부터 실행되지 않을 수 있다
  - 그래서 쉬는 커널의 `last_activity`가 스스로 바뀌는지는 이번에도 확인하지 못했다

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
# 선택: torch·numpy가 있어야 하는 tests/test_frontend.py 테스트 9개는 없으면 건너뛴다([dev]에 없다)
.venv/bin/pip install numpy torch --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple

# 테스트 (현재 68개)
.venv/bin/pytest

# 런타임 실행 (출력되는 URL/Token을 LMS에 등록)
.venv/bin/lms-runtime --origin http://<OCI 주소>:3000 --port 8888
```

- root로 실행하면 `build_command`가 `--ServerApp.allow_root=True`를 붙인다(로컬 런타임과 테스트 모두 그대로 동작한다)

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
  - 인터페이스 계약 사본 `docs/runtime-contract.md` (v0.1, 이후 v0.2로 갱신)
  - 개발 환경: anaconda Python 3.13.9로 `.venv` 생성, jupyter_server 2.21.1, ipykernel 7.4.0
  - **보안 수정**: Jupyter Server는 토큰 인증된 요청의 Origin 검사를 건너뛰어 `allow_origin`이 CORS 헤더에만 적용되고 있었다(토큰만 알면 다른 사이트에서 WebSocket 연결 가능). `lms_kernel/auth.py`의 `StrictOriginIdentityProvider`로 토큰이 있어도 Origin을 검사하도록 바꿨다
  - 통합 테스트 `tests/test_runtime_integration.py`: 토큰 없음 403, CORS 헤더 고정, 다른 origin REST 404 / WS 거부, 커널 생성 → WS로 실행 → `stream`·`execute_result` 수신
  - WSL2 localhost 포워딩 확인: Windows PowerShell에서 `localhost:<port>`로 REST 200, WebSocket 연결 성공

- 2026-11-03 (WSL) Colab 브리지
  - local 런타임 브라우저 확인은 LMS 세션이 마쳤다(https://lms.132-145-125-71.sslip.io, Windows Chrome/Edge)
  - Colab CLI 0.7.5 소스 검토와 실측 (결과는 3장 "Colab CLI 실측")
  - `lms-runtime --colab <session>` 구현: `lms_kernel/bridge.py`(tornado, 의존성 추가 없음), `lms_kernel/colab_session.py`
    - 계약 2장 경로만 중계하고 나머지는 404. 인증·origin·CORS·preflight는 local과 같다(다른 origin REST 404, WS 403)
    - 프록시 토큰 주입: REST는 헤더, WS는 쿼리. 로그에는 경로만 남긴다(쿼리의 토큰 제외)
    - 토큰 갱신: `sessions.json`을 15초마다 읽는다(CLI 호출 없음, `colab stop` 감지). 만료 5분 전, 또는 업스트림 401/403이면 `colab status -s`를 실행한다(30초 간격 제한)
    - 종료·장애: 세션이 없으면 REST 503(한국어 `message`), 연결이 안 되면 502, WS는 1011로 닫는다. 프록시가 ping을 받지 않으므로 WS가 열려 있는 동안 30초마다 REST로 커널을 확인한다(2회 실패 또는 404면 닫음)
    - WS는 서브프로토콜을 선택하지 않는다(기존 JSON 프로토콜). `colab_*` 메시지는 버린다
  - 테스트 `tests/test_colab_bridge.py`: 가짜 업스트림(Colab 프록시 방식으로 인증하는 Jupyter Server, `tests/fake_colab_proxy.py`)과 가짜 CLI(`tests/fake_colab_cli.py`)
  - 실측(T4): curl로 REST 403/200/404/preflight 204/커널 생성 201 확인. WS로 `print(1+1)`이 `2`를 반환함. `colab stop` 뒤 WS 1011, REST 503 확인

- 2026-11-03 (WSL) 계약 v0.2, 끊김 복구 실험, 자격증명 경고
  - `docs/runtime-contract.md`를 v0.2(LMS cbc9e32)로 갱신하고 `diff`로 원본과 같은지 확인했다
  - 브리지는 `session_id`를 포함해 LMS 토큰을 뺀 쿼리를 모두 Colab 쪽으로 넘긴다
  - 업스트림 재연결(B)을 기본값으로 정했다(3장 "WS 끊김")
  - 실험(T4)
    - 자연 끊김: A(브라우저가 재연결) 12회, B(브리지가 재연결) 12회, 9~60초짜리 실행. 끊김 0회, 출력 누락 0
    - 강제 끊김(30초 실행, 10초에 끊음): C-A 9회, C-B 6회. 중복 0, `execute_reply` 15회 모두 도착
    - 강제 끊김의 출력 누락: 15회 중 6회에서 끊는 순간의 1줄이 사라졌다
    - 재연결 시간: B 0.4~0.5초, A 즉시 재연결 시 첫 메시지까지 0.3~1.3초
  - `lms-runtime --colab`은 `~/.config/colab-cli` 권한이 느슨하면 `chmod 700` 경고를 출력한다(파일은 건드리지 않음). README 설치 단계에 같은 안내를 넣었다
  - googlecolab/google-colab-cli 이슈 초안 2개(로그의 자격증명, 서드파티 장시간 WS 문의)를 사용자에게 넘겼다. 제출은 사용자가 한다
  - 테스트: 끊었다 다시 붙는 TCP 중계로 A/B 무손실 확인, 재연결 실패 시 1011 확인, 진단 옵션, 권한 경고 (총 29개)

- 2026-11-03 (WSL) 응답 누락 재현 옵션
  - LMS 프론트에 응답 누락 대책이 들어갔다(LMS 95a346b): 셀이 응답을 기다리는 동안 5초마다 `GET /api/kernels/{id}`를 보고, 연속 두 번 idle이면 "결과 일부 누락"으로 끝낸다. 계약은 바뀌지 않았다
  - `--debug-drop-reply-once` 추가 (`lms_kernel/debug.py`)
    - local 모드: Jupyter Server의 `kernel_websocket_connection_class`를 바꾼다
    - Colab 모드: 브리지에서 메시지를 걸러 낸다
    - 버리는 기준은 메시지 순서가 아니라 부모 `msg_id`다
  - 테스트 `tests/test_debug_drop_reply.py`, bridge·launch 테스트
  - colab-bridge 브랜치를 main에 병합
- 2026-11-03 (WSL) local 런타임 오류 응답의 CORS 수정
  - 증상: 예전 런타임의 커널 id가 404로 돌아왔는데, 브라우저에는 CORS 오류로 보였다. 그래서 프론트가 새 커널을 만들지 못하고 "연결 실패"를 띄웠다
  - 원인: Jupyter Server는 CORS 헤더를 `prepare()`에서 붙인다. 오류가 나면 tornado `send_error()`가 `clear()`로 헤더를 지워서, 403·404 응답에 ACAO가 빠졌다
  - 수정: `lms_kernel/server.py`(`LMSServerApp`)가 허용 origin을 `tornado_settings["headers"]`에 넣어 모든 응답에 붙인다. `lms-runtime`은 `-m lms_kernel.server`로 띄운다. 회귀 테스트를 추가했다
  - 브라우저 확인(local + `--debug-drop-reply-once`)
    - 예전 커널 404 → 새 커널 생성
    - 첫 셀: 약 8초 뒤 "결과 일부 누락"으로 종료
    - 두 번째 셀: 정상

- 2026-11-03 (WSL) 계약 v0.3, 커널 헬퍼
  - `docs/runtime-contract.md`를 v0.3(LMS c70b100)으로 갱신하고 `diff`로 원본과 같은지 확인했다. 패키지 버전은 0.3.0이고, 의존성에 `packaging`을 추가했다
  - `lms_kernel.frontend`: `info`·`env`·`variables`·`module_tree` (계약 4.1~4.4)
    - torch/numpy는 학생이 이미 import했을 때만 `sys.modules`에서 본다. `env`만 torch가 설치돼 있으면 import한다
    - 학생 코드를 부르지 않는다
      - `repr`은 내장 스칼라 타입과 numpy 스칼라에만 쓴다
      - 컨테이너 길이는 기반 타입의 `__len__`으로 잰다
      - 타입 판별은 `isinstance` 대신 `issubclass(type(v))`
      - 긴 문자열은 `str[N]`, 아주 큰 int는 `int`로만 표시한다
  - `lms_viz`(최상위 패키지, 구현은 `lms_kernel.viz`): heatmap·line·scatter (계약 4.5)
    - 만들면 바로 표시하고 핸들을 돌려준다. 핸들은 `_ipython_display_`가 있어 마지막 줄에 와도 두 번 표시되지 않는다
    - line: `curve.append(step, train=..., val=...)`. 갱신은 0.2초 간격으로 묶어 보내고, 셀이 끝나면 `post_run_cell`에서 남은 갱신을 보낸다
    - 제한을 넘으면 heatmap은 잘라 내고, line·scatter는 첫 점과 끝 점을 넣어 고르게 다운샘플링한다
  - `--debug-drop-reply-once`를 `--help`에 보이게 하고, 프론트의 `info()` 호출이 대신 버려진다는 점을 적었다
  - 테스트 `tests/test_frontend.py`: 실제 ipykernel에 계약 4.1 형식으로 호출한다(torch 있음 / `sys.modules['torch']=None`으로 없음)
  - 계약 제안: ipykernel이 넣는 `open`이 `variables()`에 나온다. 제외 목록을 "`user_ns_hidden`에 있고 값이 그대로인 이름"(IPython `%who`와 같은 규칙)으로 바꾸자고 LMS 세션에 제안했다. v0.4에 반영됐다
- 2026-11-03 (WSL) 계약 v0.5
  - `docs/runtime-contract.md`를 v0.5(LMS b922d9e)로 갱신하고 `diff`로 원본과 같은지 확인했다
  - `variables()` 제외 규칙을 `user_ns_hidden` 방식(v0.4)으로 바꿨다. 새 커널에서 `open`이 빠지고, 학생이 `open = 1`로 덮어쓰면 다시 보이는 것을 테스트했다
  - `info()`의 `contract`를 `"0.5"`로 올렸다
  - 구현과 계약이 다른 점(고치지 않고 LMS 세션에 알림): 계약은 4,300자리를 넘는 int만 `int`로 표시하는데, 구현은 `bit_length() > 400`(약 120자리)부터 `int`로 표시한다
- 2026-11-03 (WSL) 계약 v0.6
  - `docs/runtime-contract.md`를 v0.6(LMS a28965c)으로 갱신하고 `diff`로 원본과 같은지 확인했다
  - 계약을 구현에 맞췄다: 큰 int는 `bit_length() > 400`이면 `int`, 120자를 넘는 summary는 120자에서 자르고 `…`를 붙인다
  - `info()`의 `contract`를 `"0.6"`으로 올렸다. 구현의 다른 부분은 바꾸지 않았다
- 2026-11-04 (WSL) 계약 v0.7, 유휴 커널 자동 종료
  - `docs/runtime-contract.md`를 v0.7(LMS d432eed)로 갱신하고 `diff`로 원본과 같은지 확인했다. `info()`의 `contract`는 `"0.7"`
  - `lms-runtime --idle-timeout <분>` (계약 2.1): 기본 120분, `0`이면 끈다. `MappingKernelManager`의 `cull_idle_timeout=분×60`, `cull_interval=300`, `cull_connected=True`, `cull_busy=False`. 기본값을 바꾸려면 계약 2.1을 먼저 고친다
    - local 런타임에만 적용된다. `--colab`과 함께 주면 적용되지 않는다는 안내만 출력한다(Colab 커널 종료는 Colab이 관리)
  - 테스트 `tests/test_idle_cull.py`: 실제 Jupyter Server에서 timeout만 3초로 줄여 확인한다. 유휴 커널 종료, WS가 붙어 있어도 종료, 실행 중인 커널은 유지
  - 채점 커널(계약 5장)은 프론트가 표준 IPython만으로 실행하므로 런타임 쪽 작업은 없다
- 2026-11-04 (WSL) root 계정 실행 허용
  - 증상: 일반 사용자 없이 root만 있는 WSL에서 `lms-runtime`을 실행하면 Jupyter Server가 "Running as root is not recommended"를 내고 종료한다. `--allow-root`는 `lms-runtime`이 받지 않는다
  - 결정: 막지 않는다. Jupyter의 root 거부는 공용 서버용 기본값이다. 이 런타임은 학생 본인 PC에서 본인 코드를 실행하고, WSL 일반 사용자도 `/mnt/c`의 Windows 파일에 접근할 수 있어 root로 늘어나는 위험은 배포판 손상 정도다. 외부 접근은 127.0.0.1·토큰·origin으로 막는다. 계정을 새로 만들고 다시 설치하게 하는 절차는 학생에게 부담이 크다
  - 구현: 실효 UID가 0이면 `--ServerApp.allow_root=True`를 붙이고 "root 권한으로 실행된다"는 안내 한 줄을 출력한다. Colab 모드는 Jupyter를 띄우지 않으므로 해당 없음
  - LMS 세션에 넘길 일: WSL 설치 안내에 "처음 실행할 때 사용자 이름·비밀번호를 만든다"를 분명히 적는다

- 2026-11-05 (WSL) Colab 유휴 종료 실측과 안내
  - 실측 결과는 3장 "유휴 종료와 컴퓨트 유닛 실측"에 적었다. v0.7의 커널 유휴 종료는 Colab 크레딧을 멈추지 못한다
  - `lms-runtime --colab` 시작 시 안내를 출력한다: Colab 정책에 따라 설정과 관계없이 종료될 수 있다는 점, 페이지를 닫거나 커널이 종료돼도 과금이 계속되므로 `colab stop -s <세션>`으로 끄라는 점
  - 사용자 결정: 로컬은 기본으로 자동 종료를 끈다. Colab은 기본으로 자동 종료(VM 반납)한다. 학생은 LMS 설정 화면에서 제어한다. 계약 2.1 개정이 먼저라 구현은 보류 중이다(다음 작업 0번)

- 2026-11-05 (WSL) 계약 v0.8, Colab 자동 반납
  - `docs/runtime-contract.md`를 v0.8(LMS 833577c)로 갱신하고 `diff`로 원본과 같은지 확인했다. `info()`의 `contract`는 `"0.8"`
  - `--idle-timeout` 기본값: local `0`(끔), colab 60분(`launch.py`의 `DEFAULT_*IDLE_TIMEOUT_MINUTES`)
  - Colab 자동 반납 `bridge.stop_when_idle`
    - 60초마다 업스트림 `GET /api/kernels`를 읽는다. busy 커널이 있거나, 커널 목록이 바뀌거나, `last_activity`가 바뀌면 타이머를 다시 시작한다
    - 업스트림 시각은 서로 비교만 하고 로컬 시계와 비교하지 않는다(WSL과 Colab VM의 시계 차이 무시). WS 연결은 활동으로 치지 않는다. 조회에 실패하면 타이머를 건드리지 않고 반납도 하지 않는다
    - 반납은 `ColabSession.stop()`(`colab stop -s <세션>`)으로 한다. 실패하면 경고를 출력하고 5분 뒤 다시 시도한다. 반납한 뒤 REST는 503이고, 열린 WS는 heartbeat(30초) 안에 1011로 닫힌다
  - 시작 안내: 과금 단위(VM), 자동 반납 시간, Colab 정책에 따른 종료 가능성. 반납하면 다시 시작하는 방법을 출력한다
  - 테스트: 가짜 CLI에 `stop` 추가. 유휴 반납과 503, busy 동안 대기(WS가 열려 있어도 반납), 활동 시 타이머 재시작, CLI 실패 시 재시도
  - 실측: 자동 반납과 크레딧 중지는 확인했다. 쉬는 커널의 `last_activity`는 커널 유실 때문에 확인하지 못했다(3장, 다음 작업 0번과 0-1번)

- 2026-11-05 (WSL) torch 없는 환경의 테스트
  - `tests/test_frontend.py`: torch·numpy가 필요한 테스트는 `_require(...)`(find_spec)로 건너뛴다. torch 없음 테스트의 `env` 대상은 numpy에서 `packaging`(필수 의존성)으로 바꿨다
  - torch·numpy 없는 venv: 54 passed, 9 skipped. 전체 환경: 63 passed

- 2026-11-05 (WSL) CI, Colab CLI 버전 확인
  - GitHub Actions `.github/workflows/tests.yml`: Python 3.10/3.12 × (CPU torch 있음/없음), `pytest -q -rs`. Colab에는 접속하지 않는다
  - `lms-runtime --colab`이 시작할 때 CLI 버전을 확인한다. CLI를 실행하지 않고, 실행 파일(링크를 따라감)과 shebang 인터프리터의 venv에서 `google_colab_cli-*.dist-info/METADATA`를 읽는다. 0.7.5가 아니거나 확인하지 못하면 경고만 출력한다(`colab_session.PINNED_CLI_VERSION`)
  - `sessions.json` 형식이 다르면(JSON 아님, 목록이 dict가 아님, 세션에 url·token 없음) `SessionFormatError`로 알린다. 시작할 때는 안내를 출력하고 종료 1, 실행 중에는 REST 503과 안내 `message`. 세션이 아예 없으면 지금처럼 "종료됨"으로 처리한다

- 2026-11-05 (WSL) `--debug-drop-reply-once`가 첫 셀을 버리도록 수정
  - 지금까지는 프론트가 접속 직후 부르는 `info()`(silent)의 응답이 버려져서 첫 셀 누락이 재현되지 않았다. 이제 `execute_input`을 처음 보낸 실행(=silent가 아닌 첫 실행)을 대상으로 한다. 커널은 silent 요청에는 `execute_input`을 보내지 않는다
  - 브라우저 확인 절차(LMS 95a346b, "첫 셀: 약 8초 뒤 결과 일부 누락")는 원래 의도대로 다시 맞는다. LMS 세션에 알릴 것

### 다음 작업 (WSL, 우선순위 순)
0. **Colab 커널 유실 조사 (최우선)**: 3장 "API로 만든 커널이 첫 실행 직후 사라짐". 같은 순서를 다른 날·다른 리전에서 재현하는지, CPU 런타임에서도 그런지 확인한다. 연결 수가 0으로 보이는 이유도 본다. googlecolab 문의에 이 결과를 덧붙인다
0-1. **쉬는 Colab 커널의 `last_activity`**: 커널이 유지되는 상태에서 몇 분 동안 값이 그대로인지 확인한다. 바뀌면 자동 반납이 일어나지 않는다. 자동 반납 자체(반납과 0.00/hr)는 실측으로 확인했다
1. **Colab 안내 범위**: 결정됨(2026-11-05, 사용자). 커널 유실 문제가 있어도 Colab 안내는 지금처럼 유지한다. 0번 결과는 LMS 세션에 알리고, 복구 화면 문구로 대응한다
2. **Colab 브라우저 확인**: https://lms.132-145-125-71.sslip.io/courses/transformer/1 과 `/runtime-test`에서 `lms-runtime --colab` URL/Token으로 확인한다
3. **Colab 쪽 `lms_kernel` 설치 방식**: 계약 4장 "헬퍼 버전 확인"과 함께 LMS 세션과 정한다. 지금은 Colab 커널에 `lms_kernel`이 없어서 프론트의 `info()`·`variables()` 호출이 `ModuleNotFoundError`로 끝난다

개선 후보 (2026-11-03 정리, 우선순위 순. 계약을 건드리는 것은 LMS 세션과 먼저 합의한다)
8. **googlecolab 이슈 후속**: 사용자가 제출한 두 이슈(로그의 자격증명, 서드파티 장시간 WS)의 URL을 3장에 적고, 답변에 따라 colab.log 경고와 WS 끊김 대책을 갱신한다
