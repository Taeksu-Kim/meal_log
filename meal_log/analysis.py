"""끼니 기록에 kcal·확인 이유를 붙이고, 캘린더·주별 추이를 만든다 (코드만).

kcal는 **1인 기준**: 메뉴 kcal × 수량 합 ÷ 인원. 기준표에 없고 직접 넣지도 않은 메뉴는 kcal 모름 (0으로 치지 않는다).
하루 kcal은 kcal를 아는 끼니만 더한 값이라 모르는 끼니가 있으면 실제보다 낮다 -- 화면에 "모름 n끼"를 같이 보여 준다.

**정리 기준일** (settings ``review_since``): 이 날짜 이전 끼니는 확인 이유가 있어도 "확인 필요"로 띄우지 않는다 (모름으로 둠,
데이터는 그대로). 매일 열든 주 단위로 열든, 기준일 뒤에 안 채운 끼니는 처리할 때까지 남는다.
처음엔 기록 마지막 날 − ``INITIAL_REVIEW_DAYS``일로 한 번 정해 저장한다. 빈 문자열 = 기준일 없음 (전부 확인 대상).
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Literal, Optional

from meal_log import menu_kcal, slots
from meal_log.store import Store

Reason = Literal["메뉴 모름", "같은 끼니 여러 건", "인원 확인", "시간 없음"]
PEOPLE_CHECK_RATIO = 2.0        # 평소(같은 소분류 중앙값)의 이 배 이상이면 "몇 명이 먹었나요?"
PEOPLE_CHECK_MIN_SAMPLES = 5    # 중앙값을 믿으려면 이만큼은 있어야
PEOPLE_CHECK_CATEGORIES = ("외식", "배달")
KcalSource = Literal["table", "db", "user", "llm", "label", "calc", "manual"]   # 기준표 근사 / 식약처 DB / 내 메뉴 / 내 메뉴(LLM 추정) / 끼니에 직접 입력
SlotState = Literal["ok", "review", "unknown", "skip", "none"]
INITIAL_REVIEW_DAYS = 7      # 처음 정리 기준일 = 기록 마지막 날 - 이만큼 (지난 기록은 모름으로 둠)
DEFAULT_BACKLOG_WARN = 20    # 확인할 끼니가 이보다 많으면 "○일 이전은 모름으로 두기" 제안
KEEP_DAYS_ON_BURY = 7        # 그 제안이 남겨 두는 최근 일수


def all_menus(store: Store) -> dict[str, menu_kcal.MenuKcal]:
    """기준표 + 사용자 메뉴 (이름이 겹치면 기준표 -- 사용자 메뉴는 기준표에 없는 이름만 받는다)."""
    out = dict(menu_kcal.MENUS)
    for r in store.user_menus():
        out.setdefault(r["name"], menu_kcal.MenuKcal(r["name"], r["unit"], r["low"], r["mid"], r["high"], r["source"],
                                                     0, r["basis"] or ""))
    return out


def item_kcal(it: dict, menus: Optional[dict[str, menu_kcal.MenuKcal]] = None) -> Optional[dict]:
    qty = it["qty"] or 0
    if it["kcal_manual"] is not None:
        v = it["kcal_manual"] * qty
        return {"low": v, "mid": v, "high": v, "source": "manual"}
    m = (menus or menu_kcal.MENUS).get(it["name"])
    if m is None:
        return None
    return {"low": m.low * qty, "mid": m.mid * qty, "high": m.high * qty, "source": m.source, "unit": m.unit,
            "db_rows": m.db_rows, "basis": m.basis}


def counted(m: dict) -> bool:
    """먹은 끼니로 셈 (장보기·건너뜀·제외 빼고)."""
    return m["slot"] in slots.EATING and not m["skipped"] and not m["excluded"]


def _typical(meals: list[dict]) -> dict[str, float]:
    by: dict[str, list[int]] = {}
    for m in meals:
        if m["source"] == "card" and m["category"] in PEOPLE_CHECK_CATEGORIES and m["amount"]:
            by.setdefault(m["category"], []).append(m["amount"])
    return {k: statistics.median(v) for k, v in by.items() if len(v) >= PEOPLE_CHECK_MIN_SAMPLES}


def annotate(meals: list[dict], typical: Optional[dict[str, float]] = None,
             menus: Optional[dict[str, menu_kcal.MenuKcal]] = None) -> list[dict]:
    """각 끼니에 kcal(1인)·항목 kcal·확인 이유를 붙인다. ``typical``: 인원 확인용 소분류 중앙값 (전체 기록에서).
    ``menus``: 기준표 + 사용자 메뉴 (없으면 기준표만)."""
    typical = _typical(meals) if typical is None else typical
    per_slot: dict[tuple[str, str], int] = {}
    for m in meals:
        if counted(m):
            per_slot[(m["date"], m["slot"])] = per_slot.get((m["date"], m["slot"]), 0) + 1
    out = []
    for m in meals:
        items = [it | {"kcal": item_kcal(it, menus)} for it in m["items"]]
        kc = None
        if counted(m) and any(it["kcal"] for it in items):
            ppl = max(m["people"] or 1, 1)
            kc = {k: round(sum(it["kcal"][k] for it in items if it["kcal"]) / ppl) for k in ("low", "mid", "high")}
            kc["partial"] = any(it["kcal"] is None for it in items)
        reasons: list[Reason] = []
        if counted(m) and not m["confirmed"]:
            if m["slot"] in slots.MEALS and not items:
                reasons.append("메뉴 모름")
            if m["slot"] in slots.MEALS and per_slot.get((m["date"], m["slot"]), 0) > 1:
                reasons.append("같은 끼니 여러 건")
            t = typical.get(m["category"] or "")
            if m["source"] == "card" and t and m["people"] == 1 and m["amount"] >= PEOPLE_CHECK_RATIO * t:
                reasons.append("인원 확인")
            if m["no_time"]:
                reasons.append("시간 없음")
        out.append(m | {"items": items, "kcal": kc, "reasons": reasons, "guessed": any(it["guessed"] for it in m["items"])})
    return out


def review_since(store: Store, allm: Optional[list[dict]] = None) -> Optional[str]:
    """정리 기준일 (ISO). 아직 안 정했으면 기록 마지막 날 − 7일로 정해 저장 (기록이 없으면 None)."""
    v = store.setting("review_since")
    if v is not None:
        return v or None
    allm = store.meals() if allm is None else allm
    if not allm:
        return None
    v = (date.fromisoformat(allm[-1]["date"]) - timedelta(days=INITIAL_REVIEW_DAYS)).isoformat()
    store.set_setting("review_since", v)
    return v


def backlog_warn(store: Store) -> int:
    v = store.setting("backlog_warn")
    return int(v) if v else DEFAULT_BACKLOG_WARN


def meals_between(store: Store, start: Optional[date], end: Optional[date]) -> list[dict]:
    """기간 안 끼니 + 확인 이유 (인원 확인 기준은 전체 기록, 같은 끼니 여러 건은 그 날 안에서 -- 둘 다 기간과 무관하게 같게).
    ``needs_review``: 확인 이유가 있고 정리 기준일 이후."""
    allm = store.meals()
    typical = _typical(allm)
    since = review_since(store, allm) or ""
    s, e = (start.isoformat() if start else ""), (end.isoformat() if end else "9999")
    return [m | {"needs_review": bool(m["reasons"]) and m["date"] >= since}
            for m in annotate(allm, typical, all_menus(store)) if s <= m["date"] <= e]


def review(store: Store) -> dict:
    """확인할 끼니 (오래된 것부터 -- 순서대로 채워 나가게). 기준일 이전이라 모름으로 둔 수와, 많이 쌓였으면 옮길 기준일 제안."""
    allm = meals_between(store, None, None)
    rows = [m for m in allm if m["needs_review"]]
    rows.sort(key=lambda m: (m["date"], m["time"] or "", m["id"]))
    counts: dict[str, int] = {}
    for m in rows:
        for r in m["reasons"]:
            counts[r] = counts.get(r, 0) + 1
    warn = backlog_warn(store)
    suggest = None
    if len(rows) > warn:
        suggest = (date.fromisoformat(rows[-1]["date"]) - timedelta(days=KEEP_DAYS_ON_BURY - 1)).isoformat()
        if suggest <= rows[0]["date"]:   # 최근 7일 안에만 몰려 있으면 옮겨도 줄지 않는다
            suggest = None
    return {"count": len(rows), "by_reason": counts, "rows": rows, "review_since": review_since(store),
            "buried": sum(1 for m in allm if m["reasons"] and not m["needs_review"]),
            "backlog_warn": warn, "suggest_since": suggest}


def slot_state(entries: list[dict]) -> SlotState:
    """한 날 한 끼니 칸의 상태. 확인 필요 > 메뉴 모름(기준일 이전) > 먹음 > 건너뜀 > 없음."""
    eaten = [m for m in entries if counted(m)]
    if any(m["needs_review"] for m in eaten):
        return "review"
    if any(not m["items"] for m in eaten):
        return "unknown"
    if eaten:
        return "ok"
    return "skip" if any(m["skipped"] for m in entries) else "none"


def _day_kcal(ms: list[dict]) -> tuple[Optional[dict], int]:
    known = [m["kcal"] for m in ms if m["kcal"]]
    unknown = sum(1 for m in ms if counted(m) and (m["kcal"] is None or m["kcal"]["partial"]))
    if not known:
        return None, unknown
    return {k: sum(x[k] for x in known) for k in ("low", "mid", "high")}, unknown


def calendar(store: Store, month: str) -> dict:
    first = date.fromisoformat(month + "-01")
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    last = nxt - timedelta(days=1)
    by_day: dict[str, list[dict]] = {}
    for m in meals_between(store, first, last):
        by_day.setdefault(m["date"], []).append(m)
    days = []
    d = first
    while d <= last:
        ms = by_day.get(d.isoformat(), [])
        cells = {}
        for s in slots.EATING:
            sm = [m for m in ms if m["slot"] == s]
            eaten = [m for m in sm if counted(m)]
            # entries: 칸에 줄로 보일 끼니 하나하나 (메뉴 글자, 모르면 None)
            cells[s] = {"state": slot_state(sm), "count": len(eaten),
                        "menus": [it["name"] for m in eaten for it in m["items"]],
                        "entries": [{"id": m["id"], "label": ", ".join(it["name"] for it in m["items"]) or None,
                                     "needs_review": m["needs_review"]} for m in eaten]}
        kc, unknown = _day_kcal(ms)
        days.append({"date": d.isoformat(), "slots": cells, "kcal": kc, "unknown": unknown,
                     "groceries": sum(1 for m in ms if m["slot"] == "장보기" and not m["excluded"])})
        d += timedelta(days=1)
    span = store.meals()
    return {"month": month, "days": days, "first_date": span[0]["date"] if span else None,
            "last_date": span[-1]["date"] if span else None}


def weekly(store: Store, end: Optional[date] = None, weeks: int = 12) -> dict:
    """주(월~일)별: 하루 평균 kcal(**모든 끼니의 kcal를 아는 날만** -- 모르는 끼니가 섞인 날은 실제보다 낮아서 뺀다),
    kcal 일부만 아는 날 수, 야식한 날, 배달·외식 횟수, kcal 모르는 끼니.
    ``end``가 든 주까지 최대 ``weeks``주, 첫 기록이 든 주보다 앞은 뺀다."""
    allm = store.meals()
    if not allm:
        return {"end": (end or date.today()).isoformat(), "weeks": []}
    if end is None:
        end = date.fromisoformat(allm[-1]["date"])
    last_monday = end - timedelta(days=end.weekday())
    start = last_monday - timedelta(weeks=weeks - 1)
    if allm:   # 첫 기록 전의 빈 주는 빼고
        first = date.fromisoformat(allm[0]["date"])
        start = max(start, first - timedelta(days=first.weekday()))
    weeks = max(0, (last_monday - start).days // 7 + 1)
    ms = meals_between(store, start, last_monday + timedelta(days=6))
    out = []
    for i in range(weeks):
        ws = start + timedelta(weeks=i)
        wk = [m for m in ms if ws.isoformat() <= m["date"] <= (ws + timedelta(days=6)).isoformat()]
        days: dict[str, list[dict]] = {}
        for m in wk:
            days.setdefault(m["date"], []).append(m)
        per_day = [_day_kcal(v) for v in days.values()]
        dk = [k for k, unknown in per_day if k and not unknown]
        eaten = [m for m in wk if counted(m)]
        out.append({
            "week_start": ws.isoformat(),
            "kcal_days": len(dk),
            "partial_days": sum(1 for k, unknown in per_day if k and unknown),
            "avg_kcal": {k: round(sum(x[k] for x in dk) / len(dk)) for k in ("low", "mid", "high")} if dk else None,
            "yasik_days": len({m["date"] for m in eaten if m["slot"] == "야식"}),
            "delivery": sum(1 for m in eaten if m["category"] == "배달"),
            "eating_out": sum(1 for m in eaten if m["category"] == "외식"),
            "meals": len(eaten),
            "unknown": sum(1 for m in eaten if m["kcal"] is None or m["kcal"]["partial"]),
        })
    return {"end": end.isoformat(), "weeks": out}
