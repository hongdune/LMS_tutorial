# LMS ↔ 학생 런타임 인터페이스 계약

- 버전: **0.6** (2026-11-03)
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
    - 런타임은 WebSocket 서브프로토콜(`v1.kernel.websocket.jupyter.org`)을 선택하지 않을 수 있다. 프론트는 서브프로토콜 없는 기존 JSON 방식도 지원해야 한다(`@jupyterlab/services`는 기본 지원).
    - 프론트는 끊긴 WebSocket을 같은 `session_id` 쿼리로 다시 연결한다. 런타임(브리지 포함)은 `session_id`를 그대로 전달해, Jupyter Server가 끊긴 동안 쌓인 메시지를 재전송할 수 있게 한다.
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
  - 오류 형식: Colab 런타임이 종료됐으면 REST `503`, 브리지가 Colab에 연결할 수 없으면 `502`. 본문은 `{"message": "<한국어 안내>"}`이며 프론트는 `message`를 그대로 보여 준다. 열려 있던 WebSocket은 코드 `1011`로 닫는다.
  - `colab stop` 직후 최대 약 15초 동안은 런타임이 정상처럼 응답할 수 있다(브리지가 세션 상태를 주기적으로 확인하는 간격).
  - Colab 전용 메시지(`colab_request` 등)는 1단계에서 브리지가 버리거나 무시한다.
- `remote`: 연구실 GPU 서버 등. 2장의 규칙을 그대로 따르는 Jupyter Server. (1단계 범위 밖)

## 4. 커널 안 헬퍼 (`lms_kernel`)

- 학생 런타임의 커널 환경에는 `lms_kernel` 패키지가 설치되어 있어야 한다. 프론트가 부르는 함수는 모두 `lms_kernel.frontend` 모듈에 둔다.
- 학생 네임스페이스에 이름을 남기지 않는다. 헬퍼가 어쩔 수 없이 이름을 만들어야 하면 `_lms_` 접두사를 쓴다.

### 4.1 호출 방식 (인스펙터·환경 점검·버전 확인)

프론트는 빈 코드와 `user_expressions`로 헬퍼를 부른다.

```json
{
  "code": "",
  "silent": true,
  "store_history": false,
  "user_expressions": {"r": "__import__('lms_kernel.frontend').frontend.variables()"}
}
```

- `__import__`로 부르므로 학생 네임스페이스에 `import` 이름이 생기지 않는다. `silent`이므로 실행 카운터·히스토리·출력이 생기지 않는다.
- 헬퍼 함수는 `_repr_json_()`을 가진 객체를 반환한다. 결과는 `execute_reply`의 `user_expressions.r.data["application/json"]`으로 온다.
- 헬퍼가 예외를 내면 `user_expressions.r`은 `{"status": "error", "ename", "evalue", "traceback"}`이다. `lms_kernel`이 없으면 `ModuleNotFoundError`가 오며, 프론트는 설치 안내(온보딩)로 보낸다.
- 인자는 프론트가 `JSON.stringify`로 만든 문자열·숫자·문자열 배열만 쓴다. 이 값은 파이썬 리터럴로도 유효하다.
- JSON 값에 `NaN`·`Infinity`는 쓰지 않는다(`null`로 바꾼다). 모든 결과 객체에는 `"version": 1`(스키마 버전)을 넣는다. 필드를 추가하는 것은 버전을 올리지 않고, 의미를 바꾸거나 필드를 지울 때 올린다.
- 헬퍼는 학생 객체의 값을 계산하지 않는다(텐서 원소 읽기, GPU 동기화, `__repr__`가 무거운 객체 전체 출력 금지). 메타데이터만 읽는다.

### 4.2 헬퍼 버전 확인 — `info()`

```json
{"version": 1, "lms_kernel": "0.3.0", "contract": "0.6"}
```

- 프론트는 커널에 붙은 직후 한 번 부른다. `contract`가 프론트가 아는 계약 버전과 다르면 런타임 업데이트를 안내한다(실행은 막지 않는다).

### 4.3 환경 점검 (2.10) — `env(requirements)`

인자는 `course.yaml`의 `requirements` 배열(예: `["torch>=2.2", "numpy"]`).

