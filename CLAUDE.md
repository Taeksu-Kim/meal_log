# meal_log — 프로젝트 규칙

공통 규칙은 상위 `../CLAUDE.md`를 따른다. 이 파일은 meal_log 전용. 설계·단계 계획은 `DESIGN.md`.

- 목적: 카드 결제(card_spending)를 끼니(아침·점심·저녁·간식·야식)로 나눠 캘린더로 보여 주고, 메뉴·kcal(추정, 범위)를 붙인다.
- **개인정보 규칙 (가장 우선):**
  - 식단·카드 기록은 `data/`(meals.db)에만. `data/`는 `.gitignore` — 어떤 저장소에도 올리지 않는다.
  - 실제 데이터는 이 PC 밖으로 보내지 않는다. LLM은 로컬 vLLM만 (`llm.py` 한 곳에서 연결). 쓰는 곳 세 개:
    채팅 입력 해석(`chat.py`) -- 사용자가 친 문장 + 메뉴 이름 목록, kcal 추정(`suggest.py`) -- 메뉴 이름·단위, 대화 도우미 로컬 세션.
    금액·가게·카드 정보는 보내지 않는다. LLM 답은 제안이고 사람이 넣기·저장을 눌러야 반영된다.
  - **예외 하나 (사용자 결정 2026-11-15)**: 대화 탭에서 사용자가 **Claude Code 세션을 고른 대화만** 대화 내용과 식단(끼니·메뉴·수량·
    kcal·인원)이 Anthropic으로 나간다. 결제 금액·가게·카드 범주·결제일은 나가지 않는다 -- 걸러 내는 곳은 `chat_tools._meal` 하나,
    `tests/test_assistant.py`가 지킨다. 도구 결과에 새 필드를 넣을 때 이 규칙부터 본다. 기본 세션은 로컬 LLM.
  - 대화 도우미 지침은 `assistant/GUIDE.md` (두 세션 공용, 개발용 CLAUDE.md가 아님). Claude 세션은 `--setting-sources ""`라
    이 CLAUDE.md는 도우미에게 안 실린다.
  - 테스트·점검·문서 예시는 전부 지어낸 가짜 데이터 (`tests/fake_card.py`). 실제 결제 행을 복사해 예시로 쓰지 않는다.
  - 실제 데이터로 돌린 결과는 **집계 숫자만** 출력 (행 단위 가게 이름·금액 출력 금지).
  - 올린 영수증·캡처 이미지는 `data/captures/`, 정확도 평가용 실제 사진·정답은 `data/eval_captures/` (git 제외). OCR은 이 PC의
    doc_parser OCR 서버만. 실제 사진의 글자를 테스트 코드에 옮기지 않는다 -- 테스트는 같은 구조를 지어낸 값으로 (`tests/test_captures.py`).
- **card_spending 코드는 고치지 않는다** (다른 세션이 작업 중일 수 있음). 필요한 변경은 사용자에게 제안만.
  연결은 API만 (`GET /api/period`, `GET /api/transactions?majors=식비&month=YYYY-MM`) — card.db를 직접 읽지 않는다.
- 사람마다·시간에 따라 바뀌는 값: kcal 기준표·가게→메뉴 규칙은 `menu_kcal.py`, 끼니 시간대 기본값·범주별 처리는 `slots.py`.
  kcal 값은 손으로 적은 근사값 [확인 안 함]이거나 식약처 음식 DB 외식 값(`DB_VALUES`, `tools/kcal_db_compare.py`) —
  화면에 항상 범위와 출처를 같이 보여 준다. DB 파일은 저장소에 넣지 않는다 (사용자가 내려받은 경로를 인자로).
  기준표에 없는 메뉴는 `compose.py`가 이름을 풀어 계산 (재료 100g당 값 = DB 행, 근거 문자열에 행 이름). 출처 이름·내 메뉴 출처는
  `menu_kcal.SOURCE_WORD`·`CUSTOM_SOURCES` 한 곳.
- **이름·글자 규칙(정규식)은 늘리지 않는 쪽이 기본** (사용자 방향 2026-11-15, `compose.py`·`receipts.py`·`chat.py`):
  규칙은 평가(`tools/*_eval.py`)나 실제 캡처·입력에서 **같은 실수가 반복될 때만** 더한다 — "이런 이름도 있겠지"로 미리 늘리지 않는다.
  끝없는 이름 변형(모르는 앞말 등)은 규칙 대신 로컬 LLM이 후보 중에서 **고르게** 하고(숫자는 DB·기준표에서만),
  사람이 한 번 확인한 메뉴는 내 메뉴로 저장해 다시 계산하지 않는다.
- **층**: 엔진(slots·receipts·chat·analysis) → 서비스(`service.py`, 상태의 유일한 주인) → 화면 API(`app.py`)와 A2A(`a2a_tasks.py`).
  새 동작은 서비스에 넣고 두 창구가 부른다 (같은 정책을 창구에 따로 쓰지 않는다). 서비스 실패는 이름 있는 예외(Invalid·Missing·
  Conflict·Unavailable·Upstream), 창구가 HTTP·JSON-RPC로 바꾼다. A2A를 바꾸면 `tests/test_a2a.py` + 브라우저 점검의 A2A 절.
- 경험 기억(`memory.py`)은 **저장하지 않고 확정된 기록에서 매번 계산**한다 (사용자 결정 2026-11-15). 기억 표를 따로 만들지 않는다 —
  저장하는 건 [틀림] 목록(`memory_rejects`)뿐. 제안은 제안만 (자동 채우기 X).
- 저장소가 지키는 것: 사람이 고친 값(끼니 옮김 `slot_locked`, 메뉴 `menu_user`, 인원·제외)은 동기화가 덮어쓰지 않는다.
- 검증: 가짜 데이터로 `python -m pytest -q` + 화면을 바꿨으면 `python tools/browser_check.py` (실제 브라우저, 늦게 온 옛 응답까지).
- 서버는 127.0.0.1 고정, 포트 8766. 멈출 때는 `pkill -f "[m]eal_log serve"` (대괄호 없이 쓰면 그 명령을 실행한 셸까지 죽는다).
