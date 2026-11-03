# LMS ↔ 학생 런타임 인터페이스 계약

- 버전: **0.1** (2026-11-03)
- 원본: `hongdune/LMS`의 `docs/runtime-contract.md`. `hongdune/LMS_tutorial`에는 같은 내용의 사본을 둔다.
- 변경 절차: 내용을 바꾸면 버전을 올리고 아래 변경 기록에 한 줄을 남긴 뒤, 두 저장소의 파일을 **같은 내용으로** 갱신한다. 한쪽만 바뀐 상태로 커밋하지 않는다.
- "결정 대기"로 표시한 항목은 한쪽에서 임의로 정하지 않는다. 정할 때는 이 문서부터 고친다.

## 1. 역할

| 쪽 | 저장소 | 책임 |
|---|---|---|
| LMS (프론트엔드) | LMS | 런타임 URL·토큰 입력과 보관, 커널 생성·재접속, 코드 실행 요청, 출력 렌더링, 채점 오케스트레이션 |
| LMS (중앙 서버) | LMS | 세션 키 `(user_id, course_id) → (runtime_id, kernel_id)` 저장. **런타임에 직접 접속하지 않는다** |
| 학생 런타임 | LMS_tutorial | Jupyter Server 실행, 커널 안 헬퍼(`lms_kernel`) 제공, Colab 브리지 |

## 2. 연결

- 학생 런타임은 **표준 Jupyter Server API**만 노출한다. 프론트는 아래 경로만 사용한다.
  - REST: `GET/POST /api/kernels`, `GET/DELETE /api/kernels/{id}`, `POST /api/kernels/{id}/interrupt`, `POST /api/kernels/{id}/restart`, `GET /api/kernelspecs`
  - WebSocket: `/api/kernels/{id}/channels` (Jupyter 메시지 프로토콜 v5)
- 등록 정보는 **URL + 토큰** 두 값이다. `lms-runtime`이 시작할 때 터미널에 출력하고, 학생이 LMS 화면에 입력한다.
  - URL: `http://127.0.0.1:<port>` (기본 포트 8888). 학생 PC 기준이며, WSL에서는 Windows 브라우저가 `localhost` 포워딩으로 접근한다.
  - 토큰: 실행할 때마다 새로 만든 무작위 문자열 (`secrets.token_urlsafe(32)`).
- 인증
  - REST: `Authorization: token <토큰>` 헤더. 이 방식은 XSRF 검사가 면제되므로 `disable_check_xsrf`를 켜지 않는다.
  - WebSocket: 브라우저는 WS에 헤더를 붙일 수 없으므로 `?token=<토큰>` 쿼리 파라미터를 쓴다(`@jupyterlab/services`의 기본 동작).
- origin 제한: 런타임은 `--origin <LMS 프론트엔드 origin>`으로 실행한다. 1단계에서는 OCI의 프론트엔드 주소다(예: `http://<OCI 주소>:3000`). 값은 브라우저가 보내는 `Origin` 헤더와 **정확히 일치**해야 한다(스킴·호스트·포트, 끝의 `/` 없음).
  - `Access-Control-Allow-Origin`은 항상 이 값으로 고정해서 응답한다.
  - Jupyter Server는 기본적으로 토큰 인증된 요청의 Origin 검사를 건너뛴다. 그래서 런타임은 `StrictOriginIdentityProvider`로 **토큰이 있어도 Origin을 검사**한다. 다른 origin에서 온 REST 요청은 404, WebSocket 연결은 거부된다. `Origin` 헤더가 없는 요청(스크립트·CLI)은 허용된다.
- 바인딩: 런타임은 `127.0.0.1`에만 바인딩한다. `0.0.0.0`으로 열지 않는다.
- 토큰 취급: 토큰은 브라우저 저장소에만 둔다. 중앙 서버로 보내지 않고, 양쪽 모두 로그에 남기지 않는다(런타임 시작 시 터미널 출력은 예외).

## 3. 런타임 종류

중앙 서버에는 별칭과 종류만 저장한다: `local` / `remote` / `colab`.

- `local`: 학생 PC의 `lms-runtime`.
- `colab`: 학생 PC의 `lms-runtime --colab <session>`이 연 로컬 브리지. 프론트 입장에서 `local`과 같은 URL + 토큰이며, 2장의 경로를 그대로 중계한다.
  - Colab 런타임은 예고 없이 종료될 수 있다. 브리지는 이를 커널 `dead` 또는 REST 오류로 드러내고, 프론트는 2.4의 복구 흐름으로 처리한다.
  - Colab 전용 메시지(`colab_request` 등)는 1단계에서 브리지가 버리거나 무시한다.
- `remote`: 연구실 GPU 서버 등. 2장의 규칙을 그대로 따르는 Jupyter Server. (1단계 범위 밖)

## 4. 커널 안 헬퍼 (`lms_kernel`)

- 학생 런타임의 커널 환경에는 `lms_kernel` 패키지가 설치되어 있어야 한다.
- 프론트가 커널에 주입·실행하는 헬퍼 코드와 그 결과로 생기는 이름은 모두 `_lms_` 접두사를 쓴다. 학생 네임스페이스에 다른 이름을 남기지 않는다.
- 헬퍼 호출은 `silent: true`, `store_history: false`로 실행해 실행 카운터와 히스토리를 건드리지 않는다.

| 기능 | 요구사항 | 결과 전달 형식 |
|---|---|---|
| 변수 인스펙터 | 2.9 | **결정 대기** (JSON 스키마, 전달 방식: `user_expressions` 또는 `display_data`) |
| 환경 점검 | 2.10 | **결정 대기** (Python·패키지 버전, CUDA/MPS, GPU 이름·메모리 JSON) |
| 시각화 위젯 (`lms_viz`) | 2.11 | `display_data`의 커스텀 MIME `application/vnd.lms.<widget>+json`. 위젯별 스키마는 **결정 대기** |
| 헬퍼 버전 확인 | — | **결정 대기**. 프론트가 런타임의 `lms_kernel` 버전을 확인하고, 계약 버전과 맞지 않으면 업데이트를 안내한다 |

## 5. 채점 커널 (2.6)

- 프론트가 같은 런타임에 **새 커널을 만들어** 학생 코드와 테스트 코드만 실행하고, 결과를 받은 뒤 그 커널을 종료한다.
- 테스트 코드는 학생에게 공개되는 것을 전제로 한다. 결과 보고 형식은 **결정 대기**.

## 변경 기록

- 0.1 (2026-11-03): 저장소 분리와 함께 최초 작성.
