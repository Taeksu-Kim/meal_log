"""가짜 card_spending 서버 -- 실제 card_spending과 같은 경로·필드(/api/period, /api/transactions)만 흉내낸다.
meal_log의 HttpCardSource를 진짜 HTTP 클라이언트 경로 그대로 태우려고 TestClient(=httpx.Client)로 붙인다.
가게 이름·금액은 전부 지어낸 값."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from meal_log.sources import HttpCardSource

FAKE_URL = "http://fake-card"


def row(d: str, t: str, name: str, amount: int, minor: str = "외식") -> dict:
    return {"date": d, "time": t, "name": name, "raw": name, "krw": amount, "usd": 0.0, "amount": amount,
            "method": "일시불", "category": f"식비 › {minor}", "source": "rule", "merchant_key": name}


class FakeCard:
    def __init__(self, rows: list[dict], period: tuple[str, str] | None = None, limit: int = 1000) -> None:
        self.rows = list(rows)
        self.period = period
        self.limit = limit
        self.calls: list[str] = []
        app = FastAPI()

        @app.get("/api/period")
        def period_() -> dict:
            self.calls.append("period")
            if self.period:
                s, e = self.period
            elif self.rows:
                s, e = min(r["date"] for r in self.rows), max(r["date"] for r in self.rows)
            else:
                s = e = None
            return {"start": s, "end": e, "next_from": None}

        @app.get("/api/transactions")
        def transactions(majors: str | None = None, month: str | None = None) -> dict:
            self.calls.append(f"tx:{month}")
            sel = [r for r in self.rows if (not majors or r["category"].split(" › ")[0] in majors.split("|"))
                   and (not month or r["date"][:7] == month)]
            sel.sort(key=lambda r: (r["date"], r["time"]), reverse=True)
            return {"count": len(sel), "total": sum(r.get("amount", 0) for r in sel), "usd_unconverted": 0.0,
                    "rows": sel[:self.limit], "truncated": len(sel) > self.limit}

        self.client = TestClient(app, base_url=FAKE_URL)
        self.source = HttpCardSource(FAKE_URL, client=self.client)