```json
{
  "version": 1,
  "python": "3.12.3",
  "platform": "Linux-6.6.87-microsoft-standard-WSL2-x86_64",
  "packages": [
    {"requirement": "torch>=2.2", "name": "torch", "installed": "2.5.1", "ok": true},
    {"requirement": "numpy", "name": "numpy", "installed": null, "ok": false}
  ],
  "torch": {
    "version": "2.5.1",
    "cuda": {"available": true, "version": "12.4",
             "devices": [{"name": "Tesla T4", "memory_total": 15828320256}]},
    "mps": false
  }
}
```

- `packages`: 요구사항마다 하나. 설치 버전은 배포판 메타데이터(`importlib.metadata`)로 읽고 버전 비교는 PEP 440(`packaging`)을 따른다. 요구사항 문자열을 해석할 수 없으면 `name: null`, `installed: null`, `ok: false`와 `"error": "<이유>"`를 넣는다.
- `torch`: torch가 설치돼 있지 않으면 `null`. 설치돼 있으면 import해서 채운다(몇 초 걸릴 수 있다). 설치돼 있지만 import가 실패하면 `version`, `cuda.available: false`, `mps: false`와 `"error": "<이유>"`를 넣는다. CUDA가 없으면 `cuda.available: false`, `version: null`, `devices: []`. 메모리 단위는 바이트.
- 설치 명령 안내는 프론트가 결과를 보고 만든다(헬퍼는 명령을 만들지 않는다).

### 4.4 변수 인스펙터 (2.9) — `variables()`, `module_tree(name)`

`variables()`:

```json
{
  "version": 1,
  "variables": [
    {"name": "q", "type": "torch.Tensor", "summary": "Tensor(2, 4, 8)",
     "shape": [2, 4, 8], "dtype": "torch.float32", "device": "cpu", "nbytes": 256},
    {"name": "model", "type": "torch.nn.modules.transformer.Transformer", "summary": "Transformer",
     "params": 44140544, "trainable": 44140544},
    {"name": "lr", "type": "float", "summary": "0.001"}
  ],
  "truncated": false
}
```

- 대상: IPython 사용자 네임스페이스(`user_ns`)에서 `_`로 시작하는 이름, 모듈 객체, IPython·ipykernel이 넣은 이름을 뺀 나머지. 넣은 이름은 IPython `%who`와 같은 규칙으로 판별한다: `user_ns_hidden`에 같은 이름이 있고 값이 **같은 객체**면 제외(`In`, `Out`, `exit`, `quit`, `get_ipython`, ipykernel의 `open` 등). 학생이 같은 이름에 새 값을 넣었으면 포함한다. 이름순으로 최대 200개, 넘으면 `truncated: true`.
- 공통 필드: `name`, `type`(모듈 경로를 포함한 클래스 이름), `summary`(한 줄, 최대 120자). 규칙대로 만든 값이 120자를 넘으면 120자에서 자르고 끝에 `…`를 붙인다(예: 119~120자 문자열의 repr은 따옴표 때문에 넘친다).
- `torch.Tensor`·`numpy.ndarray`: `shape`, `dtype`, `device`(ndarray는 `"cpu"`), `nbytes`. `dtype`은 각 라이브러리의 표기를 그대로 쓴다(torch `"torch.float32"`, numpy `"float32"`).
- `torch.nn.Module`: `params`(전체 파라미터 수), `trainable`(`requires_grad`인 파라미터 수).
- 그 밖의 타입은 공통 필드만. `summary`는 숫자·bool·None·120자 이하 문자열이면 `repr`, 긴 문자열·컨테이너면 `str[500]`·`list[3]`처럼 길이만, 그 밖에는 클래스 이름. `bit_length() > 400`(약 120자리)인 int는 repr 대신 `int`로 표시한다(120자에서 잘린 숫자는 의미가 없고, 아주 큰 int는 파이썬이 repr을 거부한다). 학생이 정의한 함수·클래스도 포함한다(`type: "function"` 등).

`module_tree(name)`: 이름이 가리키는 `nn.Module`의 하위 모듈 트리.

```json
{"version": 1, "name": "model", "type": "torch.nn.modules.transformer.Transformer",
 "params": 44140544, "trainable": 44140544, "children": [
   {"name": "encoder", "type": "...TransformerEncoder", "params": 18914304, "trainable": 18914304, "children": []}
 ], "truncated": false}
```

