"""브라우저 점검: 가짜 card_spending 결제로 meal_log 화면을 실제 Chromium(headless)에서 눌러 본다.

    python tools/browser_check.py            # 임시 DB로 서버를 직접 띄우고, 끝나면 정리 (실제 data/는 건드리지 않음)

단위 테스트(tests/)는 요청을 하나씩 보내서 화면의 동시 요청(늦게 온 옛 응답이 화면을 덮음)을 못 잡는다 -- 여기서
특정 요청을 서버에서 일부러 늦춰 확인한다. card_spending/tools/browser_check.py를 본떴다. 화면을 바꾸면 이것도 돌린다.
한글 조합 중 Enter는 합성 이벤트(isComposing=true)로 확인한다 -- 실제 IME 입력기는 아니다.
"""
import asyncio
import json
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from meal_log.app import create_app  # noqa: E402
from meal_log.sources import CardSourceDown  # noqa: E402
from tests.fake_card import FakeCard, row  # noqa: E402
from doc_parser import OcrItem  # noqa: E402
from tests.test_captures import LABEL, PAPER, PAPER_DELIVERY, FakeOcr, img  # noqa: E402
from tests.test_chat import DownLLM, PromptLLM  # noqa: E402
from tests.test_assistant import FakeClaude  # noqa: E402
from meal_log.chat_backends import ClaudeBackend, LocalBackend  # noqa: E402

LONG = "가나다라마바사아자차카타파하 본점 2층 푸드코트 김밥"   # 긴 가게 이름 (넘침 확인)
# 가짜 결제 (가게 이름·금액은 지어낸 값). 마지막 결제일 10/14 -> 처음 정리 기준일 10/7
ROWS = [
    row("2026-09-28", "12:10", "가나김밥", 4500),
    row("2026-09-30", "19:00", "다라돼지국밥", 9000),
    row("2026-10-01", "10:29", "마바분식", 6000),                     # 아침 끝
    row("2026-10-01", "10:30", "사아식당", 9000),                     # 점심 시작, 메뉴 모름 (기준일 이전 -> 모름)
    row("2026-10-01", "12:20", "자차커피", 4500, "카페·간식"),        # 점심 시간 커피 = 간식
    row("2026-10-02", "00:40", "쿠팡이츠", 21000, "배달"),            # 자정 넘김 -> 10/1 야식
    row("2026-10-02", "12:00", "카타국밥", 9000), row("2026-10-02", "12:30", "파하김밥", 4000),   # 같은 끼니 2건
    row("2026-10-03", "18:00", "가나마트", 52000, "마트·장보기"),
    *[row(f"2026-10-{d:02d}", "19:30", "우아한형제들", 20000, "배달") for d in range(5, 11)],
    row("2026-10-12", "20:00", "우아한형제들", 48000, "배달"),        # 평소의 2배 이상 -> 인원 확인
    row("2026-10-13", "12:00", LONG, 5000),
    row("2026-10-14", "13:00", "가나순대", 9000),
]


class SwitchableSource:
    """card_spending을 껐다 켰다 하는 가짜 (꺼짐 안내 화면 확인용)."""

    def __init__(self, fake: FakeCard) -> None:
        self.fake, self.down, self.url = fake, True, "http://127.0.0.1:8765"

    def fetch(self):
        if self.down:
            raise CardSourceDown(f"card_spending 서버({self.url})에 연결할 수 없음")
        return self.fake.source.fetch()


TMP = Path(tempfile.mkdtemp(prefix="meal_ui_"))
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
URL = f"http://127.0.0.1:{PORT}/"
SRC = SwitchableSource(FakeCard(ROWS))
class SwitchLLM:
    """채팅 해석용 가짜 로컬 LLM (문장 내용으로 답함). down=True면 꺼진 것처럼."""

    def __init__(self) -> None:
        self.down = False
        self.fake = PromptLLM({'"음식": "옥수수"': {"known": True, "unit": "개", "low": 80, "mid": 130, "high": 200,
                                                    "basis": "중간 크기 약 150g"},
                               "점심엔 김밥, 저녁엔 라면": {"entries": [
            {"date": time.strftime("%Y-%m-%d"), "slot": "점심", "items": [{"name": "김밥", "qty": 1, "unit": ""}]},
            {"date": time.strftime("%Y-%m-%d"), "slot": "저녁", "items": [{"name": "라면", "qty": 1, "unit": ""}]}]}})

    def complete(self, system, user, *, schema=None):
        if "식단 도우미 지침" in system and not self.down:   # 대화 도우미 (로컬 LLM 세션): 도구 한 번 -> 답
            last = user.rsplit("사용자: ", 1)[-1]
            if "[도구" in last:
                return json.dumps({"action": "answer", "text": "10/13(화) 아침에 고구마 1개를 넣었습니다. /#2026-10-13"}, ensure_ascii=False)
            if "고구마" in last:
                return json.dumps({"action": "tool", "tool": "log_meal",
                                   "args": {"text": last.splitlines()[0], "date": "2026-10-13"}}, ensure_ascii=False)
            return json.dumps({"action": "answer", "text": "드신 것을 알려 주세요."}, ensure_ascii=False)
        if "느린떡" in user:   # 느린 kcal 추정
            time.sleep(2)
            return '{"known": true, "unit": "1개", "low": 111, "mid": 122, "high": 133, "basis": "느림"}'
        if "천천히" in user:   # 느린 해석 (옛 응답이 늦게 도착하는 상황)
            time.sleep(2)
            return '{"entries": [{"date": "%s", "slot": "야식", "items": [{"name": "라면", "qty": 1, "unit": ""}]}]}' % time.strftime("%Y-%m-%d")
        return (DownLLM() if self.down else self.fake).complete(system, user, schema=schema)


class SwitchOcr(FakeOcr):
    """가짜 OCR (이미지 폭으로 지어낸 영수증 글자를 돌려줌). down=True면 연결이 끊긴 것처럼."""
    down = False

    def read(self, png, mode):
        if self.down:
            raise OSError("connection refused")
        import io as _io
        from PIL import Image as _Image
        w = _Image.open(_io.BytesIO(png)).width
        if w in (305, 306):   # 실제 사진과 같은 구조의 지어낸 OCR 결과 (영양정보 라벨 / 배민 종이 영수증)
            self.reads += 1
            return LABEL if w == 305 else PAPER_DELIVERY
        if w == 308:          # 배달앱 주문상세 (날짜·결제금액 잘림): 메뉴 19,000 + 배달팁 1,000 = 20,000원 추정
            self.reads += 1
            from tests.test_captures import DETAIL
            return [OcrItem(type="text", bbox=(0, 0, 1, 1), text=DETAIL.replace("25,000원", "19,000원").replace("2,000원", "1,000원"))]
        if w == 307:          # A2A로 보낼 편의점 영수증 (짝 없음)
            self.reads += 1
            return [OcrItem(type="text", bbox=(0, 0, 1, 1), text=PAPER.replace("라마바", "사아자"))]
        return super().read(png, mode)


LLM = SwitchLLM()
OCR = SwitchOcr()
APP = create_app(TMP / "ui.db", source=SRC, llm=LLM, ocr=OCR)


class SlowClaude(FakeClaude):
    """가짜 claude -p: 단계 ("wait", 초)로 오래 걸리는 답 (멈추기 확인)."""

    def __call__(self, cmd, cwd):
        for line in super().__call__(cmd, cwd):
            yield line
            if '"init"' in line and self.script and self.script[0] and self.script[0][0][0] == "wait":
                time.sleep(self.script.pop(0)[0][1])
                self.script.insert(0, [("text", "늦은 답입니다.")])


CLAUDE = SlowClaude()
_svc = APP.state.service
CLAUDE.svc = _svc
_svc.backends = {"local": LocalBackend(_svc, _svc._get_llm, ping=lambda: not LLM.down),
                 "claude": ClaudeBackend("http://127.0.0.1:1", runner=CLAUDE, claude_bin="/fake/claude")}
SLOW: set[str] = set()   # 이 경로+쿼리로 시작하는 요청만 늦게 답한다 (옛 응답이 나중에 도착하는 상황)


@APP.middleware("http")
async def _delay(request, call_next):
    if any(f"{request.url.path}?{request.url.query}".startswith(x) for x in SLOW):
        await asyncio.sleep(1.5)
    return await call_next(request)


server = uvicorn.Server(uvicorn.Config(APP, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(40):
    try:
        urllib.request.urlopen(URL + "api/status", timeout=2)
        break
    except Exception:
        time.sleep(0.25)
FALLBACK = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
BAD_WORDS = ("null", "undefined", "NaN")
# 화면 폭을 넘는 요소 (가장 안쪽 것 몇 개) -- 가로 스크롤 원인 찾기
OVERFLOW_JS = """[document.documentElement.scrollWidth, innerWidth, [...document.querySelectorAll('body *')]
  .filter(e => e.getBoundingClientRect().right > innerWidth + 1 && ![...e.children].some(c => c.getBoundingClientRect().right > innerWidth + 1)
          && !e.closest('.tablewrap') && getComputedStyle(e).visibility !== 'hidden')
  .slice(0, 5).map(e => { const path = []; for (let x = e; x && x !== document.body; x = x.parentElement)
    path.push(x.tagName + (x.id ? '#' + x.id : '') + (x.className && typeof x.className === 'string' ? '.' + x.className.split(' ')[0] : '')
              + ':' + Math.round(x.getBoundingClientRect().width));
    return path.slice(0, 6).join(' < '); })]"""

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail else ""))


