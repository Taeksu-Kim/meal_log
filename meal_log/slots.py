"""끼니 나누기 규칙 (DESIGN.md 4절, 코드만 -- LLM 없음). 시간대는 설정으로 바꾼다 (store의 settings).

- 결제 시간이 들어간 시간대 = 끼니. 어느 시간대에도 없으면 간식.
- **하루의 시작 = 아침 시작 시각** (기본 05:00). 그보다 이른 결제는 전날 기록 (자정 넘은 야식 → 전날 야식).
- 카페·편의점(간식 범주)은 끼니 시간대여도 간식. 단 금액이 ``snack_meal_min_krw`` 이상이면 시간대 끼니로.
- 마트·장보기는 끼니가 아니라 "장보기" (먹은 끼니로 옮기는 건 사람이 함).
- 외식·배달·그 밖의 식비 소분류(card_spending에서 사용자가 더한 것 포함)는 시간대대로.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal, Optional

Slot = Literal["아침", "점심", "저녁", "간식", "야식", "장보기"]
SLOTS: tuple[Slot, ...] = ("아침", "점심", "저녁", "간식", "야식", "장보기")
EATING: tuple[Slot, ...] = ("아침", "점심", "저녁", "간식", "야식")   # 캘린더 칸 (장보기는 먹은 기록이 아님)
MEALS: tuple[Slot, ...] = ("아침", "점심", "저녁", "야식")            # 시간대가 있는 끼니

DEFAULT_WINDOWS: dict[str, tuple[str, str]] = {
    "아침": ("05:00", "10:29"), "점심": ("10:30", "14:59"), "저녁": ("17:00", "20:59"), "야식": ("21:00", "04:59")}
DEFAULT_SNACK_MEAL_MIN_KRW = 10_000   # 카페·편의점 결제가 이 이상이면 간식이 아니라 끼니로 (편의점 도시락+음료 정도)

Kind = Literal["meal", "snack", "grocery"]
# card_spending 식비 소분류 → 다루는 방식. 여기 없는 소분류(외식·배달·사용자 소분류)는 "meal" (시간대대로).
CATEGORY_KIND: dict[str, Kind] = {"마트·장보기": "grocery", "카페·간식": "snack", "편의점": "snack"}

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)")


@dataclass(frozen=True)
class SlotSettings:
    windows: dict[str, tuple[str, str]] = field(default_factory=lambda: dict(DEFAULT_WINDOWS))
    snack_meal_min_krw: int = DEFAULT_SNACK_MEAL_MIN_KRW


@dataclass(frozen=True)
class Placement:
    date: date
    slot: Slot


def minutes(hhmm: Optional[str]) -> Optional[int]:
    """'HH:MM' 또는 'HH:MM:SS' → 자정부터 분. 비었거나 형식이 다르면 None."""
    m = _HHMM.match((hhmm or "").strip())
    return int(m[1]) * 60 + int(m[2]) if m else None


def _covered(start: int, end: int) -> set[int]:
    """[start, end] 분 (end < start면 자정을 넘음)."""
    return set(range(start, end + 1)) if start <= end else set(range(start, 1440)) | set(range(0, end + 1))


def validate(s: SlotSettings) -> list[str]:
    errs = []
    if set(s.windows) != set(MEALS):
        return [f"시간대는 {', '.join(MEALS)} 네 개가 다 있어야 함"]
    spans = {}
    for slot in MEALS:
        a, b = (minutes(x) for x in s.windows[slot])
        if a is None or b is None:
            errs.append(f"{slot}: 시각은 HH:MM (예: 05:00)")
        else:
            spans[slot] = _covered(a, b)
    names = list(spans)
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            if spans[x] & spans[y]:
                errs.append(f"{x}·{y} 시간대가 겹침")
    if not (0 <= s.snack_meal_min_krw <= 1_000_000):
        errs.append("간식→끼니 기준 금액은 0~1,000,000원")
    return errs


def day_start(s: SlotSettings) -> int:
    return minutes(s.windows["아침"][0]) or 0


def time_slot(minute: int, s: SlotSettings) -> Slot:
    for slot in MEALS:
        a, b = (minutes(x) for x in s.windows[slot])
        if minute in _covered(a, b):
            return slot
    return "간식"


def place(pay_date: date, pay_time: Optional[str], minor: str, amount: float, s: SlotSettings) -> tuple[Placement, bool]:
    """결제 → (식단 날짜·끼니, 시간 없음 여부). 시간이 없으면 결제일 그대로, 끼니는 범주로만 (식사 범주는 '간식' + 확인 필요)."""
    kind = CATEGORY_KIND.get(minor, "meal")
    m = minutes(pay_time)
    if m is None:
        return Placement(pay_date, "장보기" if kind == "grocery" else "간식"), kind != "grocery"
    d = pay_date - timedelta(days=1) if m < day_start(s) else pay_date
    if kind == "grocery":
        return Placement(d, "장보기"), False
    slot = time_slot(m, s)
    if kind == "snack" and amount < s.snack_meal_min_krw:
        slot = "간식"
    return Placement(d, slot), False
