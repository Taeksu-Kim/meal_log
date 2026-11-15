"""경험 기억 제안이 맞는지 -- 시간순으로 다시 돌려 본다 (실제 data/meals.db를 읽기만, 집계 숫자만 출력).

    python tools/memory_eval.py [--db data/meals.db]

정답 = 사람이 메뉴를 확정한 카드 끼니 (memory.learned). 각 정답 끼니를 날짜순으로 돌며 **그보다 앞선** 정답만으로 제안을 만들고,
첫 제안의 메뉴 묶음이 실제와 같은지 본다. 지표: 제안이 나온 비율(덮음), 나온 것 중 첫 제안이 맞은 비율, 두 제안 안에 든 비율,
틀린 제안 수. 제안만 하는 기능이라 "틀린 제안"은 사람이 [틀림]을 한 번 누르는 비용. 경향일 뿐 (정답이 적으면 특히).
행 단위(가게·금액·메뉴)는 출력하지 않는다 (CLAUDE.md 개인정보 규칙).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from meal_log import analysis, memory  # noqa: E402
from meal_log.store import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "meals.db"))
    a = ap.parse_args()
    store = Store(a.db)
    truth = sorted(memory.learned(analysis.meals_between(store, None, None)), key=lambda m: (m["date"], m["time"] or "", m["id"]))
    print(f"정답(사람이 메뉴를 확정한 카드 끼니) {len(truth)}개 · 배달 {sum(m['category'] == '배달' for m in truth)}")
    if len(truth) < 2:
        print("정답이 2개 미만이라 잴 수 없음 -- 영수증·채팅·대화로 카드 끼니 메뉴를 확정하면 쌓인다")
        return 0
    stat = Counter()
    for i, m in enumerate(truth):
        mem = memory.Memory(truth[:i])                      # 앞선 정답만
        target = {**m, "items": [], "menu_user": 0}         # 메뉴를 모르는 상태로 되돌려 묻는다
        sug = mem.suggest(target)
        actual = memory.signature((x["name"], x["qty"]) for x in m["items"])
        kind = "배달" if m["category"] == "배달" else "그 밖"
        stat[kind, "n"] += 1
        if not sug:
            continue
        stat[kind, "covered"] += 1
        stat[kind, "top1"] += sug[0].signature == actual
        stat[kind, "top2"] += any(s.signature == actual for s in sug)
        stat[kind, "wrong_first"] += sug[0].signature != actual
    for kind in ("배달", "그 밖"):
        n, cov = stat[kind, "n"], stat[kind, "covered"]
        if not n:
            continue
        print(f"{kind}: {n}끼 · 제안 나옴 {cov} ({cov / n:.0%})" + (
            f" · 첫 제안 맞음 {stat[kind, 'top1']}/{cov} ({stat[kind, 'top1'] / cov:.0%}) · 두 제안 안 {stat[kind, 'top2']}/{cov}"
            f" · 첫 제안 틀림 {stat[kind, 'wrong_first']}" if cov else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
