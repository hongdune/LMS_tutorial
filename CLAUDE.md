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
  - 브리지 기본값은 B: 업스트림만 같은 `session_id`로 다시 연결한다(약 0.5초, 브라우저 WS 유지). 재시도 간격은 0, 1, 2, 4초이고, 모두 실패하거나 커널이 404면 브라우저 WS를 1011로 닫는다. 진단용 숨김 옵션은 `--no-upstream-reconnect`와 `--debug-drop-upstream-after <초>`다. `--debug-drop-reply-once`는 local 모드에서도 쓸 수 있고, 커널마다 첫 실행의 `execute_reply`와 그 실행의 `status: idle`을 브라우저로 보내지 않는다(응답 누락 재현용).

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

### 다음 작업 (WSL, 우선순위 순)
1. **Colab 안내 범위 결정**: 끊김은 간헐적이고, 끊길 때 메시지 약 1개를 잃을 수 있다. 프론트 대책(LMS 95a346b)은 브라우저에서 확인했다. googlecolab 이슈 답변을 보고 베타로 열지 정한다
2. **Colab 브라우저 확인**: https://lms.132-145-125-71.sslip.io/courses/transformer/1 과 `/runtime-test`에서 `lms-runtime --colab` URL/Token으로 확인한다
3. **Colab 쪽 `lms_kernel` 설치 방식**: 계약 4장 "헬퍼 버전 확인"과 함께 LMS 세션과 정한다
4. **커널 헬퍼**: 환경 점검 → 변수 인스펙터 → `lms_viz` 순. 각각 결과 형식을 먼저 계약 문서에 정한다(LMS 세션과 합의)
