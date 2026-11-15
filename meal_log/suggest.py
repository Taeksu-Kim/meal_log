"""로컬 LLM 제안 -- 지금은 기준표에 없는 메뉴의 kcal 추정 하나 (DESIGN.md 5절).

보내는 것: 메뉴 이름과 단위, 그리고 **우리 기준표에서 이름이 비슷한 메뉴 몇 개의 kcal (참고 기준)** -- 날짜·가게·금액 안 보냄.
참고 기준을 주는 이유: LLM의 일반 상식만으로 추정하면 기준표(식약처 값·사용자가 정한 값)와 따로 논다. 비슷한 메뉴가 있으면 그 값에서
양·재료 차이만 반영하게 한다. 효과는 ``tools/kcal_estimate_eval.py``로 잰다 (식약처 값 메뉴를 정답으로, 자기 자신은 참고에서 뺌).
답은 제안이다 -- 화면이 "LLM 추정"으로 보여 주고 사람이 저장해야 내 메뉴가 된다.
모르면 known=false로 답하게 한다 (억지로 숫자를 지어내게 하지 않는다). 숫자는 코드로 범위를 다시 검사한다.
"""

from __future__ import annotations

import json
import re
from typing import Iterable, Optional

from meal_log.llm import LocalLLM, ask_json
from meal_log.menu_kcal import MenuKcal

REF_K = 4                 # 참고 기준으로 줄 이름이 비슷한 메뉴 수 (+ 양 감각용 ANCHORS 몇 개)
REF_MAX = 7
# 비슷한 메뉴가 하나도 없을 때 양 감각을 잡아 줄 기준 (밥 한 공기·간식·한 끼·음료 -- 기준표에 있는 것만 씀)
ANCHORS = ("공기밥", "김밥", "라면", "햄버거", "아메리카노", "과자")
from meal_log.menu_kcal import SOURCE_WORD  # noqa: E402  (출처 이름은 menu_kcal 한 곳)

MAX_KCAL = 5000
SYSTEM = (
    "음식 이름과 단위를 받아, 한국에서 흔히 먹는 양 기준으로 그 단위 하나의 열량(kcal) 범위를 추정한다. "
    "단위가 비어 있으면 흔히 먹는 1회 분량(1개·1그릇·1인분 등)을 정해 unit에 적는다. "
    "low·mid·high는 정수 kcal (low ≤ mid ≤ high). 무슨 음식인지 모르거나 양을 가늠할 수 없으면 known=false.\n"
    "'참고 기준'은 이 서비스가 쓰는 다른 메뉴의 kcal다. 같은 계열의 메뉴가 있으면 그 값을 출발점으로 양·재료 차이만 반영해 "
    "일관되게 추정하고, 없으면 양 감각을 맞추는 데만 쓴다. 이름이 비슷해도 다른 음식이면 쓰지 않는다 (감자 ≠ 감자탕). "
    "basis에 근거를 한 문장으로 (참고한 메뉴가 있으면 그 이름을 적는다. 예: '떡볶이 1인분 480 기준, 치즈 추가로 +100')."
)
SCHEMA = {"type": "object", "required": ["known", "unit", "low", "mid", "high", "basis"], "properties": {
    "known": {"type": "boolean"}, "unit": {"type": "string"}, "low": {"type": "integer"}, "mid": {"type": "integer"},
    "high": {"type": "integer"}, "basis": {"type": "string"}}}


def _check(d: object) -> dict:
    from agent.core.llm import OutputError

    if not isinstance(d, dict) or not isinstance(d.get("known"), bool):
        raise OutputError("known(참/거짓)이 없음")
    if not d["known"]:
        return {"known": False}
    unit = str(d.get("unit") or "").strip()
    if re.fullmatch(r"[가-힣]{1,3}", unit):   # "개" -> "1개" (LLM이 섞어 답함, 2026-11-15 실측)
        unit = "1" + unit
    try:
        low, mid, high = (int(d[k]) for k in ("low", "mid", "high"))
    except (KeyError, TypeError, ValueError) as exc:
        raise OutputError(f"low·mid·high가 정수가 아님: {d!r}") from exc
    if not unit or len(unit) > 20:
        raise OutputError("unit이 비었거나 너무 김")
    if not (0 <= low <= mid <= high <= MAX_KCAL) or high == 0:
        raise OutputError(f"kcal 범위가 이상함 (0 ≤ low ≤ mid ≤ high ≤ {MAX_KCAL}): {low}/{mid}/{high}")
    return {"known": True, "unit": unit, "low": low, "mid": mid, "high": high, "basis": str(d.get("basis") or "")[:200]}


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def similar_menus(name: str, menus: Iterable[MenuKcal], k: int = REF_K) -> list[MenuKcal]:
    """이름이 비슷한 메뉴 k개 (두 글자 조각 겹침 + 포함 관계) + 양 감각용 ANCHORS, 최대 REF_MAX개. 자기 자신은 뺀다.
    이름만 비슷한 다른 음식(감자 -> 감자탕)도 걸리므로 기준(ANCHORS)을 항상 같이 주고, 프롬프트에서 걸러 쓰게 한다."""
    pool = [m for m in menus if m.name != name]
    q = _bigrams(name)
    scored = []
    for m in pool:
        b = _bigrams(m.name)
        score = len(q & b) / len(q | b) + (0.5 if (m.name in name or name in m.name) else 0)
        if score > 0:
            scored.append((score, m.name, m))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [m for _, _, m in scored[:k]]
    by = {m.name: m for m in pool}
    picked += [by[a] for a in ANCHORS if a in by and by[a] not in picked]
    return picked[:REF_MAX]


def estimate_kcal(llm: LocalLLM, name: str, unit: Optional[str] = None,
                  menus: Optional[Iterable[MenuKcal]] = None) -> dict:
    """``menus``: 참고 기준을 고를 메뉴 (기준표 + 내 메뉴). None이면 참고 기준 없이 (평가 비교용)."""
    refs = similar_menus(name, menus) if menus is not None else []
    payload = {"음식": name, "단위": unit or ""}
    if refs:
        payload["참고 기준"] = [f"{m.name} {m.unit}: {m.low}–{m.high} (보통 {m.mid}, {SOURCE_WORD.get(m.source, m.source)})"
                             for m in refs]
    out = ask_json(llm, SYSTEM, json.dumps(payload, ensure_ascii=False), SCHEMA, _check, what="kcal estimate")
    return out | {"refs": [m.name for m in refs]} if out.get("known") else out
