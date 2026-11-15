"""우리 kcal 기준표(menu_kcal.py)를 식약처 '전국통합식품영양성분정보(음식) 표준데이터'와 비교한다 (바꾸지는 않음).

    python -I tools/kcal_db_compare.py <내려받은 .xls 경로>          # -> out/kcal_compare.md
    python -I tools/kcal_db_compare.py <내려받은 .xls 경로> --emit   # 교체 후보를 menu_kcal.DB_VALUES 형식으로 출력

DB 값은 100g(ml)당 kcal + 식품중량(그 출처가 잰 양). 1인분 kcal = 에너지 × 식품중량 ÷ 100.
발라내는 규칙 (2026-11-14 파일을 보고 정함):
- 외식 행만 (재료량 기반 산출·분석). 급식·가정식은 양이 달라 뺀다. 프랜차이즈 행은 따로 열에 참고로
- 식품중량이 100 이하(그냥 100g 기준으로만 적힌 행)·1.5kg 초과(여러 명 분량)인 행, '간편조리세트'(밀키트)는 뺀다
- 식품명 '기본명_변형'(김치찌개_참치)은 기본명으로 묶는다
- 우리 메뉴가 '공기밥 포함'이고 DB 식품명이 국·찌개·탕(밥이 이름에 없음)이면 공기밥을 더한다
"""
from __future__ import annotations

import re
import statistics
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from meal_log.menu_kcal import MENUS  # noqa: E402

GENERAL = ("외식(재료량 기반 산출함량)", "외식(분석함량)")
FRANCHISE = "외식(프랜차이즈 등 업체 제공 영양정보)"
MIN_W, MAX_W = 100, 1500
# 우리 메뉴 이름 -> DB에서 찾을 기본명 (철자·이름 체계가 다른 것만). 추측으로 넣은 것도 있어 결과의 행 수로 확인한다
ALIASES = {
    "돈까스": ["돈가스", "돈까스"], "김치찌개 정식": ["김치찌개"], "된장찌개 정식": ["된장찌개"], "제육볶음 정식": ["제육볶음"],
    "돼지국밥": ["돼지국밥", "돼지머리국밥"], "설렁탕": ["설렁탕", "설농탕"], "카레라이스": ["카레라이스", "카레"],
    "치킨": ["후라이드치킨", "프라이드치킨", "양념치킨"], "삼겹살": ["삼겹살", "삼겹살구이"], "돼지갈비": ["돼지갈비", "돼지갈비구이"],
    "곱창": ["곱창", "곱창구이"], "모둠튀김": ["튀김"], "회": ["생선회", "회"], "죽": ["전복죽", "호박죽", "죽"],
    "공기밥": ["쌀밥", "공기밥", "흰쌀밥"], "백반": ["백반"], "파스타": ["파스타", "스파게티"], "라멘": ["라멘"],
}
# 일반 외식 행보다 프랜차이즈 행이 우리 단위에 맞는 메뉴: 식품명 끝 괄호의 크기 표기로 고른다 (2026-11-14 결정)
FRANCHISE_PICKS = {"피자": re.compile(r"\(L\)\s*$")}   # 라지 1판
RICE_IN_NAME = re.compile(r"밥")          # 국밥·덮밥·비빔밥 -> 이미 밥 포함
DEFAULT_RICE = MENUS["공기밥"].mid


def load(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, engine="xlrd", dtype=str, header=1)   # 첫 줄은 비어 있고 둘째 줄이 열 이름
    df["w"] = df["식품중량"].str.extract(r"^\s*([\d.]+)\s*(?:g|ml)\s*$")[0].astype(float)
    df["serv"] = pd.to_numeric(df["에너지(kcal)"], errors="coerce") * df["w"] / 100
    df["base"] = df["식품명"].fillna("").str.split("_").str[0].str.strip()
    ok = (df["w"] > MIN_W) & (df["w"] <= MAX_W) & ~df["식품명"].fillna("").str.contains("간편조리세트") & df["serv"].notna()
    return df[ok]


def summary(vals: list[float]) -> dict:
    vals = sorted(vals)
    if len(vals) >= 4:
        q = statistics.quantiles(vals, n=4)
        lo, hi = q[0], q[2]
    else:
        lo, hi = vals[0], vals[-1]
    return {"n": len(vals), "low": round(lo), "mid": round(statistics.median(vals)), "high": round(hi)}


