"""경험 기억 (지난번 같은 결제 → 메뉴 제안) API 테스트. 결제·가게·메뉴는 전부 지어낸 값."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from meal_log.app import create_app
from tests.fake_card import FakeCard, row

ROWS = [
    row("2026-11-01", "19:30", "우아한형제들", 21000, "배달"),
    row("2026-11-03", "19:40", "우아한형제들", 21000, "배달"),
    row("2026-11-05", "20:00", "우아한형제들", 21000, "배달"),
    row("2026-11-06", "19:00", "우아한형제들", 23000, "배달"),     # 금액 다름 -> 제안 없음
    row("2026-11-07", "19:10", "쿠팡이츠", 21000, "배달"),         # 앱 다름 -> 제안 없음
    row("2026-11-02", "12:10", "다라식당", 9000, "외식"),
    row("2026-11-04", "12:20", "다라식당", 11000, "외식"),          # 같은 가게, 다른 금액 -> same_place
    row("2026-11-08", "12:30", "다라식당", 9000, "외식"),
    row("2026-11-09", "12:00", "가나김밥", 4500),                  # 규칙 추정(김밥)만 -- 기억 재료 아님
    row("2026-11-10", "12:00", "가나김밥", 4500),
]


def make(tmp_path, name="m.db"):
    app = create_app(tmp_path / name, source=FakeCard(list(ROWS)).source)
    c = TestClient(app)
    c.post("/api/sync")
    return c


def meal(c, day, place=None):
    ms = [m for m in c.get("/api/meals", params={"start": day, "end": day}).json() if place is None or m["place"] == place]
    assert len(ms) == 1, ms
    return ms[0]


def put(c, m, **changes):
    r = c.put(f"/api/meals/{m['id']}", json=changes)
    assert r.status_code == 200, r.text
    return r.json()


def test_no_memory_no_suggestion(tmp_path):
    c = make(tmp_path)
    m = meal(c, "2026-11-03")
    assert m["items"] == [] and m["suggest"] == []                     # 확정한 끼니가 없으면 제안도 없음


def test_delivery_same_amount_suggests_last_order(tmp_path):
    c = make(tmp_path)
    put(c, meal(c, "2026-11-01"), items=[{"name": "치킨", "qty": 1}, {"name": "콜라 1.25L", "qty": 1}], store="가나다치킨 역삼점")
    s = meal(c, "2026-11-03")["suggest"]
    assert len(s) == 1 and s[0]["items"] == [{"name": "치킨", "qty": 1}, {"name": "콜라 1.25L", "qty": 1}]
    assert s[0]["store"] == "가나다치킨 역삼점" and s[0]["times"] == 1 and s[0]["of"] == 1 and s[0]["basis"] == "same_amount"
    assert meal(c, "2026-11-06")["suggest"] == []                      # 금액 다름
    assert meal(c, "2026-11-07")["suggest"] == []                      # 앱 다름
    assert "suggest" not in meal(c, "2026-11-01")                      # 메뉴 있는 끼니엔 안 붙음
    # 확인 목록(확인 흐름)에도 같은 제안
    row_ = next(r for r in c.get("/api/review").json()["rows"] if r["date"] == "2026-11-03")
    assert row_["suggest"][0]["signature"] == s[0]["signature"]


def test_accept_then_ranking_by_times(tmp_path):
    c = make(tmp_path)
    put(c, meal(c, "2026-11-01"), items=[{"name": "치킨", "qty": 1}])
    put(c, meal(c, "2026-11-03"), items=[{"name": "피자", "qty": 1}])
    s = meal(c, "2026-11-05")["suggest"]
    assert [x["items"][0]["name"] for x in s] == ["피자", "치킨"]        # 같은 횟수면 최근 것 먼저
    put(c, meal(c, "2026-11-05"), items=[{"name": "치킨", "qty": 1}])     # [넣기] = 메뉴 넣기 -> 이제 기억 재료
    put(c, meal(c, "2026-11-03"), items=[])                             # 메뉴를 다 빼면 재료가 아님
    m3 = meal(c, "2026-11-03")
    assert [(x["items"][0]["name"], x["times"], x["of"]) for x in m3["suggest"]] == [("치킨", 2, 2)]


def test_place_memory_prefers_same_amount(tmp_path):
    c = make(tmp_path)
    put(c, meal(c, "2026-11-02"), items=[{"name": "김치찌개 정식", "qty": 1}])
    put(c, meal(c, "2026-11-04"), items=[{"name": "제육볶음", "qty": 1}])
    s = meal(c, "2026-11-08")["suggest"]
    assert [(x["items"][0]["name"], x["basis"]) for x in s] == [("김치찌개 정식", "same_amount"), ("제육볶음", "same_place")]


def test_rule_guess_and_excluded_are_not_memory(tmp_path):
    c = make(tmp_path)
    assert [i["name"] for i in meal(c, "2026-11-09")["items"]] == ["김밥"]   # 규칙 추정
    assert "suggest" not in meal(c, "2026-11-10")                          # 규칙 추정이 있으니 제안 대상 아님
    src = meal(c, "2026-11-01")
    put(c, src, items=[{"name": "치킨", "qty": 1}])
    put(c, src, excluded=True)                                            # 내가 먹은 게 아님 -> 재료에서 빠짐
    assert meal(c, "2026-11-03")["suggest"] == []


def test_derived_memory_follows_undo(tmp_path):
    c = make(tmp_path)
    src = meal(c, "2026-11-01")
    put(c, src, items=[{"name": "치킨", "qty": 1}])
    assert meal(c, "2026-11-03")["suggest"]
    put(c, src, items=[], confirmed=False)                              # 되돌리기 (화면이 보내는 모양)
    assert meal(c, "2026-11-03")["suggest"] == []                       # 저장된 기억이 없으니 저절로 사라짐


def test_reject_is_per_meal_idempotent_undoable_and_persists(tmp_path):
    c = make(tmp_path)
    put(c, meal(c, "2026-11-01"), items=[{"name": "치킨", "qty": 1}])
    m3 = meal(c, "2026-11-03")
    sig = m3["suggest"][0]["signature"]
    for _ in range(2):                                                   # 두 번 눌러도 같음
        r = c.post(f"/api/meals/{m3['id']}/suggestions/reject", json={"signature": sig})
        assert r.status_code == 200 and r.json()["suggest"] == []
    assert meal(c, "2026-11-05")["suggest"][0]["signature"] == sig       # 다른 끼니에는 그대로
    c2 = make(tmp_path)                                                  # 재시작
    assert meal(c2, "2026-11-03")["suggest"] == []
    r = c2.delete(f"/api/meals/{m3['id']}/suggestions/reject", params={"signature": sig})
    assert r.status_code == 200 and r.json()["suggest"][0]["signature"] == sig
    assert c2.post("/api/meals/99999/suggestions/reject", json={"signature": sig}).status_code == 404
    assert c2.post(f"/api/meals/{m3['id']}/suggestions/reject", json={"signature": ""}).status_code == 422


def test_assistant_sees_suggested_menus_only(tmp_path):
    c = make(tmp_path)
    put(c, meal(c, "2026-11-01"), items=[{"name": "치킨", "qty": 1}], store="가나다치킨 역삼점")
    out = c.post("/api/assistant/tools/pending_meals", json={}).json()
    row_ = next(m for m in out["meals"] if m["date"] == "2026-11-03")
    assert row_["suggested"] == [{"items": [{"name": "치킨", "qty": 1}], "times": 1, "of": 1}]
    text = json.dumps(out, ensure_ascii=False)
    for word in ("가나다치킨", "store", "21000", "우아한형제들", "amount", "place"):
        assert word not in text, word
    q = json.dumps(c.post("/api/assistant/tools/query_meals", json={"start": "2026-11-03", "end": "2026-11-03"}).json(),
                   ensure_ascii=False)
    assert "가나다치킨" not in q and "suggest" not in q
