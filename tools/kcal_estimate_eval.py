"""LLM kcal 추정에 '참고 기준'(비슷한 기준표 메뉴)을 주면 나아지는지 (실제 로컬 vLLM).

    python tools/kcal_estimate_eval.py [--llm-url http://127.0.0.1:8002/v1]

정답 = 식약처 DB 값으로 바꾼 메뉴 (menu_kcal.DB_VALUES). 그 메뉴 자신은 참고 기준에서 뺀다 (similar_menus가 뺌).
지표: 보통값 오차(|추정 - 정답| ÷ 정답)의 중앙값, 정답 보통값이 추정 범위 안에 든 비율. temperature 0, 1회 -- 경향일 뿐.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from meal_log import menu_kcal  # noqa: E402
from meal_log.llm import LLMBadOutput, LLMDown, make_local_llm  # noqa: E402
from meal_log.suggest import estimate_kcal  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm-url", default="http://127.0.0.1:8002/v1")
    args = ap.parse_args()
    llm = make_local_llm(args.llm_url)
    menus = list(menu_kcal.MENUS.values())
    res = {"참고 없음": [], "참고 있음": []}
    for name in menu_kcal.DB_VALUES:
        truth = menu_kcal.MENUS[name]
        line = [f"{name} ({truth.unit}) 정답 {truth.mid}"]
        for label, pool in (("참고 없음", None), ("참고 있음", menus)):
            try:
                r = estimate_kcal(llm, name, truth.unit, menus=pool)
            except (LLMDown, LLMBadOutput) as exc:
                line.append(f"{label}: 오류 {type(exc).__name__}")
                continue
            if not r.get("known"):
                line.append(f"{label}: 모름")
                res[label].append((None, False))
                continue
            err = abs(r["mid"] - truth.mid) / truth.mid
            res[label].append((err, r["low"] <= truth.mid <= r["high"]))
            line.append(f"{label}: {r['low']}–{r['high']}/{r['mid']} ({err:.0%})" + (f" 참고 {r['refs'][:2]}" if r.get("refs") else ""))
        print(" | ".join(line))
    for label, rows in res.items():
        errs = [e for e, _ in rows if e is not None]
        print(f"{label}: 보통값 오차 중앙 {statistics.median(errs):.0%} · 정답이 범위 안 {sum(h for _, h in rows)}/{len(rows)}"
              f" · 모름 {sum(e is None for e, _ in rows)}" if errs else f"{label}: 결과 없음")


if __name__ == "__main__":
    main()
