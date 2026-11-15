"""채팅 입력 해석 정확도 (실제 로컬 vLLM). 문장·정답은 지어낸 것 -- 실제 식단 기록을 쓰지 않는다.

    python tools/chat_eval.py [--llm-url http://127.0.0.1:8002/v1]

정답을 먼저 적고 잰다 (CLAUDE.md). 맞음 = 끼니 수·날짜·끼니·메뉴 이름·수량이 모두 같음. 결과는 경향일 뿐 (temperature 0, 1회).
문장을 바꿔 가며 프롬프트를 고친 뒤 같은 문장으로 다시 재면 과적합 -- 고쳤으면 새 문장(HOLDOUT)으로 확인한다.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from meal_log import chat  # noqa: E402
from meal_log.llm import LLMBadOutput, LLMDown, make_local_llm  # noqa: E402

TODAY = date(2026, 11, 15)   # 일요일
# (문장, [(날짜, 끼니 또는 None, [(메뉴, 수량)])])
CASES = [
    ("오늘 아침에 고구마 1개 먹었어", [("2026-11-15", "아침", [("고구마", 1)])]),
    ("어제 저녁 김밥 두 줄이랑 라면", [("2026-11-14", "저녁", [("김밥", 2), ("라면", 1)])]),
    ("점심엔 김밥, 저녁엔 라면 먹었어", [("2026-11-15", "점심", [("김밥", 1)]), ("2026-11-15", "저녁", [("라면", 1)])]),
    ("어제 점심 쿠팡이츠는 마라탕이었어", [("2026-11-14", "점심", [("마라탕", 1)])]),
    ("아점으로 샌드위치랑 아아 한 잔", [("2026-11-15", "점심", [("샌드위치", 1), ("아메리카노", 1)])]),
    ("월요일 저녁에 치킨 반 마리랑 맥주 두 잔", [("2026-11-09", "저녁", [("치킨", 0.5), ("맥주", 2)])]),
    ("금요일 점심 부대찌개", [("2026-11-13", "점심", [("부대찌개", 1)])]),
    ("그제 야식으로 떡볶이", [("2026-11-13", "야식", [("떡볶이", 1)])]),
    ("간식으로 바나나 하나", [("2026-11-15", "간식", [("바나나", 1)])]),
    ("오늘 저녁 집에서 삼겹살 구워 먹음", [("2026-11-15", "저녁", [("삼겹살", 1)])]),
    ("아침은 굶고 점심에 비빔밥", [("2026-11-15", "점심", [("비빔밥", 1)])]),
    ("어제 밤에 컵라면 하나랑 삼각김밥 두 개", [("2026-11-14", "야식", [("컵라면", 1), ("삼각김밥", 2)])]),
    ("점심 돈가스 정식", [("2026-11-15", "점심", [("돈까스", 1)])]),
    ("저녁에 짜장 곱빼기", [("2026-11-15", "저녁", [("짜장면", 1)])]),
    ("11월 12일 점심 칼국수", [("2026-11-12", "점심", [("칼국수", 1)])]),
]


def norm(entries) -> list:
    return sorted((e.date.isoformat(), e.slot, sorted((i.name, round(i.qty, 2)) for i in e.items)) for e in entries)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm-url", default="http://127.0.0.1:8002/v1")
    args = ap.parse_args()
    llm = make_local_llm(args.llm_url)
    ok = 0
    by_via = {"rules": [0, 0], "llm": [0, 0]}
    secs = []
    for text, want in CASES:
        t0 = time.monotonic()
        try:
            got = chat.parse(text, TODAY, TODAY, lambda: llm)
            res, via = norm(got.entries), got.via
        except (LLMDown, LLMBadOutput) as exc:
            res, via = f"오류: {type(exc).__name__}", "llm"
        dt = time.monotonic() - t0
        exp = sorted((d, s, sorted((n, round(q, 2)) for n, q in items)) for d, s, items in want)
        hit = res == exp
        ok += hit
        by_via[via][0] += hit
        by_via[via][1] += 1
        if via == "llm":
            secs.append(dt)
        print(("맞음 " if hit else "틀림 ") + f"[{via} {dt:.1f}s] {text}" + ("" if hit else f"\n      기대 {exp}\n      결과 {res}"))
    print(f"\n{ok}/{len(CASES)} 맞음 · 규칙 {by_via['rules'][0]}/{by_via['rules'][1]} · LLM {by_via['llm'][0]}/{by_via['llm'][1]}"
          + (f" · LLM 평균 {sum(secs) / len(secs):.1f}초" if secs else ""))


if __name__ == "__main__":
    main()
