"""구성 계산: 기준표에 없는 메뉴를 **이름을 풀어** 근거와 함께 추정한다 (사용자 방향 2026-11-15: "애매한 걸 합리적으로 계산하는 게 장점").

"50개한정 제육덮밥(고기양300g)" -> 틀 = 덮밥 (밥 + 토핑 + 소스·채소), 재료 = 제육, 양 = 300g
-> 밥 200~300g × 쌀밥 120/100g + 제육 300g × (116~195)/100g + 소스 50~120 = 범위·보통값 + 줄별 근거.

- 한국어 메뉴 이름은 끝말이 요리 종류(덮밥·볶음밥·떡볶이·짬뽕)이고 앞말이 재료다. 끝말로 틀을, 앞말로 재료를 찾는다.
- 틀 두 종류: **조립**(덮밥·볶음밥·비빔밥 -- 밥 + 토핑 + 소스로 계산) / **기준 메뉴**(짬뽕·떡볶이·파스타 등 기준표에 있는 요리
  -- 그 값 + 이름에 붙은 재료 추가분).
- 재료 100g당 값은 식약처 음식 DB(사용자가 내려받은 파일, 2026-11-15 조회)의 실제 행에서 옮겼다 (출처 행 이름을 basis에).
  DB에 없는 재료는 가까운 요리로 범위를 잡거나 손으로 정한 값 [확인 안 함] -- 화면에 그대로 보인다.
- 모르는 앞말(로제·크림 등)은 무시하고 notes에 남긴다. 재료 표에 없는 토핑은 "일반 토핑" 넓은 범위 (로컬 LLM이 대신 쓸 DB 요리를 고르게
  하는 건 다음 단계).
검증: tools/compose_eval.py (DB의 복합 메뉴를 이걸로 다시 추정해 DB 값과 비교).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from meal_log.menu_kcal import SOURCE_WORD, MenuKcal

RICE_K100 = 120   # 식약처 음식 DB "쌀밥" 2행 (1인분 350g, 100g당 120)
RICE_SPREAD = 0.2  # 밥 양 ±20% (200~300g 덮밥) -- compose_eval에서 범위가 너무 좁아 넓힘 (같은 세트로 고친 것, 과적합 주의)


@dataclass(frozen=True)
class Ingredient:
    name: str
    aliases: tuple[str, ...]
    low: int            # 100g당 kcal
    mid: int
    high: int
    grams: int          # 이름에 양이 없을 때 토핑 기본 양 (조립 틀) -- 기준 메뉴 틀에서는 "추가"분으로 이 양의 절반
    basis: str


# 100g당 kcal. "DB" = 식약처 음식 DB 행 (min~max, 보통 = 중앙값), "손" = 손으로 정함 [확인 안 함]
INGREDIENTS: tuple[Ingredient, ...] = (
    Ingredient("동파육", ("동파육", "동파"), 158, 300, 435, 150,
               "DB에 없음 → 돼지고기 수육 158 ~ 삼겹살구이 435 (삼겹살 간장 조림이라 둘 사이)"),
    Ingredient("삼겹살", ("삼겹살", "삼겹"), 225, 330, 435, 150, "DB 삼겹살볶음 225 ~ 삼겹살구이 435"),
    Ingredient("수육·보쌈", ("수육", "보쌈"), 84, 125, 166, 150, "DB 수육 2행 84~166"),
    Ingredient("제육", ("제육", "돼지불백", "돼지볶음"), 116, 150, 195, 200, "DB 제육볶음 116 ~ 돼지고기볶음 195"),
    Ingredient("불고기", ("불고기", "불백"), 79, 90, 182, 200, "DB 돼지·소불고기 18행 79~182 (국물 포함이라 낮음)"),
    Ingredient("돈가스", ("돈가스", "돈까스", "카츠", "가츠"), 174, 250, 326, 130, "DB 등심돈가스 174 ~ 돈가스 326"),
    Ingredient("소고기", ("소고기", "쇠고기", "차돌", "스테이크", "등심", "규"), 92, 200, 320, 150,
               "DB 소불고기 92 ~ 소등심구이 320 (덮밥은 불고기식이 많음, 차돌은 DB에 없음)"),
    Ingredient("닭갈비", ("닭갈비", "닭볶음"), 131, 157, 186, 200, "DB 닭볶음(닭갈비) 10행 131~186"),
    Ingredient("데리야끼", ("데리야끼", "데리야키"), 117, 162, 208, 150, "DB 치킨데리야끼 2행 117~208"),
    Ingredient("치킨", ("치킨", "가라아게", "닭튀김", "후라이드"), 125, 236, 375, 150, "DB 닭튀김 53행 125~375"),
    Ingredient("닭고기", ("닭고기", "닭"), 131, 157, 186, 150, "DB에 구운 닭 단독 없음 → 닭볶음(닭갈비) 131~186"),
    Ingredient("오징어", ("오징어", "쭈꾸미", "주꾸미", "낙지"), 77, 82, 128, 250, "DB 오징어볶음 9행 77~128"),
    Ingredient("새우", ("새우",), 145, 194, 265, 100, "DB 새우튀김 4행 145~265 (튀김 기준이라 볶음이면 낮음)"),
    Ingredient("장어", ("장어",), 134, 137, 289, 120, "DB 장어구이 3행 134~289"),
    Ingredient("곱창", ("곱창", "막창", "대창"), 130, 183, 235, 120, "DB 곱창볶음·구이 4행 130~235"),
    Ingredient("탕수육", ("탕수육",), 113, 160, 225, 120, "DB 탕수육 5행 113~225"),
    Ingredient("카레", ("카레", "커리"), 66, 70, 136, 200, "DB 카레소스 5행 66~136"),
    Ingredient("치즈", ("치즈",), 300, 350, 400, 30, "손 [확인 안 함]: 슬라이스·모차렐라 류"),
    Ingredient("계란", ("계란", "달걀", "에그"), 140, 170, 200, 50, "손 [확인 안 함]: 계란후라이 1개 약 50g"),
    Ingredient("햄·소시지", ("스팸", "햄", "소시지", "비엔나"), 118, 133, 280, 80, "DB 소시지볶음 6행 118~280"),
    Ingredient("참치", ("참치",), 120, 180, 250, 80, "손 [확인 안 함]: DB 참치 샐러드 200(마요 포함) 참고"),
    Ingredient("마요", ("마요",), 650, 680, 720, 20, "손 [확인 안 함]: 마요네즈"),
    Ingredient("김치", ("김치",), 15, 25, 40, 80, "손 [확인 안 함]: 배추김치 (음식 DB에 단독 행 없음)"),
)
GENERIC_TOPPING = Ingredient("토핑", (), 80, 180, 300, 150, "재료를 몰라 일반 고기·해물 토핑 범위 (넓음)")


@dataclass(frozen=True)
class Part:
    label: str
    detail: str
    low: int
    mid: int
    high: int


@dataclass(frozen=True)
class BuildFrame:
    """밥 + 토핑 + 소스로 조립하는 요리."""
    head: str
    rice_g: int
    extra: tuple[str, int, int, int]   # (이름, low, mid, high) 손 [확인 안 함]
    topping_g: int = 150               # 재료를 모를 때 일반 토핑 양 (그냥 "볶음밥"은 햄·계란 조금)


BUILD = (
    BuildFrame("덮밥", 250, ("소스·채소", 50, 80, 120)),
    BuildFrame("볶음밥", 280, ("볶음 기름·채소", 70, 100, 140), 60),
    BuildFrame("비빔밥", 230, ("나물·고추장·참기름", 120, 160, 220), 50),
    BuildFrame("라이스", 250, ("소스", 50, 80, 120)),       # 카레라이스·오므라이스
)
RICE_BOWL = BuildFrame("밥", 210, ("양념·간장·기름", 30, 50, 80))   # 간장계란밥 등 -- 기준표 메뉴로 끝나지 않을 때만 (참치김밥은 김밥)
BASE_ALIAS = {"국수": "잔치국수"}   # 기준표에 그 이름이 없는 끝말 -> 가장 가까운 기준 메뉴
# 앞말 재료를 "추가"로 더하는 기준 메뉴. 그 밖(샤브샤브·곰탕·샐러드·순대 등)은 앞말이 요리 자체라 계산할 게 없다 -> 구성 계산 없음
TOPPABLE = ("떡볶이", "라면", "짬뽕", "짜장면", "우동", "칼국수", "잔치국수", "쌀국수", "파스타", "김밥", "피자")
_HEAD_ALIAS = {"덮밥": ("덮밥", "돈부리"), "볶음밥": ("볶음밥",), "비빔밥": ("비빔밥", "비빔"), "라이스": ("라이스",)}
# 일본식 "○○동"은 이 앞말일 때만 덮밥 ("우동"·"중식우동"을 덮밥으로 본 실수, compose_eval 2026-11-15)
_DON = re.compile(r"(규|가츠|카츠|사케|텐|부타|오야코|에비|카이센|돈|장어|연어)동$")


@dataclass
class Estimate:
    name: str
    frame: str                       # "덮밥 (밥 + 토핑 + 양념)" / "짬뽕 (기준표 + 재료)"
    parts: list[Part]
    low: int
    mid: int
    high: int
    mult: float = 1.0                # 2인분·곱빼기·미니
    notes: list[str] = field(default_factory=list)
    unknown: str = ""                # 이름에서 반영 못 한 말 -- 있으면 "대략" (요리 자체가 다를 수 있음: 함박스테이크 ≠ 스테이크)

    def basis(self) -> str:
        """내 메뉴로 저장할 때 근거 한 줄 (200자 안)."""
        s = " + ".join(f"{p.label} {p.detail}" for p in self.parts)
        return (f"구성 계산: {s}" + (f" ×{self.mult:g}" if self.mult != 1 else ""))[:200]


_TAG = re.compile(r"[\[【][^\]】]{0,15}[\]】]")
_LIMITED = re.compile(r"\d+\s*개\s*한정|한정|신메뉴|인기|추천|시그니처|BEST|best")
_GRAMS = re.compile(r"(\d{2,4})\s*g\b", re.I)
_SERV = re.compile(r"(\d)\s*인분")


def _r10(x: float) -> int:
    return int(round(x / 10.0) * 10)


def _clean(name: str) -> tuple[str, Optional[int], float, list[str]]:
    """꼬리표·괄호·'한정'을 떼고, 적힌 양(g)·배수(인분·곱빼기·미니)를 읽는다."""
    notes = []
    grams = None
    m = _GRAMS.search(name)
    if m:
        grams = int(m[1])
    mult = 1.0
    if s := _SERV.search(name):
        mult = float(s[1])
    s = _TAG.sub(" ", name)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = _LIMITED.sub(" ", s)
    if "곱빼기" in s:
        mult, s = mult * 1.5, s.replace("곱빼기", " ")
    if re.search(r"미니|하프|반\s*그릇", s):
        mult, s = mult * 0.5, re.sub(r"미니|하프|반\s*그릇", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s, grams, mult, notes


def _find_ingredients(prefix: str) -> list[Ingredient]:
    found, rest = [], prefix
    for ing in sorted(INGREDIENTS, key=lambda i: -max(len(a) for a in i.aliases)):
        for a in sorted(ing.aliases, key=len, reverse=True):
            if a in rest:
                found.append((prefix.find(a), ing))
                rest = rest.replace(a, " " * len(a))
                break
    return [ing for _, ing in sorted(found, key=lambda x: x[0])]


def estimate(name: str, menus: dict[str, MenuKcal]) -> Optional[Estimate]:
    """기준표에 없는 메뉴 이름 -> 구성 추정. 틀(끝말)을 못 찾으면 None. 이미 기준표에 있는 이름도 None (그 값을 쓰면 됨)."""
    if not name or name in menus:
        return None
    clean, grams, mult, notes = _clean(name)
    compact = clean.replace(" ", "")
    if not compact or compact in {k.replace(" ", "") for k in menus}:
        return None
    # 1) 조립 틀 (끝말)
    for fr in BUILD:
        alias = next((a for a in sorted(_HEAD_ALIAS[fr.head], key=len, reverse=True) if compact.endswith(a)), None)
        if alias is None and fr.head == "덮밥" and _DON.search(compact):
            alias = "동"
        if alias is None:
            continue
        return _build(name, fr, compact[: -len(alias)], grams, mult, notes)
    # 2) 기준 메뉴 틀: 기준표 메뉴 이름으로 끝나면 (가장 긴 것) 그 값 + 앞말 재료 추가분
    tails = sorted((k for k in menus if compact.endswith(k.replace(" ", "")) and len(k.replace(" ", "")) >= 2),
                   key=lambda k: -len(k.replace(" ", "")))
    alias_tail = next((a for a in BASE_ALIAS if compact.endswith(a) and BASE_ALIAS[a] in menus), None)
    if not tails and alias_tail:
        base, prefix = menus[BASE_ALIAS[alias_tail]], compact[: -len(alias_tail)]
        notes.append(f"'{alias_tail}'는 {base.name} 기준")
    elif tails:
        base = menus[tails[0]]
        prefix = compact[: -len(base.name.replace(" ", ""))]
    elif compact.endswith("밥"):
        return _build(name, RICE_BOWL, compact[:-1], grams, mult, notes)
    else:
        return None
    parts = [Part(base.name, f"기준표 {base.unit} ({_src(base)})", base.low, base.mid, base.high)]
    if not any(base.name.endswith(t) for t in TOPPABLE):
        # 토핑을 얹는 요리가 아니면 계산할 게 없다 (기준표 값을 그대로 쓰는 셈) -> 구성 계산을 내지 않고 "비슷한 메뉴"에 맡긴다.
        # compose_eval 2026-11-15: 이 경우(샐러드·곰탕 등) 오차 중앙 44%로, "계산"이라는 이름이 과한 신뢰를 줬다
        return None
    for ing in _find_ingredients(prefix):
        g = grams or ing.grams // 2
        parts.append(Part(f"{ing.name} 추가", f"{g}g × {ing.low}~{ing.high}/100g ({ing.basis})",
                          _r10(g * ing.low / 100), _r10(g * ing.mid / 100), _r10(g * ing.high / 100)))
    left = _unknown_words(prefix, _find_ingredients(prefix))
    if left:
        notes.append(f"반영 못 한 말: {left}")
    return _sum(name, f"{base.name} (기준표 + 재료)", parts, mult, notes, left)


def _build(name: str, fr: BuildFrame, prefix: str, grams: Optional[int], mult: float, notes: list[str]) -> Estimate:
    ings = _find_ingredients(prefix)
    if not ings and fr is not RICE_BOWL:   # 덮밥·볶음밥은 토핑이 있는 요리 -- 모르면 일반 토핑 범위. 그냥 "○○밥"(잡곡밥)은 밥만
        ings = [Ingredient(GENERIC_TOPPING.name, (), GENERIC_TOPPING.low, GENERIC_TOPPING.mid, GENERIC_TOPPING.high,
                           fr.topping_g, GENERIC_TOPPING.basis)]
    lo_g, hi_g = round(fr.rice_g * (1 - RICE_SPREAD)), round(fr.rice_g * (1 + RICE_SPREAD))   # 가게마다 밥 양이 다름
    parts = [Part("밥", f"{lo_g}~{hi_g}g × 쌀밥 {RICE_K100}/100g (식약처 DB)", _r10(lo_g * RICE_K100 / 100),
                  _r10(fr.rice_g * RICE_K100 / 100), _r10(hi_g * RICE_K100 / 100))]
    for i, ing in enumerate(ings):
        g = grams if (grams and i == 0) else (ing.grams if len(ings) == 1 else ing.grams // 2)
        parts.append(Part(ing.name, f"{g}g × {ing.low}~{ing.high}/100g ({ing.basis})",
                          _r10(g * ing.low / 100), _r10(g * ing.mid / 100), _r10(g * ing.high / 100)))
    en, el, em, eh = fr.extra
    parts.append(Part(en, "손으로 정한 값 [확인 안 함]", el, em, eh))
    _left = _unknown_words(prefix, ings)
    if _left:
        notes.append(f"반영 못 한 말: {_left}")
    return _sum(name, f"{fr.head} (밥 + 토핑 + 양념)", parts, mult, notes, _left)


def _src(m: MenuKcal) -> str:
    return SOURCE_WORD.get(m.source, m.source)


def _unknown_words(prefix: str, ings: list[Ingredient]) -> str:
    rest = prefix
    for ing in ings:
        for a in ing.aliases:
            rest = rest.replace(a, "")
    return rest.strip()


def _sum(name: str, frame: str, parts: list[Part], mult: float, notes: list[str], unknown: str = "") -> Estimate:
    lo, mi, hi = (sum(getattr(p, k) for p in parts) * mult for k in ("low", "mid", "high"))
    if mult != 1:
        notes.append(f"이름의 양 표시로 ×{mult:g}")
    return Estimate(name, frame, parts, _r10(lo), _r10(mi), _r10(hi), mult, notes, unknown)
