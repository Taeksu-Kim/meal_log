"""대화 도우미 로컬 LLM 세션 점검 (실제 로컬 vLLM). 결제·대화는 전부 지어낸 것, 임시 DB -- 실제 data/는 건드리지 않는다.

    python tools/assistant_eval.py [--llm-url http://127.0.0.1:8002/v1]

화면과 같은 길(서비스 send_message → LocalBackend → 도구)로 여러 턴 대화를 돌리고, 결과를 DB 상태로 판정한다
(답 문장이 그럴듯한지가 아니라 기록이 맞게 바뀌었는지). 프롬프트 토큰은 vLLM /tokenize로 잰다 (KV 캐시 한도 확인).
결과는 경향일 뿐 (1회). 시나리오를 보며 지침·도구를 고쳤으면 새 시나리오로 다시 잰다 (과적합).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from meal_log.service import MealService  # noqa: E402
from tests.fake_card import FakeCard, row  # noqa: E402

TODAY = date(2026, 11, 15)   # 일요일
ROWS = [row("2026-11-14", "19:30", "우아한형제들", 21000, "배달"),      # 어제 저녁 배달 (메뉴 모름)
        row("2026-11-13", "12:10", "가나김밥", 4500),
        row("2026-11-16", "19:20", "우아한형제들", 21000, "배달")]     # 같은 금액 배달 (경험 기억 제안 대상)                   # 그제 점심 (기준표 규칙으로 김밥)


class Recorder:
    """LLM 호출마다 프롬프트 토큰(vLLM /tokenize)과 걸린 시간을 기록."""

    def __init__(self, llm, base: str) -> None:
        self.llm, self.base, self.calls = llm, base.rstrip("/").removesuffix("/v1"), []

    def tokens(self, text: str) -> int:
        req = urllib.request.Request(f"{self.base}/tokenize", data=json.dumps({"prompt": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=10))["count"]

    def complete(self, system, user, *, schema=None):
        t0 = time.time()
        try:
            return self.llm.complete(system, user, schema=schema)
        finally:
            self.calls.append({"tokens": self.tokens(system + "\n" + user), "sec": time.time() - t0})


def meals(svc, d):
    return [m for m in svc.meals(date.fromisoformat(d), date.fromisoformat(d)) if not m["excluded"]]


def items(svc, d, slot):
    return [(i["name"], i["qty"]) for m in meals(svc, d) if m["slot"] == slot for i in m["items"]]


# (이름, [사용자 말...], 판정(svc, 이벤트 목록들, 답 목록) -> (맞음, 설명)). 같은 DB에 차례로 쌓인다.
def s_log(svc, evs, ans):
    got = items(svc, "2026-11-15", "아침")
    return got == [("고구마", 1.0)], f"11/15 아침 {got}"


def s_fill(svc, evs, ans):
    ms = [m for m in meals(svc, "2026-11-14") if m["slot"] == "저녁"]
    got = [(i["name"], i["qty"]) for m in ms for i in m["items"]]
    return len(ms) == 1 and ms[0]["place"] == "우아한형제들" and got == [("치킨", 0.5)], f"11/14 저녁 {len(ms)}끼 {got}"


def s_ask_slot(svc, evs, ans):
    first_saved = any(e["type"] == "tool_result" and e["summary"].startswith("기록함") for e in evs[0])
    asked = "끼니" in ans[0] or "언제" in ans[0] or "?" in ans[0]
    got = items(svc, "2026-11-15", "간식")
    return (not first_saved and asked and got == [("바나나", 2.0)],
            f"첫 턴 저장 {first_saved}, 되물음 {asked}, 11/15 간식 {got}")


def s_query(svc, evs, ans):
    used = any(e["type"] == "tool" and e["name"] == "query_meals" for e in evs[0])
    return used and "치킨" in ans[0], f"query_meals {used}, 답에 치킨 {'치킨' in ans[0]}"


def s_update(svc, evs, ans):
    ms = [m for m in meals(svc, "2026-11-14") if m["slot"] == "저녁"]
    got = [(i["name"], i["qty"]) for m in ms for i in m["items"]]
    return len(ms) == 1 and ms[0]["people"] == 2 and got == [("치킨", 0.5)], f"인원 {[m['people'] for m in ms]}, 메뉴 {got}"


def s_offtopic(svc, evs, ans):
    tools = [e["name"] for e in evs[0] if e["type"] == "tool"]
    return not any(t in ("log_meal", "update_meal", "save_menu") for t in tools) and "def " not in ans[0], f"도구 {tools}"


def s_privacy(svc, evs, ans):
    leak = any(x in ans[0] for x in ("21000", "21,000", "2만"))
    return not leak, f"금액 노출 {leak}: {ans[0][:60]}"


def s_weekly(svc, evs, ans):
    used = any(e["type"] == "tool" and e["name"] in ("weekly", "query_meals") for e in evs[0])
    return used and "kcal" in ans[0].lower(), f"도구 {used}"


def s_h_replace(svc, evs, ans):
    got = items(svc, "2026-11-13", "점심")
    n = len([m for m in meals(svc, "2026-11-13") if m["slot"] == "점심"])
    return got == [("라면", 1.0)] and n == 1, f"11/13 점심 {n}끼 {got}"


def s_h_night(svc, evs, ans):
    got = items(svc, "2026-11-14", "야식")
    return got == [("컵라면", 1.0)], f"11/14 야식 {got}"


def s_h_menu(svc, evs, ans):
    used = any(e["type"] == "tool" and e["name"] == "menu_info" for e in evs[0])
    writes = any(e["type"] == "tool" and e["name"] in ("log_meal", "update_meal", "save_menu") for e in evs[0])
    return used and not writes and "kcal" in ans[0].lower(), f"menu_info {used}, 쓰기 {writes}"


def s_memory(svc, evs, ans):
    first_write = any(e["type"] == "tool" and e["name"] in ("update_meal", "log_meal") for e in evs[0])
    used = any(e["type"] == "tool" and e["name"] == "pending_meals" for e in evs[0])
    got = items(svc, "2026-11-16", "저녁")
    return (used and not first_write and "치킨" in ans[0] and got == [("치킨", 0.5)],
            f"pending_meals {used}, 묻기 전 저장 {first_write}, 11/16 저녁 {got}")


SCENARIOS = [
    ("기록: 오늘 아침 고구마", ["오늘 아침에 고구마 1개 먹었어"], s_log),
    ("메뉴 모르는 카드 결제 채우기", ["어제 저녁 배달은 치킨 반 마리였어"], s_fill),
    ("끼니 빠짐 → 되묻고 저장", ["바나나 두 개 먹었어", "간식이었어"], s_ask_slot),
    ("조회: 어제 뭐 먹었지", ["어제 뭐 먹었는지 알려줘"], s_query),
    ("고치기: 인원 2명", ["어제 저녁 치킨은 둘이 나눠 먹은 거야"], s_update),
    ("식단 밖 요청 거절", ["파이썬으로 피보나치 코드 짜줘"], s_offtopic),
    ("결제 금액은 모름", ["어제 배달 얼마 냈어?"], s_privacy),
    ("주간 kcal", ["이번 주 하루 평균 몇 kcal 먹었어?"], s_weekly),
    # 처음 보는 시나리오 (2026-11-15, 지침 문구를 고친 뒤 추가 -- 위 8개는 개발 중 본 것)
    ("홀드아웃: 그제 점심 메뉴 바꾸기", ["그제 점심은 김밥 말고 라면이었어"], s_h_replace),
    ("홀드아웃: 어제 밤 야식", ["어제 밤 11시쯤 컵라면 하나 먹었어"], s_h_night),
    ("홀드아웃: 메뉴 kcal 묻기", ["떡볶이 1인분은 몇 kcal야?"], s_h_menu),
    # 경험 기억 (2026-11-15 추가): 11/14 저녁 치킨이 확정돼 있으니 같은 금액인 11/16 배달에 제안이 붙는다
    ("경험 기억: 제안은 확인받고 넣기", ["메뉴 모르는 끼니 정리 좀 도와줘", "응 맞아 그거야"], s_memory),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm-url", default="http://127.0.0.1:8002/v1")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="meal_assist_"))
    svc = MealService(tmp / "m.db", source=FakeCard(list(ROWS)).source, llm_url=a.llm_url)
    svc.sync()
    rec = Recorder(svc._get_llm(), a.llm_url)
    svc._llm = rec
    ok_n, turn_secs = 0, []
    for name, turns, judge in SCENARIOS:
        conv = svc.create_conversation("local")
        evs, ans = [], []
        for text in turns:
            t0 = time.time()
            ev = list(svc.send_message(conv["id"], text, today=TODAY))
            turn_secs.append(time.time() - t0)
            evs.append(ev)
            ans.append("".join(e["text"] for e in ev if e["type"] == "text"))
        ok, why = judge(svc, evs, ans)
        ok_n += ok
        errs = [e["message"][:80] for t in evs for e in t if e["type"] == "error"]
        steps = [" → ".join(e["name"] for e in t if e["type"] == "tool") or "-" for t in evs]
        print(f"{'PASS' if ok else 'FAIL'} {name} -- {why}")
        print(f"     도구: {' | '.join(steps)}" + (f"  오류: {errs}" if errs else ""))
        for u, x in zip(turns, ans):
            print(f"     사용자: {u}\n     도우미: {x[:160]}")
    tk = [c["tokens"] for c in rec.calls]
    print(f"\n{ok_n}/{len(SCENARIOS)} PASS · LLM 호출 {len(rec.calls)}회 · 프롬프트 토큰 최대 {max(tk)} 평균 {sum(tk) // len(tk)}"
          f" · 턴당 {sum(turn_secs) / len(turn_secs):.1f}초 (최대 {max(turn_secs):.1f})")
    return 0 if ok_n == len(SCENARIOS) else 1


if __name__ == "__main__":
    sys.exit(main())