def get(path):
    return json.load(urllib.request.urlopen(URL + path.lstrip("/"), timeout=5))


def rpc(method, params):
    """다른 에이전트처럼 A2A JSON-RPC로 보낸다."""
    req = urllib.request.Request(URL + "a2a", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))


def clean(page) -> bool:
    t = page.inner_text("body")
    return not any(w in t for w in BAD_WORDS)


def cell(page, day, slot):
    r = page.locator(f"#cal .day[data-date='{day}'] .r[data-slot='{slot}']")
    return (r.get_attribute("class") or "").replace("r ", ""), r.locator(".v").inner_text()


def wait_cell(page, day, slot, state):
    page.wait_for_function(
        f"document.querySelector(\"#cal .day[data-date='{day}'] .r[data-slot='{slot}']\")?.classList.contains('{state}')")


def pick_day(page, day):
    page.wait_for_selector("#cal:not([aria-busy])")   # 달을 바꾸는 중이면 옛 달 칸이 아직 보임 -- 다 그린 뒤에 고른다
    if page.locator(f"#cal .day[data-date='{day}']").count() == 0:   # 다른 달이 보이고 있으면 주소의 #날짜로 이동
        page.evaluate(f"location.hash = '#{day}'")
        page.wait_for_selector(f"#cal:not([aria-busy]) .day[data-date='{day}']")
    page.locator(f"#cal .day[data-date='{day}']").click()
    page.wait_for_function(f"document.querySelector('#dayBody').dataset.day === '{day}'")


def entry(page, text):
    return page.locator("#dayBody .entry", has_text=text)


def undo(page, said):
    """알림 `said`가 뜬 것을 확인한 뒤 그 알림의 [되돌리기]를 누른다. 앞 동작의 알림(15초 유지)이 아직 떠 있을 때
    그걸 누르면 엉뚱한 동작이 되돌려진다 (2026-11-15: 10/10 치킨 넣기가 되돌려져 캡처 되돌리기 점검이 가끔 실패)."""
    msg = page.locator("#status .msg", has_text=said)
    msg.wait_for()
    msg.locator("button", has_text="되돌리기").click()


