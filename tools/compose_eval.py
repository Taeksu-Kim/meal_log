"""구성 계산(compose.py)이 맞는지 -- 식약처 음식 DB의 복합 메뉴를 이름만으로 다시 추정해 DB 1인분 값과 비교한다.

    python tools/compose_eval.py <전국통합식품영양성분정보_음식_표준데이터.xls>

대상 = DB의 요리 이름(base, "_" 앞) 중 compose가 틀을 찾는 것 (기준표에 이미 있는 이름은 빼고). 정답 = 그 이름 행들의 1인분 kcal 중앙값.
지표: 보통값 오차(|추정 - 정답| ÷ 정답) 중앙값, 정답이 추정 범위 안에 든 비율. 재료 표의 근거로 쓴 요리(제육볶음 등)는 덮밥·볶음밥
이름이 아니라 대상과 겹치지 않는다 (밥 값 "쌀밥"만 공통). 경향일 뿐 -- DB 1인분 중량이 행마다 크게 다르다 (오징어덮밥 900g 등).
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from kcal_db_compare import load  # noqa: E402

from meal_log import compose, menu_kcal  # noqa: E402


def main(path: str) -> int:
    df = load(path)
    menus = dict(menu_kcal.MENUS)   # 기준표만 (사용자 메뉴 없이)
    rows = []
    for base, g in df.groupby("base"):
        e = compose.estimate(base, menus)
        if e is None:
            continue
        truth = float(g["serv"].median())
        rows.append((base, len(g), truth, float(g["w"].median()), e))
    if not rows:
        print("대상 없음")
        return 1
    errs, inside = [], 0
    for base, n, truth, w, e in sorted(rows, key=lambda r: r[0]):
        err = abs(e.mid - truth) / truth
        errs.append(err)
        inside += e.low <= truth <= e.high
        print(f"{base} ({n}행, 1인분 {w:.0f}g) 정답 {truth:.0f} | 추정 {e.low}–{e.high}/{e.mid} ({err:.0%})"
              f"{'' if e.low <= truth <= e.high else ' 범위 밖'} · {e.frame}" + (f" · {e.notes}" if e.notes else ""))
    print(f"\n전체 {len(rows)}개 · 보통값 오차 중앙 {statistics.median(errs):.0%} · 정답이 범위 안 {inside}/{len(rows)}")
    for label, keep in (("이름을 다 이해함", lambda e: not e.unknown), ("모르는 말 섞임 (화면에 '대략')", lambda e: bool(e.unknown))):
        sub = [(t, e) for _, _, t, _, e in rows if keep(e)]
        if sub:
            print(f"  {label}: {len(sub)}개 · 오차 중앙 {statistics.median(abs(e.mid - t) / t for t, e in sub):.0%}"
                  f" · 범위 안 {sum(e.low <= t <= e.high for t, e in sub)}/{len(sub)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