def main(path: str, emit: bool = False) -> None:
    df = load(path)
    gen, fr = df[df["식품기원명"].isin(GENERAL)], df[df["식품기원명"] == FRANCHISE]
    rice_rows = gen[gen["base"].isin(ALIASES["공기밥"])]["serv"].tolist()
    rice = round(statistics.median(rice_rows)) if rice_rows else DEFAULT_RICE
    rows, counts = [], {"교체 후보": 0, "확인 필요": 0, "참고만": 0, "매칭 없음": 0}
    for name, m in MENUS.items():
        keys = ALIASES.get(name, [name])
        hit = gen[gen["base"].isin(keys) | gen["대표식품명"].isin(keys)]
        with_rice = "공기밥 포함" in m.unit
        vals, added = [], 0
        for nm, v in zip(hit["식품명"], hit["serv"]):
            if with_rice and not RICE_IN_NAME.search(nm):
                v += rice
                added += 1
            vals.append(v)
        fv = fr[fr["base"].isin(keys) | fr["대표식품명"].isin(keys)]["serv"].tolist()
        fs = summary(fv) if fv else None
        if name in FRANCHISE_PICKS:
            pv = fr[(fr["base"].isin(keys) | fr["대표식품명"].isin(keys))
                    & fr["식품명"].str.contains(FRANCHISE_PICKS[name], na=False)]["serv"].tolist()
            s = summary(pv)
            counts["프랜차이즈 채택"] = counts.get("프랜차이즈 채택", 0) + 1
            rows.append(("프랜차이즈 채택", name, m, s, 0, fs))
            continue
        if not vals:
            verdict, s = "매칭 없음", None
        else:
            s = summary(vals)
            diff = abs(s["mid"] - m.mid) / m.mid
            verdict = "참고만" if s["n"] < 3 else ("교체 후보" if diff <= 0.3 else "확인 필요")
        counts[verdict] += 1
        rows.append((verdict, name, m, s, added, fs))
    order = {"프랜차이즈 채택": 0, "교체 후보": 0, "확인 필요": 1, "참고만": 2, "매칭 없음": 3}
    rows.sort(key=lambda r: (order[r[0]], r[1]))
    out = ROOT / "out" / "kcal_compare.md"
    out.parent.mkdir(exist_ok=True)
    lines = [f"# kcal 기준표 vs 식약처 음식 DB ({Path(path).name})", "",
             f"외식(산출·분석) 행, 식품중량 {MIN_W} 초과 ~ {MAX_W} 이하, 간편조리세트 제외. "
             f"공기밥 = {rice} kcal ({'DB ' + str(len(rice_rows)) + '행 중앙값' if rice_rows else '우리 표 값'}). "
             "범위는 4행 이상이면 25~75%, 아니면 최소~최대.", "",
             "판정: 교체 후보 = 3행 이상이고 보통값 차이 30% 이내 / 확인 필요 = 3행 이상인데 30% 넘게 다름 / 참고만 = 1~2행 / "
             "프랜차이즈 채택 = 프랜차이즈 행을 크기 표기로 골라 씀 (DB 칸이 그 값)", "",
             "| 판정 | 메뉴 (단위) | 우리 낮음/보통/높음 | DB 행 | DB 범위/중앙 | 공기밥 더한 행 | 프랜차이즈 행·중앙 |",
             "|---|---|---|---|---|---|---|"]
    def rng(s: dict | None) -> str:
        return "{low}~{high} / {mid}".format(**s) if s else ""

    for verdict, name, m, s, added, fs in rows:
        franchise = "{n} · {mid}".format(**fs) if fs else ""
        lines.append(f"| {verdict} | {name} ({m.unit}) | {m.low}/{m.mid}/{m.high} | {s['n'] if s else 0} | {rng(s)} | "
                     f"{added or ''} | {franchise} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"공기밥 {rice} kcal ({len(rice_rows)}행) · " + " · ".join(f"{k} {v}" for k, v in counts.items()) + f"  -> {out}")
    if emit:
        for verdict, name, m, s, added, fs in sorted(rows, key=lambda r: r[1]):
            if verdict in ("교체 후보", "프랜차이즈 채택"):
                print(f'    "{name}": ({s["low"]}, {s["mid"]}, {s["high"]}, {s["n"]}),')


if __name__ == "__main__":
    main(sys.argv[1], emit="--emit" in sys.argv[2:])
