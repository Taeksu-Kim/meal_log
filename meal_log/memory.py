"""경험 기억: 사람이 메뉴를 확정한 카드 끼니 -> 메뉴 모르는 카드 끼니에 "지난번 같은 금액/가게" 제안.

사용자 결정 (2026-11-15):
- **제안만** -- 자동으로 채우지 않는다. 사람이 [넣기]를 눌러야 반영, [틀림]이면 그 끼니에 그 제안을 다시 띄우지 않는다.
- **저장하지 않고 확정된 기록에서 매번 계산** -- 끼니를 고치거나 되돌리면 기억도 저절로 맞다. 따로 저장하는 건 [틀림] 목록뿐.

열쇠 (배달 결제의 결제 이름은 배달앱 이름뿐이라 실제 가게를 모른다):
- 배달 = (결제 이름, 금액). 같은 앱·같은 금액 주문은 흔히 되풀이되지만 같은 주문이라는 보장은 없다 -> 제안만.
- 그 밖(외식·카페·편의점) = 결제 이름이 곧 가게. 같은 금액이면 그걸 먼저, 아니면 그 가게에서 자주 먹은 것.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Literal, Optional

from meal_log import slots

MAX_SUGGEST = 2
Basis = Literal["same_amount", "same_place"]


@dataclass(frozen=True)
class Suggestion:
    signature: str                         # 메뉴 묶음 표식 ("치킨×0.5|콜라 1.25L×1") -- [틀림] 기록의 열쇠
    items: tuple[tuple[str, float], ...]   # (메뉴, 수량)
    store: Optional[str]                   # 영수증으로 알게 된 실제 가게 (배달) -- 화면에만, 대화 도우미에는 안 줌
    times: int                             # 같은 열쇠에서 이 메뉴로 확정된 횟수
    of: int                                # 같은 열쇠에서 확정된 전체 횟수 (다른 메뉴 포함)
    last: str                              # 마지막으로 확정된 날짜
    basis: Basis


def signature(items: Iterable[tuple[str, float]]) -> str:
    return "|".join(f"{n}×{q:g}" for n, q in sorted(items))


def learned(meals: list[dict]) -> list[dict]:
    """기억의 재료: 사람이 메뉴를 넣은(menu_user) 카드 끼니, 메뉴가 있고 제외·건너뜀이 아닌 것."""
    return [m for m in meals if m["source"] == "card" and m["menu_user"] and m["items"]
            and not m["excluded"] and not m["skipped"] and m["slot"] in slots.EATING]


def wants_suggestion(m: dict) -> bool:
    """제안을 붙일 끼니: 메뉴를 모르는 카드 끼니 (규칙 추정이 붙은 끼니는 이미 메뉴가 있으니 제외)."""
    return (m["source"] == "card" and not m["items"] and not m["excluded"] and not m["skipped"]
            and m["slot"] in slots.EATING)


class Memory:
    """확정된 끼니 목록으로 한 번 묶어 두고 여러 끼니에 제안 (요청 한 번에 한 번 만든다)."""

    def __init__(self, meals: list[dict]) -> None:
        self.by_place: dict[str, list[dict]] = defaultdict(list)
        for m in learned(meals):
            self.by_place[m["place"]].append(m)

    def suggest(self, meal: dict, rejected: Iterable[str] = ()) -> list[Suggestion]:
        pool = [x for x in self.by_place.get(meal["place"], []) if x["id"] != meal["id"]]
        same_amount = [x for x in pool if x["amount"] == meal["amount"]]
        if meal["category"] == "배달":
            groups = [(same_amount, "same_amount")]
        else:
            groups = [(same_amount, "same_amount"), ([x for x in pool if x["amount"] != meal["amount"]], "same_place")]
        out: list[Suggestion] = []
        seen = set(rejected)
        for ms, basis in groups:
            by_sig: dict[str, list[dict]] = defaultdict(list)
            for x in ms:
                by_sig[signature((i["name"], i["qty"]) for i in x["items"])].append(x)
            # 많이 확정된 것 먼저, 같으면 최근 것
            ranked = sorted(by_sig.items(), key=lambda kv: (len(kv[1]), max(x["date"] for x in kv[1])), reverse=True)
            for sig, xs in ranked:
                if sig in seen:
                    continue
                seen.add(sig)
                latest = max(xs, key=lambda x: (x["date"], x["time"] or ""))
                out.append(Suggestion(signature=sig, items=tuple((i["name"], i["qty"]) for i in latest["items"]),
                                      store=next((x["store"] for x in sorted(xs, key=lambda x: x["date"], reverse=True)
                                                  if x["store"]), None),
                                      times=len(xs), of=len(ms), last=latest["date"], basis=basis))
                if len(out) >= MAX_SUGGEST:
                    return out
        return out