with sync_playwright() as p:
    try:
        b = p.chromium.launch()
    except Exception:
        b = p.chromium.launch(executable_path=str(FALLBACK[-1]))
    page = b.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # 서버가 일부러 돌려준 4xx/503(꺼짐·잘못된 설정)은 화면이 처리하는 응답이라 JS 오류로 세지 않는다
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and errors.append(m.text))

    # ---- 빈 DB + card_spending 꺼짐 (열자마자 자동 가져오기 시도)
    page.goto(URL)
    page.wait_for_function("document.querySelector('#status').textContent.includes('꺼져')")
    check("꺼짐: 알림 줄에 켜는 법", "python -m card_spending serve" in page.inner_text("#status"), page.inner_text("#status"))
    check("꺼짐: 머리글 'card_spending 꺼짐'", "꺼짐" in page.inner_text("#syncState"))
    page.wait_for_selector("#onboardSync")
    check("빈 DB: 캘린더 자리에 첫 사용 안내 + 시작 버튼", "다섯 줄로 표시" in page.inner_text("#cal")
          and page.is_visible("#onboardSync"))
    check("빈 DB: 확인할 끼니 칩 숨김", page.is_hidden("#reviewChip"))
    page.wait_for_selector("#weekRows tr")
    check("빈 DB: 주별 추이 빈 문구", "아직 기록이 없습니다" in page.inner_text("#weekRows"))
    check("빈 DB: 화면에 null/undefined/NaN 없음", clean(page))
    page.screenshot(path=str(TMP / "empty_down_1280.png"), full_page=True)

    # ---- 켜고 첫 사용 안내의 시작 버튼
    SRC.down = False
    page.click("#onboardSync")
    page.wait_for_function("document.querySelector('#syncState').textContent.includes('새로')")
    check(f"가져오기: 새로 {len(ROWS)}끼", f"새로 {len(ROWS)}끼" in page.inner_text("#syncState"), page.inner_text("#syncState"))
    page.wait_for_selector("#onboardSync", state="detached")
    check("가져온 뒤: 꺼짐 알림·첫 사용 안내 사라짐", "꺼져" not in page.inner_text("#status"))
    api_review = get("/api/review")
    n_review = api_review["count"]
    page.wait_for_function("!document.querySelector('#reviewChip').hidden")
    check(f"확인할 끼니 칩 = API {n_review}끼 (정리 기준일 {api_review['review_since']} 이후만)",
          f"확인할 끼니 {n_review}" in page.inner_text("#reviewChip") and api_review["review_since"] == "2026-10-07",
          page.inner_text("#reviewChip"))

    # ---- 해시 없이 다시 열기: 자동 가져오기는 중복 없음, 처음 고른 날 = 가장 오래된 확인할 끼니
    page.goto(URL)
    page.wait_for_function("document.querySelector('#syncState').textContent.includes('가져옴')")
    check("다시 열기: 자동 가져오기, 새로 0 (중복 없음)", "새로" not in page.inner_text("#syncState"), page.inner_text("#syncState"))
    page.wait_for_function("document.querySelector('#dayTitle').textContent.startsWith('10/7 ')")
    check("처음 고른 날 = 가장 오래된 확인할 끼니 날 (10/7)", True)
    page.wait_for_function("document.querySelector('#weekLine').textContent.includes('배달')")
    check("주 요약 한 줄 (고른 날의 주)", "10/5–10/11 주" in page.inner_text("#weekLine"), page.inner_text("#weekLine"))

    # ---- 식판 칸
    got = [cell(page, "2026-10-01", s) for s in ("아침", "점심", "저녁", "간식", "야식")]
    check("10/1 식판: 아침 떡볶이(추정)·점심 모름·저녁 없음·간식 아메리카노·야식 모름",
          got == [("ok", "떡볶이"), ("unknown", "모름"), ("none", ""), ("ok", "아메리카노"), ("unknown", "모름")], str(got))
    check("10/7 저녁: 확인 필요 '?'", cell(page, "2026-10-07", "저녁") == ("review", "?"))
    kc1 = page.locator("#cal .day[data-date='2026-10-01'] .kc").inner_text()
    kc7 = page.locator("#cal .day[data-date='2026-10-07'] .kc").inner_text()
    check("kcal 칸: 아는 것+모르는 것 섞인 10/1 '…+? kcal', 아무것도 모르는 10/7 '? kcal'",
          kc1.endswith("+? kcal") and kc7 == "? kcal", f"{kc1} | {kc7}")
    check("긴 가게 이름: 칸 폭을 넘지 않음",
          page.locator("#cal .day[data-date='2026-10-13']").evaluate("e => e.scrollWidth <= e.clientWidth + 1"))

    # ---- 확인 흐름: 칩 -> 가장 오래된 끼니의 검색창 -> Enter -> 다음 끼니
    page.click("#reviewChip")
    page.wait_for_selector("#picker.open")
    check("칩 누르면 10/7 끼니의 메뉴 검색창이 열림", page.inner_text("#dayTitle").startswith("10/7 ")
          and page.evaluate("document.activeElement.id") == "pickInput")
    page.fill("#pickInput", "치킨")
    first = page.locator("#pickList li").first.inner_text()
    check("'치킨' 검색: 첫 줄 치킨 + kcal 범위", "치킨" in first and "kcal" in first, first.replace("\n", " "))
    page.keyboard.press("Enter")
    wait_cell(page, "2026-10-07", "저녁", "ok")
    check("메뉴 넣으면 칸에 '치킨' + 되돌리기 알림", cell(page, "2026-10-07", "저녁") == ("ok", "치킨")
          and "되돌리기" in page.inner_text("#status"), page.inner_text("#status").replace("\n", " "))
    page.wait_for_function("document.querySelector('#dayTitle').textContent.startsWith('10/8 ')")
    # 10/7에 넣은 치킨이 경험 기억이 되어, 같은 금액인 10/8에는 검색창 대신 제안 [넣기]에 포커스
    page.wait_for_function("document.activeElement?.classList.contains('take')")
    check("연속 확인: 다음 끼니(10/8)는 같은 금액 제안 '치킨' [넣기]에 포커스 (검색창 안 열림)",
          "치킨" in entry(page, "우아한형제들").locator(".sugg").inner_text() and not page.is_visible("#picker"))
    entry(page, "우아한형제들").locator(".addmenu").click()   # 제안 대신 직접 고르기
    page.wait_for_selector("#picker.open")
    check("칩 숫자 줄어듦", f"확인할 끼니 {n_review - 1}" in page.inner_text("#reviewChip"), page.inner_text("#reviewChip"))

    # 한글 조합 중 Enter는 선택이 아님 (합성 이벤트)
    page.fill("#pickInput", "국밥")
    page.evaluate("""document.getElementById('pickInput').dispatchEvent(
        new KeyboardEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true, cancelable: true}))""")
    page.wait_for_timeout(400)
    check("한글 조합 중 Enter: 선택하지 않고 검색창 유지", page.is_visible("#picker") and page.input_value("#pickInput") == "국밥"
          and cell(page, "2026-10-08", "저녁")[0] == "review")
    page.keyboard.press("Enter")   # 조합이 끝난 뒤의 Enter = 선택
    wait_cell(page, "2026-10-08", "저녁", "ok")
    check("조합 끝난 Enter: '돼지국밥' 저장", cell(page, "2026-10-08", "저녁") == ("ok", "돼지국밥"), str(cell(page, "2026-10-08", "저녁")))
    page.wait_for_function("document.querySelector('#dayTitle').textContent.startsWith('10/9 ')")
    page.wait_for_function("document.activeElement?.classList.contains('take')")
    rows = entry(page, "우아한형제들").locator(".sugg").all_inner_texts()
    check("10/9: 같은 금액 제안 두 줄, 최근 것(10/8 돼지국밥) 먼저", len(rows) == 2 and "돼지국밥" in rows[0] and "치킨" in rows[1]
          and "2번 중 1번" in rows[0], " | ".join(rows))
    entry(page, "우아한형제들").locator(".addmenu").click()
    page.wait_for_selector("#picker.open")
    page.keyboard.press("Escape")
    check("Esc로 검색창 닫힘", not page.is_visible("#picker"))

    # 메뉴 빼기 -> 되돌리기
    pick_day(page, "2026-10-08")
    entry(page, "우아한형제들").locator(".chip .x").click()
    wait_cell(page, "2026-10-08", "저녁", "review")
    undo(page, '을(를) 뺐습니다')
    wait_cell(page, "2026-10-08", "저녁", "ok")
    check("메뉴 빼기 → 되돌리기로 복구", cell(page, "2026-10-08", "저녁") == ("ok", "돼지국밥"))

    # 먹지 않음 -> 칸에서 빠짐 -> 되돌리기
    pick_day(page, "2026-10-09")
    entry(page, "우아한형제들").locator("button", has_text="먹지 않음").click()
    wait_cell(page, "2026-10-09", "저녁", "none")
    page.wait_for_timeout(800)
    check("먹지 않음: 칸에서 빠지고 가게 이름에 취소선", "excluded" in (entry(page, "우아한형제들").get_attribute("class") or ""))
    check("확인 흐름 밖(날짜를 직접 고름)에서는 다른 날로 넘어가지 않음",
          page.evaluate("document.querySelector('#dayBody').dataset.day") == "2026-10-09" and not page.is_visible("#picker"))
    undo(page, '먹지 않음으로')
    wait_cell(page, "2026-10-09", "저녁", "review")
    check("먹지 않음 → 되돌리기", True)

    # 인원 2명 -> '인원 확인' 사라짐 (메뉴 모름은 남음)
    pick_day(page, "2026-10-12")
    check("평소 2배 배달에 '인원 확인'", "인원 확인" in entry(page, "48,000원").inner_text())
    entry(page, "48,000원").locator("select[aria-label='인원']").select_option("2")
    page.wait_for_function("!document.querySelector('#dayBody').textContent.includes('인원 확인')")
    check("인원 2명 → '인원 확인' 사라지고 '메뉴 모름' 남음", "메뉴 모름" in entry(page, "48,000원").inner_text())

    # ---- 빈 끼니에 직접 넣기·건너뜀·지우기·되돌리기 (기록 없는 10/15)
    pick_day(page, "2026-10-15")
    check("기록 없는 날: 안내 + 끼니마다 '+ 먹은 것 추가'", "이날 기록이 없습니다" in page.inner_text("#dayTotal")
          and page.locator("#dayBody .slot .addmenu").count() == 5)
    page.locator("#dayBody .slot[data-slot='저녁'] .addmenu").click()
    page.fill("#pickInput", "집밥")
    page.keyboard.press("Enter")
    wait_cell(page, "2026-10-15", "저녁", "ok")
    check("직접 넣기: 저녁 '집밥 한상'", cell(page, "2026-10-15", "저녁") == ("ok", "집밥 한상"))
    page.locator("#dayBody .slot[data-slot='저녁'] .addmenu").click()   # 같은 끼니에 메뉴 하나 더 (표에 없는 메뉴)
    page.fill("#pickInput", "가지볶음특")
    check("표에 없는 메뉴: '그대로 넣기'", "그대로 넣기" in page.inner_text("#pickList"))
    page.keyboard.press("Enter")
    page.wait_for_function("document.querySelector('#dayBody').textContent.includes('가지볶음특')")
    check("표에 없는 메뉴가 섞이면 '1인 약 … + ? kcal'", "+ ? kcal" in page.inner_text("#dayBody .slot[data-slot='저녁']"),
          page.inner_text("#dayBody .slot[data-slot='저녁']").replace("\n", " ")[:120])
    page.locator("#dayBody .slot[data-slot='아침'] button", has_text="건너뜀").click()
    wait_cell(page, "2026-10-15", "아침", "skip")
    check("건너뜀: 칸에 '건너뜀'", cell(page, "2026-10-15", "아침") == ("skip", "건너뜀"))
    page.locator("#dayBody .slot[data-slot='아침'] button", has_text="건너뜀 취소").click()
    wait_cell(page, "2026-10-15", "아침", "none")
    check("건너뜀 취소", True)
    page.locator("#dayBody .slot[data-slot='저녁'] button", has_text="지우기").click()
    wait_cell(page, "2026-10-15", "저녁", "none")
    undo(page, '직접 넣은 기록을 지웠습니다')
    wait_cell(page, "2026-10-15", "저녁", "ok")
    check("직접 넣은 기록 지우기 → 되돌리기로 다시 생김", "가지볶음특" in cell(page, "2026-10-15", "저녁")[1])

    # ---- 끼니 옮기기
    pick_day(page, "2026-10-14")
    check("가게 이름 추정 표시 (가나순대 → 순대국)", "가게 이름으로 추정" in entry(page, "가나순대").inner_text()
          and "순대국" in entry(page, "가나순대").inner_text())
    entry(page, "가나순대").locator("select[aria-label='끼니 옮기기']").select_option("저녁")
    wait_cell(page, "2026-10-14", "저녁", "ok")
    check("끼니 옮기기 → 칸 반영 + '저녁으로 옮겼습니다'", "저녁으로 옮겼습니다" in page.inner_text("#status"), page.inner_text("#status"))

    # ---- 채팅 입력: 오늘 아침 옥수수 (규칙, 새 끼니) -> 넣기 -> 되돌리기
    today_iso = time.strftime("%Y-%m-%d")
    check("채팅: 날짜 칸 기본값 = 오늘", page.input_value("#chatDate") == today_iso)
    page.fill("#chatInput", "오늘 아침에 옥수수 1개 먹었어")
    page.evaluate("""document.getElementById('chatInput').dispatchEvent(
        new KeyboardEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true, cancelable: true}))""")
    page.wait_for_timeout(400)
    check("채팅: 한글 조합 중 Enter는 해석하지 않음", page.inner_text("#chatPreview").strip() == "")
    page.press("#chatInput", "Enter")
    page.wait_for_selector("#chatApply")
    pv = page.inner_text("#chatPreview")
    check("채팅 미리보기: 아침 · 옥수수 · kcal 모름 · 새 끼니 · 규칙으로 해석", page.locator("#chatPreview select[aria-label='끼니']").input_value() == "아침"
          and "옥수수" in pv and "kcal 모름" in pv and "새 끼니로 넣기" in pv and "규칙으로 해석함" in pv, pv.replace("\n", " | ")[:200])
    check("채팅: 해석 뒤 '넣기'에 포커스 (Enter 두 번이면 끝)", page.evaluate("document.activeElement.id") == "chatApply")
    page.keyboard.press("Enter")
    page.wait_for_function("document.querySelector('#status').textContent.includes('넣었습니다')")
    got = get(f"/api/meals?start={today_iso}&end={today_iso}")
    check("채팅 넣기: 오늘 아침에 옥수수 기록 + 고른 날이 오늘로", [(m["slot"], [i["name"] for i in m["items"]]) for m in got] == [("아침", ["옥수수"])]
          and page.evaluate("document.querySelector('#dayBody').dataset.day") == today_iso and page.input_value("#chatInput") == "")
    undo(page, '옥수수을(를) 넣었습니다')
    page.wait_for_function("document.querySelector('#status').textContent.includes('되돌렸습니다')")
    check("채팅 넣기 → 되돌리기로 지워짐", get(f"/api/meals?start={today_iso}&end={today_iso}") == [])

    # 날짜 칸 + 메뉴 모르는 카드 결제 채우기 (10/10 저녁 우아한형제들)
    page.fill("#chatDate", "2026-10-10")
    page.fill("#chatInput", "저녁에 치킨 반 마리")
    page.press("#chatInput", "Enter")
    page.wait_for_selector("#chatApply")
    tsel = page.locator("#chatPreview select[aria-label='넣을 곳']")
    check("채팅: 같은 날 메뉴 모르는 결제에 채우기가 기본", tsel.count() == 1 and "이 결제에 채우기: 우아한형제들 19:30" in tsel.inner_text()
          and tsel.input_value() != "new", tsel.inner_text().replace("\n", " | ") if tsel.count() else "")
    page.click("#chatApply")
    wait_cell(page, "2026-10-10", "저녁", "ok")
    d10 = get("/api/meals?start=2026-10-10&end=2026-10-10")
    check("채팅 채우기: 새 끼니 없이 그 결제에 '치킨 ×0.5'", len(d10) == 1 and d10[0]["place"] == "우아한형제들"
          and [(i["name"], i["qty"]) for i in d10[0]["items"]] == [("치킨", 0.5)])
    page.fill("#chatDate", today_iso)

    # 단위가 다르면 수량을 고치기 전까지 넣기 막힘
    page.fill("#chatInput", "점심 피자 세 조각")
    page.press("#chatInput", "Enter")
    page.wait_for_selector("#chatApply")
    check("채팅: 단위 다름(조각 vs 라지 1판) → '단위 확인' + 넣기 막힘", "단위 확인" in page.inner_text("#chatPreview") and page.is_disabled("#chatApply"))
    page.screenshot(path=str(TMP / "chat_unit.png"), clip={"x": 0, "y": 0, "width": 1280, "height": 320})
    page.locator("#chatPreview input[aria-label='피자 수량']").fill("0.4")
    check("채팅: 수량을 고치면 넣기 가능 + kcal 표시", page.is_enabled("#chatApply") and "kcal" in page.inner_text("#chatPreview")
          and "단위 확인" not in page.inner_text("#chatPreview"))
    page.click("#chatApply")   # 수량 칸에서 바로 넣기 클릭 (예전엔 포커스가 빠지며 다시 그려져 클릭이 사라졌음)
    page.wait_for_function("document.querySelector('#status').textContent.includes('넣었습니다')")
    pz = [m for m in get(f"/api/meals?start={today_iso}&end={today_iso}") if m["slot"] == "점심"]
    check("채팅: 수량 칸에서 바로 넣기 → 피자 ×0.4 저장", [(i["name"], i["qty"]) for m in pz for i in m["items"]] == [("피자", 0.4)])
    undo(page, '피자을(를) 넣었습니다')
    page.wait_for_function("document.querySelector('#status').textContent.includes('되돌렸습니다')")
    page.fill("#chatInput", "점심 피자 세 조각")
    page.press("#chatInput", "Enter")
    page.wait_for_selector("#chatApply")
    page.locator("#chatPreview button", has_text="취소").click()
    check("채팅: 취소하면 미리보기 닫힘", page.inner_text("#chatPreview").strip() == "")

    # 끼니가 여럿인 문장 -> 로컬 LLM (가짜)
    page.fill("#chatInput", "점심엔 김밥, 저녁엔 라면 먹었어")
    page.press("#chatInput", "Enter")
    page.wait_for_selector("#chatApply")
    check("채팅: 끼니 여럿 → LLM 해석, 2끼 미리보기", "로컬 LLM으로 해석함" in page.inner_text("#chatPreview")
          and page.locator("#chatPreview .chatpv-ent").count() == 2 and "2끼 넣기" in page.inner_text("#chatApply"))
    page.screenshot(path=str(TMP / "chat_preview.png"), clip={"x": 0, "y": 0, "width": 1280, "height": 320})
    # 끼니 없는 문장 -> 끼니 고르기 전 넣기 막힘
    page.fill("#chatInput", "옥수수 반 개")
    page.press("#chatInput", "Enter")
    page.wait_for_function("document.querySelector('#chatPreview').textContent.includes('끼니를 골라')")
    check("채팅: 끼니 없으면 '끼니를 골라 주세요' + 넣기 막힘", page.is_disabled("#chatApply"))
    # 느린 LLM 해석 중에 다른 문장을 보내면 늦게 온 옛 해석은 무시
    page.fill("#chatInput", "월요일 야식 천천히 라면")
    page.press("#chatInput", "Enter")
    page.fill("#chatInput", "점심에 김밥 한 줄")
    page.press("#chatInput", "Enter")
    page.wait_for_timeout(2800)
    check("채팅 동시 요청: 늦게 온 옛 해석(라면)이 새 해석(김밥)을 덮지 않음", "김밥" in page.inner_text("#chatPreview")
          and "라면" not in page.inner_text("#chatPreview"), page.inner_text("#chatPreview").replace("\n", " | ")[:120])
    # LLM 꺼짐
    LLM.down = True
    page.fill("#chatInput", "점심엔 김밥, 저녁엔 라면 먹었어")
    page.press("#chatInput", "Enter")
    page.wait_for_function("document.querySelector('#chatPreview').textContent.includes('연결할 수 없습니다')")
    check("채팅: LLM 꺼짐 → 나눠 적으라는 안내", "나눠 적어" in page.inner_text("#chatPreview"))
    LLM.down = False
    page.keyboard.press("Escape")
    page.fill("#chatInput", "")

    # ---- 내 메뉴: 채팅 미리보기의 '옥수수 kcal 모름' -> LLM 추정 -> 저장 -> 미리보기·끼니에 kcal
    page.fill("#chatInput", "아침에 옥수수 2개")
    page.press("#chatInput", "Enter")
    page.wait_for_selector("#chatPreview .kcalset")
    page.click("#chatPreview .kcalset")
    check("kcal 정하기 상자: 제목·단위 기본값(말한 단위)", page.is_visible("#kcalBox") and page.inner_text("#kTitle") == "옥수수 kcal 정하기"
          and page.input_value("#kUnit") == "1개", page.input_value("#kUnit"))
    page.click("#kSave")
    check("kcal 정하기: 숫자 없이 저장하면 안내", "낮음·높음" in page.inner_text("#kMsg"))
    page.click("#kEstimate")
    page.wait_for_function("document.querySelector('#kBasis').textContent.includes('LLM 추정 근거')")
    page.screenshot(path=str(TMP / "kcal_box.png"), clip={"x": 0, "y": 0, "width": 1280, "height": 560})
    check("LLM 추정: 칸 채움 (80–200, 보통 130) + 근거", (page.input_value("#kLow"), page.input_value("#kHigh"), page.input_value("#kMid")) == ("80", "200", "130")
          and "150g" in page.inner_text("#kBasis"))
    page.click("#kSave")
    page.wait_for_function("document.querySelector('#status').textContent.includes('내 메뉴에 저장')")
    pv = page.inner_text("#chatPreview")
    check("저장 뒤 채팅 미리보기에 kcal (×2 = 160–400)", "160–400 kcal" in pv and "kcal 모름" not in pv, pv.replace("\n", " | ")[:160])
    mine = {m["name"]: m for m in get("/api/menus") if m["custom"]}
    check("내 메뉴 저장: 출처 LLM 추정, 근거 함께", mine.get("옥수수", {}).get("source") == "llm" and "150g" in mine["옥수수"]["basis"])
    page.click("#chatApply")
    page.wait_for_function("document.querySelector('#status').textContent.includes('넣었습니다')")
    page.wait_for_function("document.querySelector('#dayBody').textContent.includes('옥수수')")
    chip = page.locator("#dayBody .chip", has_text="옥수수")
    check("끼니에 '내 메뉴' 표시 + kcal", "내 메뉴" in chip.inner_text() and "160–400" in chip.inner_text(), chip.inner_text().replace("\n", " "))
    undo(page, '옥수수을(를) 넣었습니다')   # 끼니 넣기 되돌리기
    page.wait_for_function("document.querySelector('#status').textContent.includes('되돌렸습니다')")
    # 설정의 내 메뉴 목록 -> 지우기 -> 되돌리기
    page.click("#settingsBtn")
    check("설정: 내 메뉴 목록에 옥수수 · LLM 추정", "옥수수" in page.inner_text("#myMenus") and "LLM 추정" in page.inner_text("#myMenus"))
    page.locator("#myMenus tr", has_text="옥수수").locator("button", has_text="지우기").click()
    page.wait_for_function("document.querySelector('#status').textContent.includes('지웠습니다')")
    check("내 메뉴 지우기", not any(m["custom"] for m in get("/api/menus")) and "아직 없습니다" in page.inner_text("#myMenus"))
    undo(page, '내 메뉴에서')
    page.wait_for_function("document.querySelector('#myMenus').textContent.includes('옥수수')")
    check("내 메뉴 지우기 → 되돌리기", any(m["name"] == "옥수수" for m in get("/api/menus") if m["custom"]))
    page.click("#settingsClose")
    # 고른 날의 kcal 모름 칩에서 직접 정하기 (표에 없는 메뉴 '가지볶음특'은 10/15에 있음)
    pick_day(page, "2026-10-15")
    page.locator("#dayBody .kcalset").first.click()
    page.fill("#kUnit", "1접시"); page.fill("#kLow", "150"); page.fill("#kHigh", "250")
    page.press("#kHigh", "Enter")
    page.wait_for_function("document.querySelector('#status').textContent.includes('가지볶음특')")
    check("고른 날 칩에서 직접 정하기(Enter 저장) → 그 끼니 kcal 계산됨", "kcal 모름" not in page.inner_text("#dayBody .slot[data-slot='저녁']")
          and get("/api/menus")[-1]["custom"], page.inner_text("#dayBody .slot[data-slot='저녁']").replace("\n", " ")[:160])
    # LLM 추정 중에 상자를 닫고 다른 메뉴를 열면, 늦게 온 옛 추정은 새 상자를 덮지 않는다
    page.fill("#chatInput", "간식으로 느린떡이랑 쑥떡")
    page.press("#chatInput", "Enter")
    page.wait_for_function("document.querySelectorAll('#chatPreview .kcalset').length === 2")
    page.locator("#chatPreview .kcalset").nth(0).click()
    page.click("#kEstimate")
    page.keyboard.press("Escape")
    page.locator("#chatPreview .kcalset").nth(1).click()
    page.wait_for_timeout(2600)
    check("kcal 추정 동시 요청: 옛 추정(느린떡)이 새 상자(쑥떡)를 덮지 않음", page.inner_text("#kTitle") == "쑥떡 kcal 정하기"
          and page.input_value("#kLow") == "" and page.inner_text("#kBasis") == "", f"{page.inner_text('#kTitle')} low={page.input_value('#kLow')}")
    page.click("#kCancel")
    page.locator("#chatPreview button", has_text="취소").click()

    # ---- 영수증·캡처 올리기 (가짜 OCR: 폭 301 = 배달 영수증 20,000원 10/10, 302 = 편의점 영수증 5,000원 10/11)
    for w in (301, 302, 303):
        (TMP / f"cap{w}.png").write_bytes(img(w))
    (TMP / "note.txt").write_text("not an image")
    page.set_input_files("#capFile", [str(TMP / "cap301.png"), str(TMP / "cap302.png")])
    page.wait_for_function("document.querySelectorAll('#capArea .capcard').length === 2")
    cards = page.locator("#capArea .capcard")
    d = cards.nth(0)
    check("캡처: 2장 읽음 + 배달 영수증 카드(가게·일시·금액)", "2장을 읽었습니다" in page.inner_text("#capArea")
          and d.locator("input[aria-label='가게 이름']").input_value() == "가나다치킨 역삼점" and "20,000원" in d.inner_text()
          and "19:12" in d.inner_text())
    tsel = d.locator("select[aria-label='넣을 곳']")
    check("캡처: 금액·날짜 맞는 카드 결제(10/10 우아한형제들)가 기본 대상", "이 결제에 채우기: 우아한형제들 10/10 19:30" in tsel.locator("option").first.inner_text()
          and tsel.input_value() != "new", tsel.locator("option").first.inner_text())
    check("캡처: 옵션·배달팁·할인은 품목에서 빠짐", d.locator(".chip").count() == 2 and "배달팁" not in d.locator(".chips").inner_text())
    d.locator(".chip .nm", has_text="후라이드치킨").click()                 # 읽은 이름 -> 기준표 메뉴로 바꾸기
    page.fill("#pickInput", "치킨")
    page.keyboard.press("Enter")
    page.wait_for_function("document.querySelector('#capArea .capcard').textContent.includes('kcal')")
    check("캡처: 품목 이름을 기준표 '치킨'으로 바꾸면 kcal 표시", "치킨" in d.locator(".chips").inner_text() and "kcal" in d.locator(".chips").inner_text())
    d.locator("button", has_text="확정").click()
    page.wait_for_function("document.querySelector('#status').textContent.includes('영수증 메뉴를 채웠습니다')")
    wait_cell(page, "2026-10-10", "저녁", "ok")
    d10 = get("/api/meals?start=2026-10-10&end=2026-10-10")
    check("캡처 확정: 카드 결제 끼니에 메뉴·가게 이름", [i["name"] for i in d10[0]["items"]] == ["치킨", "콜라 1.25L"] and d10[0]["store"] == "가나다치킨 역삼점"
          and page.locator("#capArea .capcard").count() == 1)
    undo(page, '영수증 메뉴를 채웠습니다')
    page.wait_for_function("document.querySelectorAll('#capArea .capcard').length === 2")
    d10 = get("/api/meals?start=2026-10-10&end=2026-10-10")
    check("캡처 확정 → 되돌리기: 이전 메뉴로 + 캡처 다시 대기", [i["name"] for i in d10[0]["items"]] == ["치킨"] and d10[0]["store"] is None,
          f"{[(i['name'], i['qty']) for i in d10[0]['items']]} store={d10[0]['store']} status={page.inner_text('#status')[:60]}")
    # 종이 영수증: 짝 없음 -> 새 끼니 (15:03 -> 간식이 기본)
    p = page.locator("#capArea .capcard", has_text="라마바")
    check("종이 영수증: 짝 없으면 '새 끼니로' + 간식 기본 + 안내", p.locator("select[aria-label='넣을 곳']").input_value() == "new"
          and p.locator("select[aria-label='끼니']").input_value() == "간식" and "카드 결제가 없습니다" in p.inner_text())
    p.locator("button", has_text="확정").click()
    page.wait_for_function("document.querySelector('#status').textContent.includes('새 끼니를 넣었습니다')")
    d11 = get("/api/meals?start=2026-10-11&end=2026-10-11")
    check("종이 영수증 확정: 10/11 간식 새 끼니 (3품목, 가게 이름)", [(m["slot"], len(m["items"]), m["place"]) for m in d11] == [("간식", 3, "라마바 편의점 선릉점")])
    # 같은 이미지 다시 + 이미지 아닌 파일
    page.set_input_files("#capFile", [str(TMP / "cap302.png"), str(TMP / "note.txt")])
    page.wait_for_function("document.querySelector('#capArea').textContent.includes('다시 읽지 않았습니다')")
    check("캡처: 같은 이미지는 다시 읽지 않음 (이미 확정)", "이미 확정·치움" in page.inner_text("#capArea") and OCR.reads == 2)
    # 못 읽은 캡처 -> 치우기 -> 되돌리기
    page.set_input_files("#capFile", str(TMP / "cap303.png"))
    page.wait_for_function("document.querySelector('#capArea').textContent.includes('못 읽어')")
    q = page.locator("#capArea .capcard", has_text="결제금액 못 읽음")
    check("못 읽은 캡처: 안내 + 메뉴 없어 확정 막힘", q.locator("button", has_text="확정").is_disabled())
    q.locator("button", has_text="치우기").click()
    page.wait_for_function("document.querySelector('#status').textContent.includes('치웠습니다')")
    n_after = page.locator("#capArea .capcard").count()
    undo(page, '치웠습니다')
    page.wait_for_function(f"document.querySelectorAll('#capArea .capcard').length === {n_after + 1}")
    check("캡처 치우기 → 되돌리기", True)
    # OCR 꺼짐
    OCR.down = True
    (TMP / "cap304.png").write_bytes(img(304))
    page.set_input_files("#capFile", str(TMP / "cap304.png"))
    page.wait_for_function("document.querySelector('#capArea').textContent.includes('OCR 서버가 꺼져')")
    check("OCR 꺼짐 → 켜는 법 안내", "bash ../doc_reader/doc_parser/tools/ocr.sh start" in page.inner_text("#capArea"))
    OCR.down = False
    page.screenshot(path=str(TMP / "captures.png"), full_page=False)

    # ---- 영양정보 라벨 -> 내 메뉴 -> 채팅으로 "포장 식품 + 재료" 한 끼
    (TMP / "cap305.png").write_bytes(img(305))
    (TMP / "cap306.png").write_bytes(img(306))
    page.set_input_files("#capFile", str(TMP / "cap305.png"))
    page.wait_for_selector("#capArea .capcard:has-text('영양정보 라벨')")
    lab = page.locator("#capArea .capcard", has_text="영양정보 라벨")
    check("라벨 카드: 제품명·1팩(200g) = 410 kcal", lab.locator("input[aria-label='제품 이름']").input_value() == "가나다불고기"
          and "1팩(200g) = 410 kcal" in lab.inner_text(), lab.inner_text().replace("\n", " | ")[:160])
    lab.locator("button", has_text="내 메뉴로 저장").click()
    page.wait_for_function("document.querySelector('#status').textContent.includes('가나다불고기')")
    mine = {m["name"]: m for m in get("/api/menus") if m["custom"]}
    check("라벨 저장: 내 메뉴(출처 포장지 영양정보) + 카드 사라짐", mine.get("가나다불고기", {}).get("source") == "label"
          and page.locator("#capArea .capcard:has-text('영양정보 라벨')").count() == 0)
    page.fill("#chatDate", today_iso)
    page.fill("#chatInput", "저녁에 불고기 가나다랑 양파 하나, 팽이 버섯 한 묶음 같이 먹었어")
    page.press("#chatInput", "Enter")
    page.wait_for_selector("#chatApply")
    pv = page.inner_text("#chatPreview")
    check("채팅: 라벨 제품(이름 순서 달라도) + 양파 + 팽이버섯, 모두 kcal", all(x in pv for x in ("가나다불고기", "양파", "팽이버섯"))
          and "kcal 모름" not in pv and "단위 확인" not in pv, pv.replace("\n", " | ")[:200])
    page.click("#chatApply")
    page.wait_for_function("document.querySelector('#status').textContent.includes('넣었습니다')")
    meal = [m for m in get(f"/api/meals?start={today_iso}&end={today_iso}") if m["slot"] == "저녁"]
    check("채팅 넣기: 한 끼 kcal = 410 + 75 + 55 = 540", meal and meal[0]["kcal"]["mid"] == 540 and not meal[0]["kcal"]["partial"],
          str(meal[0]["kcal"] if meal else None))
    page.locator("#dayBody .chip", has_text="가나다불고기").wait_for()
    check("고른 날: 라벨 제품에 '포장지 영양정보' 표시", "포장지 영양정보" in page.locator("#dayBody .chip", has_text="가나다불고기").inner_text())

    # ---- 배민 종이 영수증 구조: 가려진 달을 요일로 바로잡음, 곁들임, 모르는 품목은 비슷한 메뉴 값으로
    page.set_input_files("#capFile", str(TMP / "cap306.png"))
    page.wait_for_selector("#capArea .capcard:has-text('가나곱창')")
    rc = page.locator("#capArea .capcard", has_text="가나곱창")
    check("종이 배달 영수증: 11/12(목) 19:42 16,000원 + 날짜 바로잡음 알림", "11/12(목) · 19:42 · 16,000원" in rc.inner_text()
          and "요일에 맞춰" in rc.inner_text(), rc.inner_text().replace("\n", " | ")[:200])
    check("곁들임 표시 (순대 추가·콜라)", rc.locator(".chip:has-text('순대 추가') .badge", has_text="곁들임").count() == 1
          and rc.locator(".chip:has-text('콜라 500ml') .badge", has_text="곁들임").count() == 1)
    rc.locator(".chip:has-text('양념곱창') .kcalset").click()
    page.wait_for_selector("#kSimilar button")
    check("kcal 정하기: 비슷한 메뉴 값(곱창) 제안", "곱창 1인분" in page.inner_text("#kSimilar"))
    page.locator("#kSimilar button", has_text="곱창").click()
    check("비슷한 메뉴 값으로 칸 채움 + 근거", page.input_value("#kLow") == "500" and "곱창 값으로" in page.inner_text("#kBasis"))
    page.click("#kSave")
    page.wait_for_function("document.querySelector('#status').textContent.includes('양념곱창')")
    page.wait_for_function("!document.querySelector(\"#capArea .capcard[data-id] .chip .kcalset\")?.closest('.chip')?.textContent.includes('양념곱창')")
    check("저장 뒤 영수증 카드의 양념곱창에 kcal", "kcal" in rc.locator(".chip", has_text="양념곱창").inner_text())
    check("짝 없음 -> 새 끼니, 19:42 -> 저녁 기본", rc.locator("select[aria-label='넣을 곳']").input_value() == "new"
          and rc.locator("select[aria-label='끼니']").input_value() == "저녁")
    rc.locator("button", has_text="확정").click()
    page.wait_for_function("document.querySelector('#status').textContent.includes('새 끼니를 넣었습니다')")
    d13 = get("/api/meals?start=2026-11-12&end=2026-11-12")
    check("배달 영수증 확정: 11/12 저녁 (양념곱창 kcal 계산, 곁들임 포함)", d13 and d13[0]["slot"] == "저녁"
          and [i["name"] for i in d13[0]["items"]][:1] == ["양념곱창"] and d13[0]["kcal"] is not None, str([(m["slot"], [i["name"] for i in m["items"]]) for m in d13]))

    # ---- A2A: 다른 에이전트가 보낸 일이 화면에 보이고, 화면에서 확정하면 작업이 완료로
    import base64
    r = rpc("SendMessage", {"message": {"role": "ROLE_USER", "messageId": "bc-1",
                                        "parts": [{"text": "오늘 점심에 김밥 한 줄"}, {"data": {"today": today_iso}}]}})["result"]["task"]
    check("A2A 먹은 것 기록: 완료 + 확인 링크(#오늘)", r["status"]["state"] == "TASK_STATE_COMPLETED"
          and r["metadata"]["reviewUrl"].endswith(f"#{today_iso}"), r["status"].get("message", {}).get("parts", [{}])[0].get("text", ""))
    page.goto(r["metadata"]["reviewUrl"])
    page.wait_for_function("document.querySelector('#dayBody').textContent.includes('김밥')")
    check("A2A로 넣은 끼니가 화면(그날 상세)에 'A2A' 메모와 함께 보임", "A2A" in page.locator("#dayBody .slot[data-slot='점심']").inner_text())
    (TMP / "cap307.png").write_bytes(img(307))
    t = rpc("SendMessage", {"message": {"role": "ROLE_USER", "parts": [
        {"raw": base64.b64encode((TMP / "cap307.png").read_bytes()).decode(), "filename": "a2a.png", "mediaType": "image/png"}]}})["result"]["task"]
    check("A2A 영수증: 확인 대기(INPUT_REQUIRED) + 캡처 링크", t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
          and "#capture=" in t["metadata"]["reviewUrl"])
    page.goto(t["metadata"]["reviewUrl"])
    card = page.locator("#capArea .capcard.focus")
    card.wait_for()
    check("A2A 캡처: 링크로 열면 그 카드 강조 + 'A2A' 표시", "사아자" in card.locator("input[aria-label='가게 이름']").input_value()
          and card.locator(".badge", has_text="A2A").count() == 1)
    card.locator("button", has_text="확정").click()
    page.wait_for_function("document.querySelector('#status').textContent.includes('새 끼니를 넣었습니다')")
    t2 = rpc("GetTask", {"id": t["id"]})["result"]
    check("화면에서 확정 → A2A 작업 완료", t2["status"]["state"] == "TASK_STATE_COMPLETED")

    # ---- kcal 출처 표시: 김밥은 식약처 DB 값, 순대국은 손으로 적은 근사값
    pick_day(page, "2026-10-13")
    chip = entry(page, "푸드코트").locator(".chip").first
    check("kcal 출처: 김밥 칩에 '식약처' + 설명", "식약처" in chip.inner_text()
          and "식약처 음식 영양성분 DB" in (chip.locator(".badge", has_text="식약처").get_attribute("title") or ""))
    pick_day(page, "2026-10-14")
    check("kcal 출처: 순대국은 '식약처' 표시 없음", "식약처" not in entry(page, "가나순대").inner_text())

    # ---- 많이 쌓였을 때: 쌓임 알림 기준을 낮춰 '이전은 모름으로 두기' 제안 -> 실행 -> 되돌리기
    page.click("#settingsBtn")
    check("설정 열림 (머리글 버튼)", page.is_visible("#settingsPanel") and page.get_attribute("#settingsBtn", "aria-expanded") == "true")
    page.fill("#warnInput", "3")
    page.fill("#sinceInput", "2026-09-01")   # 오래 안 열어서 9월부터 쌓인 상황
    page.click("#setSave")
    page.wait_for_function("document.querySelector('#setMsg').textContent.includes('저장했습니다')")
    page.wait_for_selector("#buryBtn:not([hidden])")
    before = page.inner_text("#reviewChip")
    check("쌓임 알림: '○/○ 이전은 모름으로 두기' 제안", "이전은 모름으로 두기" in page.inner_text("#buryBtn"), page.inner_text("#buryBtn"))
    page.click("#buryBtn")
    page.wait_for_function("document.querySelector('#status').textContent.includes('모름으로 두었습니다')")
    page.wait_for_function(f"document.querySelector('#reviewChip').textContent !== {json.dumps(before)}")
    after = page.inner_text("#reviewChip")
    undo(page, '모름으로 두었습니다')
    page.wait_for_function(f"document.querySelector('#reviewChip').textContent === {json.dumps(before)}")
    check("모름으로 두기 → 칩 줄어듦 → 되돌리기로 복구", before != after, f"{before} -> {after} -> 복구")
    page.fill("#warnInput", "20")
    page.fill("#sinceInput", "2026-10-07")
    page.fill("#w-저녁-0", "14:00")
    page.click("#setSave")
    page.wait_for_function("document.querySelector('#setMsg').textContent.includes('겹침')")
    check("설정: 겹치면 오류 + 고치는 법", "앞서게 고치세요" in page.inner_text("#setMsg"), page.inner_text("#setMsg"))
    page.click("#setDefault")
    page.click("#setSave")
    page.wait_for_function("document.querySelector('#setMsg').textContent.includes('저장했습니다')")
    check("설정: 기본값 저장", True)
    page.click("#settingsClose")

    # ---- 동시 요청: 9월 캘린더 응답을 늦추고 바로 10월로 -> 늦게 온 9월 응답이 화면을 덮지 않아야
    SLOW.add("/api/calendar?month=2026-09")
    page.click("#prevMonth")
    page.click("#nextMonth")
    page.wait_for_timeout(2200)
    check("동시 요청: 늦게 온 옛 달(9월) 응답 무시", page.inner_text("#monthLabel") == "2026년 10월"
          and page.locator("#cal .day[data-date^='2026-10']").count() == 31
          and page.locator("#cal .day[data-date^='2026-09']").count() == 0)
    SLOW.clear()
    SLOW.add("/api/meals?start=2026-10-02")
    page.locator("#cal .day[data-date='2026-10-02']").click()
    page.locator("#cal .day[data-date='2026-10-03']").click()
    page.wait_for_timeout(2200)
    check("동시 요청: 날짜 상세도 마지막에 누른 날(10/3)", page.inner_text("#dayTitle").startswith("10/3 ")
          and "가나마트" in page.inner_text("#dayBody") and "카타국밥" not in page.inner_text("#dayBody"))
    SLOW.clear()
    check("장보기: 메뉴·인원 없이 '먹은 끼니 아님'", "먹은 끼니 아님" in page.inner_text("#dayBody")
          and entry(page, "가나마트").locator(".chips").count() == 0)

    # ---- 주별 추이 + 주로 이동
    wk = get("/api/weekly?weeks=12")["weeks"]
    check("주별 추이: API와 같은 주 수, 가장 오래된 주 = 첫 기록 주(9/28)",
          page.locator("#weekRows tr").count() == len(wk) and wk[0]["week_start"] == "2026-09-28"
          and page.locator("#weekRows tr").last.inner_text().startswith("2026-09-28"),
          f"{page.locator('#weekRows tr').count()}행 · " + page.inner_text("#weekRows").replace("\n", " | ")[:200])
    page.locator("#weekRows button", has_text="2026-09-28").click()
    page.wait_for_function("document.querySelector('#monthLabel').textContent === '2026년 9월'")
    page.wait_for_function("document.querySelector('#weekLine').textContent.includes('9/28–10/4 주')")
    check("주별 추이에서 주 누르면 그 주 월요일로", page.inner_text("#dayTitle").startswith("9/28 "))



    # ---- 배달앱 주문상세 캡처 (날짜·결제금액 없음) -> 메뉴금액+배달팁으로 찾은 같은 금액 배달 결제를 골라 넣기
    page.set_input_files("#capFile", files=[{"name": "detail.png", "mimeType": "image/png", "buffer": img(308)}])
    det = page.locator("#capArea .capcard", has_text="가나분식")
    det.wait_for()
    opts = det.locator("select[aria-label='넣을 곳'] option").all_inner_texts()
    check("주문상세 캡처: '메뉴 19,000 + 배달팁 1,000 = 20,000원으로 찾음' + 날짜 없음 안내",
          "= 20,000원으로 찾음" in det.inner_text() and "날짜를 못 읽어" in det.inner_text(), det.inner_text()[:200].replace("\n", " "))
    check("주문상세 캡처: 후보 = 같은 금액 배달 결제 (메뉴 모르는 최근 것 먼저)", len(opts) >= 3 and all("20,000원" in o for o in opts[:-1])
          and opts[-1] == "새 끼니로 넣기" and "(지금 메뉴" not in opts[0], " | ".join(opts))
    check("주문상세 캡처: 곁들임(김말이튀김) 칩 + 순대 ×2", "김말이튀김" in det.inner_text() and "곁들임" in det.inner_text()
          and det.locator("input[aria-label='순대 수량']").input_value() == "2")
    # 구성 계산: "치즈 떡볶이(2인분)" -> 칩에 "구성 추정 약 … · 확인" -> 상자에 줄별 근거 -> 채우기 -> 저장(출처 '구성 계산')
    calc = det.locator(".chip", has_text="치즈 떡볶이").locator(".kcalset")
    check("캡처 칩: 기준표에 없는 메뉴는 '구성 추정 약 … kcal · 확인'", "구성 추정 약" in calc.inner_text(), calc.inner_text())
    calc.click()
    page.wait_for_selector("#kCompose .parts")
    box = page.inner_text("#kCompose")
    check("kcal 상자: '구성으로 추정' + 줄별 근거(떡볶이 기준표 · 치즈 추가) + 2인분",
          "구성으로 추정" in box and "떡볶이" in box and "치즈 추가" in box and "2인분" in box, box.replace("\n", " ")[:160])
    page.locator("#kcalBox").screenshot(path=str(TMP / "compose_box.png"))
    page.click("#kCompose .usecalc")
    check("[이 값으로 채우기] → 단위·kcal 칸이 채워짐", page.input_value("#kUnit") == "2인분" and page.input_value("#kLow") != "")
    page.click("#kSave")
    page.wait_for_function("!document.querySelector('#kcalBox').classList.contains('open')")
    mine = next(m for m in get("/api/menus") if m["name"] == "치즈 떡볶이(2인분)")
    check("저장 → 내 메뉴 출처 '구성 계산' + 근거 한 줄", mine["source"] == "calc" and mine["basis"].startswith("구성 계산:"), mine["basis"][:80])
    page.wait_for_function("document.querySelector('#capArea').textContent.includes('치즈 떡볶이') && "
                           "![...document.querySelectorAll('#capArea .chip')].some(e => e.textContent.includes('치즈 떡볶이') && e.textContent.includes('구성 추정'))")
    det.locator("button", has_text="치우기").click()
    page.wait_for_function("![...document.querySelectorAll('#capArea .capcard')].some(e => e.textContent.includes('가나분식'))")

    # ---- 경험 기억: 앞에서 사람이 넣은 메뉴(10/7 치킨, 10/8 돼지국밥, 10/10 치킨 ×0.5) -> 같은 앱·금액(20,000원) 끼니에 제안
    pick_day(page, "2026-10-06")
    sg = entry(page, "우아한형제들").locator(".sugg")
    texts = sg.all_inner_texts()
    check("경험 기억: 제안 최대 두 줄, 최근(10/10 치킨 ×0.5) 먼저 + '3번 중 1번'",
          len(texts) == 2 and "치킨 ×0.5" in texts[0] and "마지막 10/10" in texts[0] and "3번 중 1번" in texts[0], " | ".join(texts))
    sg.first.locator("button", has_text="틀림").click()
    page.wait_for_function("document.querySelector('#status').textContent.includes('제안을 띄우지 않습니다')")
    page.wait_for_function("![...document.querySelectorAll('#dayBody .sugg')].some(e => e.textContent.includes('치킨 ×0.5'))")
    texts = entry(page, "우아한형제들").locator(".sugg").all_inner_texts()
    check("[틀림] → 그 제안만 빠지고 다음 후보가 올라옴, 다른 날(10/5)엔 그대로", len(texts) == 2 and "돼지국밥" in texts[0]
          and any("치킨×0.5" == s_["signature"] for m in get("/api/meals?start=2026-10-05&end=2026-10-05") for s_ in m.get("suggest", [])),
          " | ".join(texts))
    undo(page, "제안을 띄우지 않습니다")
    page.wait_for_function("document.querySelector('#dayBody .sugg')?.textContent.includes('치킨 ×0.5')")
    check("[틀림] 되돌리기 → 그 제안 다시 첫 줄", True)
    entry(page, "우아한형제들").locator(".sugg").first.locator("button", has_text="넣기").click()
    wait_cell(page, "2026-10-06", "저녁", "ok")
    d6 = get("/api/meals?start=2026-10-06&end=2026-10-06")[0]
    check("[넣기] → 메뉴 채워짐 + 캘린더 반영 + 제안 줄 사라짐", [(i["name"], i["qty"]) for i in d6["items"]] == [("치킨", 0.5)]
          and page.locator("#dayBody .sugg").count() == 0)
    undo(page, "지난번 메뉴로 채웠습니다")
    wait_cell(page, "2026-10-06", "저녁", "review")
    check("[넣기] 되돌리기 → 다시 메뉴 모름 + 제안", page.locator("#dayBody .sugg").count() == 2
          and get("/api/meals?start=2026-10-06&end=2026-10-06")[0]["items"] == [])
    first = get("/api/review")["rows"][0]
    page.click("#reviewChip")
    page.wait_for_function(f"document.querySelector('#dayBody').dataset.day === '{first['date']}'")
    page.wait_for_timeout(300)
    focus = page.evaluate("document.activeElement?.className || ''")
    check("확인 흐름: 제안 있는 끼니는 검색창 대신 [넣기]에 포커스 (없으면 검색창)",
          ("take" in focus and not page.is_visible("#picker")) if first.get("suggest") else page.is_visible("#picker"),
          f"{first['date']} 제안 {len(first.get('suggest') or [])} · 포커스 {focus}")
    page.keyboard.press("Escape")
    page.set_viewport_size({"width": 390, "height": 844})
    pick_day(page, "2026-10-05")
    page.locator("#dayBody .sugg").first.scroll_into_view_if_needed()
    page.screenshot(path=str(TMP / "memory_390.png"), full_page=False)
    check("390폭 제안 줄: 가로 스크롤 없음", page.evaluate("document.documentElement.scrollWidth <= innerWidth"), str(page.evaluate(OVERFLOW_JS)))
    page.set_viewport_size({"width": 1280, "height": 900})
    pick_day(page, "2026-10-05")
    page.locator("#dayPanel").screenshot(path=str(TMP / "memory_1280.png"))

    # ---- 대화 도우미 (세션 2종): 탭 · 새 대화 · 로컬 LLM 기록 → 캘린더 반영 · Claude 이어서 · 멈추기 · 꺼짐 · 지우기
    page.on("dialog", lambda d: d.accept())
    pick_day(page, "2026-10-13")
    page.click("#tabConv")
    check("대화 탭: 처음엔 빈 안내 + 입력 잠김", page.is_visible("#convView") and not page.is_visible("#dayView")
          and "새 대화 시작" in page.inner_text("#msgs") and page.is_disabled("#askInput"))
    page.click("#convNewBtn")
    ch = page.locator("#convChoices .choice")
    check("새 대화: 기본 로컬 LLM 선택, Claude는 나가는 범위 안내",
          ch.nth(0).get_attribute("aria-checked") == "true" and "Anthropic" in ch.nth(1).inner_text()
          and "결제 금액·가게·카드 정보는 보내지 않습니다" in ch.nth(1).inner_text(), ch.nth(1).inner_text())
    page.click("#convCreate")
    page.wait_for_selector("#convHead .badge")
    check("로컬 대화: 머리에 '로컬 LLM' + 'PC 안에서만'", "로컬 LLM" in page.inner_text("#convHead") and "PC 안에서만" in page.inner_text("#convHead"))
    page.fill("#askInput", "아침에 고구마 1개 먹었어")
    page.dispatch_event("#askInput", "keydown", {"key": "Enter", "isComposing": True})
    page.wait_for_timeout(300)
    check("대화: 한글 조합 중 Enter는 보내지 않음", page.locator("#msgs .msg-u").count() == 0)
    page.press("#askInput", "Enter")
    page.wait_for_function("document.querySelector('#msgs .msg-a .foot, #msgs .msg-a')?.textContent.includes('넣었습니다')")
    page.wait_for_selector("#askGo:not([hidden])")
    card = page.inner_text("#msgs .toolcard")
    check("로컬 대화: 도구 카드 '기록함: 10/13 아침 고구마' + 답", card.startswith("기록함: 10/13 아침 고구마"), card)
    wait_cell(page, "2026-10-13", "아침", "ok")
    check("도우미가 기록한 끼니가 캘린더에 반영 + 그날 강조", "고구마" in cell(page, "2026-10-13", "아침")[1]
          and "touched" in page.get_attribute("#cal .day[data-date='2026-10-13']", "class"))
    page.locator("#msgs .toolcard button", has_text="10/13 보기").click()
    page.wait_for_function("!document.querySelector('#dayView').hidden")
    check("도구 카드 [보기] → 고른 날 탭의 그날 (고구마, 메모 '대화')", page.inner_text("#dayTitle").startswith("10/13 ")
          and "고구마" in page.inner_text("#dayBody"))
    page.click("#tabConv")
    page.reload()
    page.wait_for_function("document.querySelectorAll('#msgs .msg-u').length === 1")
    check("새로고침: 대화 탭·대화 내용 유지 (서버 저장)", page.is_visible("#convView") and "고구마" in page.inner_text("#msgs")
          and "기록함" in page.inner_text("#msgs .toolcard"))
    # 로컬 LLM이 꺼진 뒤: 보내면 오류 카드 + 켜는 법, 새 대화에선 잠김
    LLM.down = True
    page.fill("#askInput", "고구마 하나 더")
    page.click("#askGo")
    page.wait_for_selector("#msgs .msg-a.fail")
    check("로컬 LLM 꺼짐: 오류 카드에 켜는 법", "vLLM" in page.locator("#msgs .msg-a.fail").last.inner_text(),
          page.locator("#msgs .msg-a.fail").last.inner_text()[:120])
    page.click("#convNewBtn")
    page.wait_for_function("document.querySelector('#convChoices .choice')?.disabled")
    check("새 대화: 꺼진 로컬 LLM은 잠금 + 이유, Claude가 선택됨", "쓸 수 없음" in ch.nth(0).inner_text()
          and ch.nth(1).get_attribute("aria-checked") == "true")
    LLM.down = False
    page.click("#convCreate")
    page.wait_for_function("document.querySelector('#convHead').textContent.includes('Claude Code')")
    mid = next(m["id"] for m in get("/api/meals?start=2026-10-12&end=2026-10-12") if m["slot"] == "저녁")
    CLAUDE.script = [[("tool", "query_meals", {"start": "2026-10-12", "end": "2026-10-12"}),
                      ("tool", "update_meal", {"meal_id": mid, "items": [{"name": "치킨", "qty": 1}], "people": 2}),
                      ("text", "10/12(월) 저녁을 치킨, 2명으로 고쳤습니다.")],
                     [("text", "네, 그렇게 두겠습니다.")]]
    page.fill("#askInput", "10/12 배달은 치킨이었고 둘이 먹었어")
    page.click("#askGo")
    page.wait_for_function("document.querySelector('#msgs .msg-a .foot')?.textContent.includes('$')")
    txt = page.inner_text("#msgs")
    check("Claude 대화: 조회·고침 카드 + 비용 표시", "식단 조회" in txt and "고침: 10/12 저녁 치킨 (2명)" in txt and "$0.070" in txt, txt[-200:])
    wait_cell(page, "2026-10-12", "저녁", "ok")
    check("Claude가 고친 끼니도 캘린더에 반영", "치킨" in cell(page, "2026-10-12", "저녁")[1])
    page.fill("#askInput", "고마워")
    page.click("#askGo")
    page.wait_for_function("[...document.querySelectorAll('#msgs .msg-a')].pop().textContent.includes('그렇게 두겠습니다')")
    check("Claude 대화: 둘째 말은 같은 세션 이어서 (--resume)", "--resume" in CLAUDE.cmds[-1]
          and CLAUDE.cmds[-1][CLAUDE.cmds[-1].index("--resume") + 1] == CLAUDE.cmds[-2][CLAUDE.cmds[-2].index("--session-id") + 1])
    # 멈추기: 오래 걸리는 답을 멈추면 '멈췄습니다', 잠시 뒤 다시 보낼 수 있음
    CLAUDE.script = [[("wait", 2)]]
    page.fill("#askInput", "천천히 답해줘")
    page.click("#askGo")
    page.wait_for_selector("#askStop:not([hidden])")
    check("답하는 중: 보내기 대신 멈추기, 대화 바꾸기 잠김", page.is_hidden("#askGo") and page.is_disabled("#convSel"))
    page.click("#askStop")
    page.wait_for_selector("#askGo:not([hidden])")
    check("멈추기 → '멈췄습니다'", "멈췄습니다" in page.locator("#msgs .msg-a").last.inner_text())
    page.wait_for_timeout(2500)   # 서버 쪽 실행이 끝나 잠금이 풀릴 때까지
    CLAUDE.script = [[("text", "다시 답합니다.")]]
    page.fill("#askInput", "다시")
    page.click("#askGo")
    page.wait_for_function("[...document.querySelectorAll('#msgs .msg-a')].pop().textContent.includes('다시 답합니다')")
    check("멈춘 뒤 다시 보내기 됨", True)
    opts = page.locator("#convSel option").all_inner_texts()
    check("대화 목록: 두 대화, 세션 이름 표시", len(opts) == 2 and any("Claude Code" in o for o in opts) and any("로컬 LLM" in o for o in opts), str(opts))
    page.screenshot(path=str(TMP / "chat_1280.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(300)
    page.locator("#convView").scroll_into_view_if_needed()
    page.screenshot(path=str(TMP / "chat_390.png"), full_page=True)
    check("390폭 대화: 가로 스크롤 없음", page.evaluate("document.documentElement.scrollWidth <= innerWidth"), str(page.evaluate(OVERFLOW_JS)))
    page.set_viewport_size({"width": 1280, "height": 900})
    page.click("#convDel")
    page.wait_for_function("document.querySelectorAll('#convSel option').length === 1")
    check("대화 지우기 → 남은 대화(로컬)로, 식단 기록은 그대로", "로컬 LLM" in page.inner_text("#convHead")
          and "치킨" in cell(page, "2026-10-12", "저녁")[1] and len(get("/api/conversations")) == 1)
    check("대화 화면에 null/undefined/NaN 없음", clean(page))
    page.click("#tabDay")

    # ---- 새로고침: 고른 날 유지 (주소의 #날짜)
    pick_day(page, "2026-09-30")
    page.reload()
    page.wait_for_function("document.querySelector('#dayBody').textContent.includes('다라돼지국밥')")
    check("새로고침해도 고른 날(9/30) 유지", page.inner_text("#dayTitle").startswith("9/30 "))
    check("화면에 null/undefined/NaN 없음", clean(page))
    page.goto(URL + "#2026-10-01")
    page.wait_for_function("document.querySelector('#dayBody').textContent.includes('쿠팡이츠')")
    check("자정 넘은 배달: 10/1 야식 + '결제는 10/2 새벽'", "결제는 10/2 새벽" in page.inner_text("#dayBody"))
    check("하루 합계: '하루 최소 약 … kcal + ? (… 모르는 끼니 n개)'", "하루 최소 약" in page.inner_text("#dayTotal")
          and "+ ?" in page.inner_text("#dayTotal") and "모르는 끼니 2개" in page.inner_text("#dayTotal"), page.inner_text("#dayTotal"))
    check("주 요약: 평균은 '다 아는 n일 기준'", "다 아는" in page.inner_text("#weekLine"), page.inner_text("#weekLine"))
    page.wait_for_timeout(300)
    page.screenshot(path=str(TMP / "main_1280.png"), full_page=True)

    # ---- 폭 3종 + 가로 스크롤
    for w, h in ((390, 844), (768, 1024)):
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(300)
        page.screenshot(path=str(TMP / f"main_{w}.png"), full_page=True)
        check(f"{w}폭: 페이지 가로 스크롤 없음", page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
              str(page.evaluate(OVERFLOW_JS)))
    page.set_viewport_size({"width": 1280, "height": 900})
    check("JS 오류 없음", not errors, "; ".join(errors))

    # ---- 다크 모드
    ctx = b.new_context(color_scheme="dark", viewport={"width": 1280, "height": 900})
    dp = ctx.new_page()
    dp.goto(URL + "#2026-10-12")
    dp.wait_for_function("document.querySelector('#dayBody').textContent.includes('48,000원')")
    dp.wait_for_timeout(300)
    dp.screenshot(path=str(TMP / "main_dark.png"), full_page=True)
    b.close()
server.should_exit = True
print(f"{sum(results)}/{len(results)} PASS  (캡처: {TMP})")
sys.exit(0 if all(results) else 1)