- 깊이 최대 6, 노드당 자식 최대 100개. 루트에는 `truncated`를 항상 넣고, 하위 노드에는 잘렸을 때만 `truncated: true`를 넣는다.
- 이름이 없거나 `nn.Module`이 아니면 `KeyError`/`TypeError`를 낸다(4.1의 오류 형식).

### 4.5 시각화 위젯 (2.11) — `lms_viz`

- 커널 → 프론트 전달은 `display_data`의 커스텀 MIME `application/vnd.lms.<widget>+json`이다. 번들에는 항상 `text/plain` 대체 표현을 함께 넣어 Jupyter·Colab에서도 무언가 보이게 한다.
- 실시간 갱신(학습 곡선)은 Jupyter 표준 `display_id` + `update_display_data`를 쓴다. 갱신 메시지는 **전체 데이터**를 다시 보낸다(부분 추가 없음). 프론트는 같은 `display_id`의 출력을 교체한다.
- 크기 제한은 헬퍼가 지킨다. 넘으면 heatmap은 앞쪽 256×256만 남기고(라벨과 칸을 맞추기 위해), line·scatter는 첫 점과 끝 점을 포함해 고르게 줄인 뒤 `"truncated": true`를 넣는다. 제한 안이면 이 필드는 없다.
- 공통 선택 필드: `title`, `x_title`, `y_title`.

| MIME | 필드 | 제한 |
|---|---|---|
| `application/vnd.lms.heatmap+json` | `version`, `data`(2차원 숫자 배열, 행 우선), `x_labels`, `y_labels`(선택, 문자열 배열), `color`(`"sequential"` 기본 / `"diverging"`) | 256×256 셀 |
| `application/vnd.lms.line+json` | `version`, `series`: `[{"name", "x": [숫자], "y": [숫자]}]` | 시리즈당 10,000점, 시리즈 20개 |
| `application/vnd.lms.scatter+json` | `version`, `x`, `y`(숫자 배열), `label`(선택, 점마다 문자열), `group`(선택, 점마다 문자열 — 색 구분) | 5,000점 |

- 슬라이더·드롭다운으로 코드를 다시 실행하는 상호작용(2.11 둘째 항목)은 콘텐츠 문법과 프론트의 일이며 이 계약에 포함하지 않는다.

## 5. 채점 커널 (2.6)

- 프론트가 같은 런타임에 **새 커널을 만들어** 학생 코드와 테스트 코드만 실행하고, 결과를 받은 뒤 그 커널을 종료한다.
- 테스트 코드는 학생에게 공개되는 것을 전제로 한다. 결과 보고 형식은 **결정 대기**.

## 변경 기록

- 0.1 (2026-11-03): 저장소 분리와 함께 최초 작성.
- 0.2 (2026-11-03): WebSocket 서브프로토콜 미선택 허용과 `session_id` 전달(2장), Colab 브리지 오류 형식과 종료 감지 지연(3장).
- 0.6 (2026-11-03): 인스펙터 `summary`의 큰 int 기준(`bit_length() > 400`)과 120자 초과 시 자르기 규칙을 구현(LMS_tutorial 37d5790)에 맞춤.
- 0.5 (2026-11-03): LMS_tutorial 구현(d202e0c)에서 정한 세부 사항 반영 — `env`의 해석 불가 요구사항(`name: null`)과 torch import 실패(`torch.error`), 인스펙터 `dtype` 표기·`summary` 규칙·함수 포함, `module_tree` 루트 `truncated`, `lms_viz` 축소 방식.
- 0.4 (2026-11-03): 인스펙터 제외 규칙을 `user_ns_hidden` 기준으로 변경(ipykernel이 넣는 `open`이 변수로 잡히던 문제). 4.1 호출 방식은 실제 ipykernel로 검증함.
- 0.3 (2026-11-03): 커널 안 헬퍼 확정(4장) — `user_expressions` 호출 방식, `info`·`env`·`variables`·`module_tree` 결과 스키마, `lms_viz` 위젯 3종(heatmap·line·scatter)의 MIME과 스키마, 실시간 갱신은 `update_display_data`.
