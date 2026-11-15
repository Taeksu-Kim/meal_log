"""대화 도우미가 쓰는 도구 -- 두 대화 세션(① Claude Code, ② 로컬 LLM)이 **같은 목록**을 쓴다 (REF/AGENT_PRODUCT.md 채팅 세션 2종).

모두 서비스(service.py) 함수 위의 얇은 층. 결과에는 **식단만** 담는다 (사용자 결정 2026-11-15): 끼니·메뉴·수량·kcal·인원·시각은 주고,
결제 금액·가게 이름·카드 범주·결제일은 주지 않는다 -- ①은 내용이 Anthropic으로 나가기 때문. 걸러 내는 곳은 ``_meal`` 하나.
기록은 확실할 때만: log_meal은 끼니·수량이 모자라면 저장하지 않고 ``need``를 돌려줘 도우미가 사용자에게 되묻게 한다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable

from meal_log import menu_kcal, slots
from meal_log.service import Invalid, MealService

MAX_DAYS = 31


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    run: Callable[[MealService, dict], Any]


def _date(v: Any, what: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError as exc:
        raise Invalid(f"{what}는 YYYY-MM-DD") from exc


def _meal(m: dict) -> dict:
    """끼니 하나 -> 도우미에게 줄 모양 (결제 금액·가게·카드 범주·결제일은 뺀다)."""
    return {"id": m["id"], "date": m["date"], "slot": m["slot"], "time": m["time"],
            "items": [{"name": i["name"], "qty": i["qty"],
                       "kcal": None if not i["kcal"] else {k: round(i["kcal"][k]) for k in ("low", "mid", "high")}}
                      for i in m["items"]],
            "people": m["people"], "kcal_per_person": m["kcal"], "needs_menu": not m["items"] and not m["skipped"],
            "skipped": bool(m["skipped"]), "excluded": bool(m["excluded"]), "note": m["note"]}


def _query(svc: MealService, a: dict) -> dict:
    end = _date(a.get("end") or dt.date.today().isoformat(), "end")
    start = _date(a.get("start") or (end - dt.timedelta(days=6)).isoformat(), "start")
    if start > end or (end - start).days >= MAX_DAYS:
        raise Invalid(f"기간은 start ≤ end, {MAX_DAYS}일까지")
    ms = [m for m in svc.meals(start, end) if m["slot"] in slots.EATING]
    days = []
    d = start
    while d <= end:
        day = [m for m in ms if m["date"] == d.isoformat()]
        eaten = [m for m in day if not m["skipped"] and not m["excluded"]]
        known = [m["kcal"] for m in eaten if m["kcal"]]
        unknown = sum(1 for m in eaten if not m["kcal"] or m["kcal"]["partial"])
        days.append({"date": d.isoformat(), "weekday": "월화수목금토일"[d.weekday()], "meals": [_meal(m) for m in day],
                     "kcal_known_sum": {k: sum(x[k] for x in known) for k in ("low", "mid", "high")} if known else None,
                     "unknown_meals": unknown})
        d += dt.timedelta(days=1)
    return {"start": start.isoformat(), "end": end.isoformat(), "days": days,
            "note": "kcal_known_sum은 kcal를 아는 끼니만 더한 값. unknown_meals가 있으면 실제는 이보다 많음"}


def _log(svc: MealService, a: dict) -> dict:
    text = str(a.get("text") or "").strip()
    if not text:
        raise Invalid("text가 필요")
    parsed = svc.chat_parse(text, day=_date(a["date"], "date") if a.get("date") else None)
    entries = parsed["entries"]
    if not entries:
        return {"saved": [], "message": "먹은 내용을 찾지 못함"}
    for e in entries:
        if not e["slot"] and a.get("slot") in slots.EATING:
            e["slot"] = a["slot"]
        for it in e["items"]:
            fix = next((x for x in a.get("items") or [] if x.get("name") == it["name"]), None)
            if fix and isinstance(fix.get("qty"), (int, float)) and 0 < fix["qty"] <= 20:
                it["qty"], it["unit_mismatch"] = float(fix["qty"]), False
    need = {}
    if any(not e["slot"] for e in entries):
        need["slot"] = "끼니(아침·점심·저녁·간식·야식)를 물어보고 slot으로 다시 부를 것"
    bad = [{"name": i["name"], "said": f"{i['qty']:g}{i['said_unit']}", "table_unit": i["unit"]}
           for e in entries for i in e["items"] if i["unit_mismatch"]]
    if bad:
        need["qty"] = {"items": bad, "how": "기준 단위로 센 수량을 물어보고 items [{name, qty}]로 다시 부를 것"}
    preview = [{"date": e["date"], "slot": e["slot"], "items": [{"name": i["name"], "qty": i["qty"], "known": i["known"]}
                                                                for i in e["items"]]} for e in entries]
    if need:
        return {"saved": [], "need": need, "preview": preview}
    saved = [svc.apply_entry(e, note="대화") for e in entries]
    return {"saved": [_meal(svc.meal(s["meal_id"])) for s in saved],
            "filled_existing": sum(1 for s in saved if s["filled"])}


def _update(svc: MealService, a: dict) -> dict:
    changes = {k: a[k] for k in ("people", "slot", "excluded") if k in a}
    if "items" in a:
        changes["items"] = [{"name": str(i["name"]), "qty": float(i.get("qty", 1)), "kcal": None} for i in a["items"]]
    if "slot" in changes and changes["slot"] not in slots.SLOTS:
        raise Invalid("slot은 아침·점심·저녁·간식·야식·장보기")
    if not changes:
        raise Invalid("바꿀 것(items·people·slot·excluded)이 없음")
    return _meal(svc.change_meal(int(a["meal_id"]), changes))


def _menu_info(svc: MealService, a: dict) -> dict:
    from meal_log import analysis, chat

    menus = analysis.all_menus(svc.store)
    name = chat.match_menu(str(a.get("name") or ""), menus)
    m = menus.get(name)
    if m:
        return {"known": True, "name": m.name, "unit": m.unit, "low": m.low, "mid": m.mid, "high": m.high,
                "source": menu_kcal.SOURCE_WORD[m.source]}
    # compose: 이름을 풀어 근거와 함께 계산한 추정 (있으면 이걸 먼저 말하고, 저장은 사용자 동의 후 save_menu basis에 그대로)
    return {"known": False, "name": name, "compose": svc.compose_view(str(a.get("name") or ""), menus),
            "similar": svc.similar(name, menus)}


def _save_menu(svc: MealService, a: dict) -> dict:
    out = svc.put_menu(str(a.get("name") or ""), unit=str(a.get("unit") or "1인분"), low=int(a["low"]),
                       mid=int(a["mid"]) if a.get("mid") is not None else None, high=int(a["high"]),
                       source="user" if a.get("from_user") else "calc" if a.get("from_compose") else "llm",
                       basis=str(a.get("basis") or "")[:200] or None)
    return {"saved": out["menu"]["name"], "unit": out["menu"]["unit"], "low": out["menu"]["low"], "high": out["menu"]["high"]}


def _weekly(svc: MealService, a: dict) -> dict:
    w = svc.weekly(None, max(1, min(int(a.get("weeks") or 4), 12)))
    return {"weeks": [{k: x[k] for k in ("week_start", "avg_kcal", "kcal_days", "partial_days", "meals", "yasik_days", "unknown")}
                      for x in w["weeks"]],
            "note": "avg_kcal은 끼니 kcal를 모두 아는 날(kcal_days)만의 하루 평균 (1인 추정)"}


def _pending(svc: MealService, a: dict) -> dict:
    r = svc.review()
    # suggested: 경험 기억 제안 (지난번 같은 결제로 확정된 메뉴) -- 메뉴·횟수만, 가게·금액은 뺀다 (식단만 규칙)
    return {"count": r["count"], "meals": [{"id": m["id"], "date": m["date"], "slot": m["slot"], "time": m["time"],
                                            "reasons": m["reasons"],
                                            "suggested": [{"items": s["items"], "times": s["times"], "of": s["of"]}
                                                          for s in m.get("suggest", [])]} for m in r["rows"][:30]],
            "note": "suggested는 같은 결제로 전에 확정된 메뉴 -- 같은 주문이라는 보장은 없으니 사용자에게 확인받고 update_meal"}


_ITEMS = {"type": "array", "items": {"type": "object", "required": ["name"],
                                     "properties": {"name": {"type": "string"}, "qty": {"type": "number"}}}}
TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("query_meals", "기간(최대 31일)의 날짜별 끼니·메뉴·kcal. 날짜는 YYYY-MM-DD, 없으면 최근 7일",
         {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}}, _query),
    Tool("log_meal", "먹은 것을 한국어 문장 그대로 기록. 끼니·수량이 모자라면 저장하지 않고 need를 돌려줌 -> 사용자에게 묻고 slot·items를 더해 다시 부름",
         {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}, "date": {"type": "string"},
                                                               "slot": {"type": "string", "enum": list(slots.EATING)},
                                                               "items": _ITEMS}}, _log),
    Tool("update_meal", "기록된 끼니 고치기: 메뉴 목록(items, 통째로 바꿈)·인원·끼니·먹지 않음(excluded)",
         {"type": "object", "required": ["meal_id"], "properties": {
             "meal_id": {"type": "integer"}, "items": _ITEMS, "people": {"type": "integer"},
             "slot": {"type": "string", "enum": list(slots.SLOTS)}, "excluded": {"type": "boolean"}}}, _update),
    Tool("menu_info", "메뉴 하나의 kcal 기준값(단위·범위·출처). 모르면 비슷한 기준 메뉴",
         {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}, _menu_info),
    Tool("save_menu", "기준표에 없는 메뉴의 kcal를 내 메뉴로 저장. 사용자가 숫자를 말했으면 from_user=true, menu_info의 compose 값이면 "
         "from_compose=true + basis=compose.basis, 그 밖의 추정이면 basis에 근거",
         {"type": "object", "required": ["name", "unit", "low", "high"], "properties": {
             "name": {"type": "string"}, "unit": {"type": "string"}, "low": {"type": "integer"}, "mid": {"type": "integer"},
             "high": {"type": "integer"}, "from_user": {"type": "boolean"}, "from_compose": {"type": "boolean"},
             "basis": {"type": "string"}}}, _save_menu),
    Tool("weekly", "최근 주(월요일 시작)별 하루 평균 kcal·야식한 날·kcal 모르는 끼니",
         {"type": "object", "properties": {"weeks": {"type": "integer"}}}, _weekly),
    Tool("pending_meals", "메뉴를 아직 모르는 등 확인이 필요한 끼니 목록 (오래된 것부터)",
         {"type": "object", "properties": {}}, _pending),
)}


def run_tool(svc: MealService, name: str, args: dict) -> Any:
    tool = TOOLS.get(name)
    if tool is None:
        raise Invalid(f"없는 도구: {name} ({', '.join(TOOLS)})")
    try:
        return tool.run(svc, args or {})
    except (KeyError, TypeError, ValueError) as exc:
        raise Invalid(f"{name} 인자가 이상함: {exc}") from exc
