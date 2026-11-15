"""card_spending에서 식비 결제 가져오기 (DESIGN.md 6절 방식 A: card_spending API 호출).

계약 (card_spending ``GET /api/transactions?majors=식비`` 행): date, time, name, raw, krw, usd, amount, method, category,
source, merchant_key. 취소된 결제는 card_spending이 이미 뺀다.

- 응답이 한 번에 1,000건에서 잘리므로(``truncated``) 달마다 나눠 받는다 (``GET /api/period``로 기간을 먼저).
- 응답 행에 결제 고유 id가 없다 → 날짜·시간·원래 이름·금액·결제방법으로 키를 만들고, 완전히 같은 결제가 여럿이면 순번을 붙인다.
  card_spending이 나중에 ``id``를 주면 그것을 쓴다. (한계: card_spending에서 시간·금액이 갱신되면 다른 결제로 보여
  옛 끼니는 지워지고 새로 생긴다 -- 그 끼니에 고친 메뉴도 같이 사라짐.)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Optional, Protocol

import httpx

FOOD_MAJOR = "식비"


@dataclass(frozen=True)
class CardTxn:
    txn_id: str
    pay_date: date
    pay_time: Optional[str]
    name: str           # card_spending이 정리한 사용처 이름
    amount: int         # 원 (해외 결제는 card_spending 환율로 환산된 값, 환율이 없으면 원화 부분만)
    minor: str          # 식비 소분류 (배달·외식·카페·간식·편의점·마트·장보기·사용자 소분류)


@dataclass(frozen=True)
class Fetched:
    start: Optional[date]   # card_spending에 저장된 기간 -- 이 안에서 사라진 결제 = 취소된 결제
    end: Optional[date]
    txns: list[CardTxn]


class CardSourceDown(Exception):
    """card_spending 서버에 연결할 수 없음 (꺼져 있음)."""


class CardSourceError(Exception):
    """연결은 됐지만 응답이 예상과 다름."""


class CardSource(Protocol):
    url: str

    def fetch(self) -> Fetched: ...


def _months(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def txn_key(row: dict) -> str:
    base = "|".join(str(row.get(k, "")) for k in ("date", "time", "raw", "krw", "usd", "method"))
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def to_txns(rows: list[dict]) -> list[CardTxn]:
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        try:
            if r.get("id"):
                tid = str(r["id"])
            else:
                k = txn_key(r)
                seen[k] = seen.get(k, 0) + 1
                tid = f"{k}#{seen[k]}"
            cat = str(r.get("category") or "")
            out.append(CardTxn(txn_id=tid, pay_date=date.fromisoformat(r["date"]), pay_time=r.get("time") or None,
                               name=str(r.get("name") or r.get("raw") or ""), amount=round(float(r["amount"])),
                               minor=cat.split(" › ", 1)[1] if " › " in cat else cat))
        except (KeyError, TypeError, ValueError) as exc:
            raise CardSourceError(f"card_spending 응답 행 형식이 예상과 다름: {exc!r}") from exc
    return out


class HttpCardSource:
    """``client``: 테스트용 주입 (가짜 card_spending 앱의 TestClient). 없으면 실제 HTTP."""

    def __init__(self, url: str = "http://127.0.0.1:8765", client: Optional[httpx.Client] = None,
                 timeout: float = 10.0) -> None:
        self.url = url.rstrip("/")
        self._client = client
        self._timeout = timeout

    def _get(self, client: httpx.Client, path: str, params: Optional[dict] = None) -> dict:
        try:
            r = client.get(f"{self.url}{path}", params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise CardSourceDown(f"card_spending 서버({self.url})에 연결할 수 없음") from exc
        except httpx.HTTPError as exc:
            raise CardSourceError(f"card_spending 요청 실패: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise CardSourceError(f"card_spending {path} 응답 {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise CardSourceError(f"card_spending {path} 응답이 JSON이 아님") from exc

    def fetch(self) -> Fetched:
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            period = self._get(client, "/api/period")
            if not period.get("start") or not period.get("end"):
                return Fetched(None, None, [])
            start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
            rows: list[dict] = []
            for month in _months(start, end):
                d = self._get(client, "/api/transactions", {"majors": FOOD_MAJOR, "month": month})
                if d.get("truncated"):
                    raise CardSourceError(f"{month} 식비 결제가 card_spending 한 번 응답 한도를 넘음 -- 나눠 받기 필요")
                rows += d.get("rows") or []
            return Fetched(start, end, to_txns(rows))
        finally:
            if self._client is None:
                client.close()
