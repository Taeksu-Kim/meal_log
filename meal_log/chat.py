"""채팅 입력 해석: "오늘 아침에 고구마 1개 먹었어" -> [{날짜, 끼니, 메뉴·수량}]. 저장은 하지 않는다 (화면이 보여 주고 사람이 넣는다).

- **규칙 먼저** (코드): 날짜 말(오늘·어제·그제·n월 n일·n/n), 끼니 말 하나, "메뉴 n개/그릇/반" 목록.
  끼니 말이 여럿이거나 요일·애매한 말이 있거나 메뉴를 깔끔히 못 떼어 내면 규칙은 포기한다 (None).
- **못 하면 로컬 LLM** (제안만): 보내는 것은 그 문장과 기준표 메뉴 이름 목록뿐 (금액·가게·카드 정보 안 보냄). 답은 JSON 스키마로 고정,
  날짜·수량 범위를 코드로 다시 검사한다.
- 날짜를 말하지 않으면 ``base``(화면의 날짜 칸, 기본 오늘). 끼니를 말하지 않으면 None -- 화면에서 고르게 한다 (지어내지 않음).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Collection, Literal, Optional

from meal_log import menu_kcal, slots
from meal_log.llm import LLMDown, LocalLLM, ask_json

Via = Literal["rules", "llm"]
UNKNOWN_SLOT = "모름"
MAX_TEXT = 300
MAX_QTY = 20


@dataclass(frozen=True)
class Item:
    name: str
    qty: float = 1.0
    unit: str = ""   # 사용자가 말한 단위 ("조각"). 기준표 단위("라지 1판")와 다르면 화면이 수량 확인을 요청한다


@dataclass
class Entry:
    date: date
    slot: Optional[str]          # None = 문장에 끼니가 없음 (화면에서 고름)
    items: list[Item] = field(default_factory=list)


@dataclass
class Parsed:
    via: Via
    entries: list[Entry]
    seconds: float = 0.0
    loose: bool = False   # LLM이 꺼져 있어 규칙을 느슨하게 적용한 결과 (화면이 확인을 더 강하게 요청)


# ---------------------------------------------------------------- 규칙
_REL_DAYS = {"오늘": 0, "금일": 0, "어제": -1, "어저께": -1, "그제": -2, "그저께": -2, "엊그제": -2}
_SLOT_WORDS: tuple[tuple[str, str], ...] = (
    (r"아침|조식", "아침"), (r"점심|런치", "점심"), (r"저녁|석식|디너", "저녁"),   # "중식"은 중국 음식과 헷갈려 뺌
    (r"간식|디저트|후식", "간식"), (r"야식|밤참", "야식"))
_DEFER = re.compile(r"아점|브런치|[월화수목금토일]요일|지난\s*주|저번\s*주|지지난|그끄제|빼고|말고|대신|반만|남겼")
_PARTICLE_AFTER = r"\s*(?:에는|에도|에|엔|때|땐|으로|로|은|는|도)?"
_VERB = re.compile(r"\s*(?:을|를)?\s*(?:먹었(?:어요|어|다|음|습니다|네|지)?|먹음|먹고|마셨(?:어요|어|다|음|습니다)?|마심|"
                   r"했(?:어요|어|다|음)?|함|시켜\s*먹었\w*|해치웠\w*|이?었(?:어요|어|다|음)|이?였(?:어요|어|다|음))\s*[.!~ㅎㅋ]*\s*$")
_FILLER = re.compile(r"(?:^|\s)(?:아까|방금|좀|대충|그냥|약|한\s*번|같이|함께|저|그)(?=\s|$)")
_PRONOUN = re.compile(r"^(?:이거|그거|저거|요거|이것|그것|저것)$")   # "이거랑 양파" -> 가리키는 말은 메뉴가 아님
_TOPIC = re.compile(r"(?<=[가-힣]{2})(?:은|는)$")                    # "스테이크는" -> 스테이크
_SEP = re.compile(r",|，|그리고|이랑|하고|랑|&|\+|/|및")
_NUM_WORDS = {"한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "네": 4, "넷": 4, "다섯": 5, "반": 0.5}
_UNITS = r"개|그릇|잔|줄|인분|조각|공기|마리|판|캔|병|봉지|봉|팩|묶음|단|송이|포기|모|컵|접시|장|알|쪽|숟가락|스푼|입"
# 같은 양을 가리키는 단위 (기준표 단위와 말한 단위를 맞춰 볼 때). 확실한 것만
UNIT_SAME = (("봉", "봉지", "묶음", "팩"), ("개", "알"), ("잔", "컵"))
# 수량: 숫자는 붙여 써도 되지만 숫자 말(한·두·반)은 앞에 띄어쓰기가 있어야 한다 ("만두"가 "만" × 2가 되지 않게)
_QTY_TAIL = re.compile(rf"^(?P<name>.*?)(?:\s*(?P<d>\d+(?:\.\d+)?)|\s+(?P<w>{'|'.join(_NUM_WORDS)}))\s*(?P<u>{_UNITS})?\s*$")
_OBJ = re.compile(r"(?<=[가-힣])(?:을|를)$")
# 사람들이 흔히 다르게 부르는 이름 -> 기준표 이름 (확실한 것만)
ALIASES = {"짜장": "짜장면", "자장면": "짜장면", "돈가스": "돈까스", "카레": "카레라이스", "라떼": "카페라떼",
           "쌀밥": "공기밥", "밥 한 공기": "공기밥", "순댓국": "순대국", "국밥": "돼지국밥", "햄버거세트": "햄버거 세트"}
MAX_NAME = 12   # 규칙으로 뗀 이름이 이보다 길면 문장을 제대로 못 자른 것으로 보고 LLM에 넘긴다


def canonical(name: str, menus: Collection[str] = menu_kcal.MENUS) -> str:
    n = re.sub(r"\s+", " ", name).strip()
    return n if n in menus else ALIASES.get(n, ALIASES.get(n.replace(" ", ""), n))


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)}


FUZZY_MIN = 0.6   # 두 글자 조각 겹침 비율 ("양념 닭다리 스테이크" vs "닭다리양념스테이크" = 0.60)


def match_menu(name: str, menus: Collection[str] = menu_kcal.MENUS) -> str:
    """사용자가 말한·영수증에 적힌 이름 -> 아는 메뉴 이름. 같음 -> 별칭 -> 띄어쓰기 무시 같음 -> 조각 겹침(하나만 넘을 때).
    못 맞추면 정리한 이름 그대로 (kcal 모름)."""
    n = canonical(name, menus)
    if n in menus:
        return n
    key = n.replace(" ", "")
    same = [m for m in menus if m.replace(" ", "") == key]
    if len(same) == 1:
        return same[0]
    if len(key) >= 4:
        q = _bigrams(key)
        scored = sorted(((len(q & b) / len(q | b), m) for m in menus if (b := _bigrams(m.replace(" ", "")))), reverse=True)
        if scored and scored[0][0] >= FUZZY_MIN and (len(scored) == 1 or scored[1][0] < FUZZY_MIN):
            return scored[0][1]
    return n


def unit_ok(said: str, table_unit: str) -> bool:
    """말한 단위가 기준표 단위와 같은 양인가 ("묶음" vs "1봉(약 150g)" = 같음, "조각" vs "라지 1판" = 다름)."""
    if not said or said in table_unit:
        return True
    return any(said in group and any(u in table_unit for u in group) for group in UNIT_SAME)


def _find_date(text: str, today: date) -> tuple[Optional[date], int]:
    """(날짜, 날짜 표현 개수). 표현이 여럿이면 규칙은 포기한다."""
    found: list[date] = []
    for w, off in _REL_DAYS.items():
        if re.search(w, text):
            found.append(today + timedelta(days=off))
    for m in re.finditer(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일|(?<![\d.])(\d{1,2})/(\d{1,2})(?![\d/])", text):
        mo, d = (int(m[1]), int(m[2])) if m[1] else (int(m[3]), int(m[4]))
        try:
            dt = date(today.year, mo, d)
        except ValueError:
            return None, 99
        found.append(dt if dt <= today else dt.replace(year=today.year - 1))
    uniq = sorted(set(found))
    return (uniq[0] if uniq else None), len(uniq)


def parse_rules(text: str, today: date, base: date, menus: Collection[str] = menu_kcal.MENUS, *,
                strict: bool = True) -> Optional[Parsed]:
    """``menus``: 메뉴 이름 (기준표 + 사용자 메뉴). strict면 여러 단어 이름은 아는 메뉴일 때만 받는다 (덜 잘린 말은 LLM에).
    strict=False는 LLM이 꺼졌을 때의 물러서기 -- 여러 단어 이름도 그대로 받는다 ("팽이 버섯")."""
    t = re.sub(r"\s+", " ", text).strip()
    if not t or _DEFER.search(t):
        return None
    day, n_dates = _find_date(t, today)
    if n_dates > 1:
        return None
    found_slots = {s for pat, s in _SLOT_WORDS if re.search(pat, t)}
    if len(found_slots) > 1:
        return None
    rest = t
    for w in _REL_DAYS:
        rest = re.sub(w + _PARTICLE_AFTER, " ", rest)
    rest = re.sub(r"\d{1,2}\s*월\s*\d{1,2}\s*일" + _PARTICLE_AFTER + r"|(?<![\d.])\d{1,2}/\d{1,2}(?![\d/])" + _PARTICLE_AFTER, " ", rest)
    for pat, _ in _SLOT_WORDS:
        rest = re.sub(f"(?:{pat})" + r"(?:\s*(?:밥|식사))?(?:으?로)?" + _PARTICLE_AFTER, " ", rest)
    rest = _FILLER.sub(" ", _VERB.sub("", rest)).strip()
    items: list[Item] = []
    for chunk in _SEP.split(rest):
        c = _TOPIC.sub("", chunk.strip()).strip()
        if not c or _PRONOUN.match(c):
            continue
        qty, unit = 1.0, ""
        m = _QTY_TAIL.match(c)
        if m and m["name"].strip():
            qty = float(m["d"]) if m["d"] else float(_NUM_WORDS[m["w"]])
            unit = m["u"] or ""
            c = m["name"].strip()
        c = match_menu(_TOPIC.sub("", _OBJ.sub("", c).strip()).strip(), menus)
        if not c or len(c) > MAX_NAME or re.search(r"\d", c) or not (0 < qty <= MAX_QTY):
            return None
        if strict and " " in c and c not in menus:   # "중식으로 짬뽕"처럼 덜 잘린 말 -> LLM에 맡긴다
            return None
        items.append(Item(c, qty, unit))
    if not items:
        return None
    return Parsed("rules", [Entry(day or base, found_slots.pop() if found_slots else None, items)])


# ---------------------------------------------------------------- 로컬 LLM
DOW = "월화수목금토일"
SYSTEM = (
    "사용자가 먹은 것을 적은 한국어 문장을 식사 기록으로 바꾼다. 문장에 있는 것만 쓰고 지어내지 않는다. "
    "먹은 내용이 없으면 entries를 비운다. 끼니(아침·점심·저녁·간식·야식)가 다르면 entry를 나눈다.\n"
    "- date: YYYY-MM-DD. 오늘은 {today}({dow}). 날짜를 말하지 않으면 {base}. '어제'·'그제'·요일은 계산하지 말고 "
    "아래 최근 날짜 표에서 고른다 (요일만 말하면 표 안의 그 요일 = 오늘 이전의 가장 가까운 날)\n최근 날짜: {recent}\n"
    "- slot: 아침·점심·저녁·간식·야식 중 하나. 아점·브런치는 점심. 문장으로 알 수 없으면 '모름'\n"
    "- items: 먹은 음식마다 하나. name은 아래 메뉴 목록에 같은 음식이 있으면 목록의 이름을 그대로, 없으면 사용자가 말한 음식 이름"
    "(조사·동사·수량 빼고). qty는 몇 개·몇 그릇인지 숫자 (반 = 0.5, 말하지 않으면 1), unit은 사용자가 말한 단위(개·조각·그릇 등, "
    "말하지 않았으면 빈 문자열). '빼고'·'말고'로 뺀 음식은 넣지 않는다. 가게·배달앱 이름(쿠팡이츠 등)은 음식이 아니다"
)


def _schema() -> dict:
    return {"type": "object", "required": ["entries"], "properties": {"entries": {"type": "array", "items": {
        "type": "object", "required": ["date", "slot", "items"], "properties": {
            "date": {"type": "string"},
            "slot": {"type": "string", "enum": [*slots.EATING, UNKNOWN_SLOT]},
            "items": {"type": "array", "items": {"type": "object", "required": ["name", "qty", "unit"], "properties": {
                "name": {"type": "string"}, "qty": {"type": "number"}, "unit": {"type": "string"}}}}}}}}}


def _validate(data: object, today: date, menus: Collection[str]) -> list[Entry]:
    from agent.core.llm import OutputError

    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list) or len(entries) > 6:
        raise OutputError("entries 목록이 없거나 너무 많음")
    out = []
    for e in entries:
        try:
            d = date.fromisoformat(str(e["date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise OutputError(f"date 형식이 YYYY-MM-DD가 아님: {e!r}") from exc
        if not (today - timedelta(days=366) <= d <= today):
            raise OutputError(f"date가 오늘({today}) 이후이거나 1년보다 전: {d}")
        slot = e.get("slot")
        if slot not in (*slots.EATING, UNKNOWN_SLOT):
            raise OutputError(f"slot이 목록에 없음: {slot!r}")
        items = []
        for it in e.get("items") or []:
            name, qty, unit = str(it.get("name", "")).strip(), it.get("qty", 1), str(it.get("unit") or "").strip()
            if not name or len(name) > 60 or not isinstance(qty, (int, float)) or not (0 < qty <= MAX_QTY) or len(unit) > 8:
                raise OutputError(f"item 이름·수량·단위가 이상함: {it!r}")
            items.append(Item(match_menu(name, menus), float(qty), unit))
        if items:
            out.append(Entry(d, None if slot == UNKNOWN_SLOT else slot, items))
    return out


def parse_llm(llm: LocalLLM, text: str, today: date, base: date, menus: Collection[str] = menu_kcal.MENUS) -> Parsed:
    import json

    # LLM은 날짜 계산이 약하다 (지난 월요일을 다음 월요일로 계산, 2026-11-15 실측) -> 최근 7일 표를 주고 고르게 한다
    recent = ", ".join(f"{(today - timedelta(days=i)).isoformat()}({DOW[(today - timedelta(days=i)).weekday()]}"
                       f"{', 오늘' if i == 0 else ', 어제' if i == 1 else ', 그제' if i == 2 else ''})" for i in range(6, -1, -1))
    system = SYSTEM.format(today=today.isoformat(), dow=DOW[today.weekday()], base=base.isoformat(), recent=recent)
    user = "메뉴 목록: " + ", ".join(menus) + "\n\n문장: " + json.dumps(text, ensure_ascii=False)
    started = time.monotonic()
    entries = ask_json(llm, system, user, _schema(), lambda d: _validate(d, today, menus), what="meal entries")
    return Parsed("llm", entries, round(time.monotonic() - started, 1))


def parse(text: str, today: date, base: date, llm_factory, *, force_llm: bool = False,
          menus: Collection[str] = menu_kcal.MENUS) -> Parsed:
    """규칙 -> (못 하거나 force_llm이면) LLM. ``llm_factory()``는 클라이언트를 돌려주거나 llm.LLMDown을 던진다."""
    if not force_llm:
        got = parse_rules(text, today, base, menus)
        if got is not None:
            return got
    try:
        return parse_llm(llm_factory(), text, today, base, menus)
    except LLMDown:
        loose = None if force_llm else parse_rules(text, today, base, menus, strict=False)
        if loose is None:
            raise
        loose.loose = True
        return loose
