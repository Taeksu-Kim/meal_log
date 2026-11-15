"""API 테스트 (가짜 card_spending, 임시 DB). 진입점은 meal_log의 FastAPI 앱, 바깥(card_spending)만 가짜."""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from meal_log import menu_kcal
from meal_log.app import create_app
from meal_log.sources import HttpCardSource
from tests.fake_card import FakeCard, row


def make(tmp_path, rows, **kw):
    fake = FakeCard(rows, **kw)
    return TestClient(create_app(tmp_path / "m.db", source=fake.source)), fake


def by_place(c, **params) -> dict:
    return {m["place"]: m for m in c.get("/api/meals", params=params).json()}


def test_slot_boundaries(tmp_path):
    c, _ = make(tmp_path, [
        row("2026-03-02", "10:29", "가경식당", 9000),
        row("2026-03-02", "10:30", "나경식당", 9000),
        row("2026-03-02", "14:59", "다경식당", 9000),
        row("2026-03-02", "15:00", "라경식당", 9000),    # 점심~저녁 사이 = 간식
        row("2026-03-02", "20:59", "마경식당", 9000),
        row("2026-03-02", "21:00", "바경식당", 9000),
        row("2026-03-02", "05:00", "사경식당", 9000),    # 하루 시작 = 아침
    ])
    assert c.post("/api/sync").json()["added"] == 7
    m = by_place(c)
    got = {k: (v["date"], v["slot"]) for k, v in m.items()}
    assert got == {"가경식당": ("2026-03-02", "아침"), "나경식당": ("2026-03-02", "점심"),
                   "다경식당": ("2026-03-02", "점심"), "라경식당": ("2026-03-02", "간식"),
                   "마경식당": ("2026-03-02", "저녁"), "바경식당": ("2026-03-02", "야식"),
                   "사경식당": ("2026-03-02", "아침")}


def test_after_midnight_is_previous_day_yasik(tmp_path):
    c, _ = make(tmp_path, [
        row("2026-03-01", "00:40", "쿠팡이츠", 21000, "배달"),     # 달이 바뀌는 자정 넘김 -> 전날(2월 마지막 날)
        row("2026-03-02", "04:59", "우아한형제들", 18000, "배달"),
        row("2026-03-02", "03:10", "가나편의점", 4000, "편의점"),  # 새벽 간식도 전날로
    ])
    c.post("/api/sync")
    m = by_place(c)
    assert (m["쿠팡이츠"]["date"], m["쿠팡이츠"]["slot"]) == ("2026-02-28", "야식")
    assert m["쿠팡이츠"]["pay_date"] == "2026-03-01"
    assert (m["우아한형제들"]["date"], m["우아한형제들"]["slot"]) == ("2026-03-01", "야식")
    assert (m["가나편의점"]["date"], m["가나편의점"]["slot"]) == ("2026-03-01", "간식")
    feb = {d["date"]: d for d in c.get("/api/calendar", params={"month": "2026-02"}).json()["days"]}
    assert feb["2026-02-28"]["slots"]["야식"]["count"] == 1
    assert "메뉴 모름" in m["쿠팡이츠"]["reasons"]   # 배달앱 이름만으로는 메뉴를 모른다


def test_category_rules(tmp_path):
    c, _ = make(tmp_path, [
        row("2026-03-03", "12:10", "가나커피", 4500, "카페·간식"),     # 점심 시간 커피 = 간식
        row("2026-03-03", "12:20", "다라카페", 16000, "카페·간식"),    # 금액이 크면 끼니로
        row("2026-03-03", "08:00", "마바편의점", 3000, "편의점"),
        row("2026-03-03", "18:00", "사아마트", 52000, "마트·장보기"),
        row("2026-03-04", "13:00", "가나순대", 9000, "외식"),
    ])
    c.post("/api/sync")
    m = by_place(c)
    assert m["가나커피"]["slot"] == "간식" and m["다라카페"]["slot"] == "점심" and m["마바편의점"]["slot"] == "간식"
    assert m["사아마트"]["slot"] == "장보기" and m["사아마트"]["kcal"] is None and not m["사아마트"]["reasons"]
    assert [i["name"] for i in m["가나순대"]["items"]] == ["순대국"] and m["가나순대"]["guessed"]
    assert m["가나순대"]["kcal"]["mid"] == menu_kcal.MENUS["순대국"].mid
    assert [i["name"] for i in m["가나커피"]["items"]] == ["아메리카노"]
    day = {d["date"]: d for d in c.get("/api/calendar", params={"month": "2026-03"}).json()["days"]}["2026-03-03"]
    assert day["groceries"] == 1 and "장보기" not in day["slots"]


def test_same_slot_multiple(tmp_path):
    c, _ = make(tmp_path, [
        row("2026-03-05", "12:10", "가국밥", 9000), row("2026-03-05", "12:40", "나김밥", 4000),
        row("2026-03-05", "19:00", "다치킨", 23000, "배달"),
    ])
    c.post("/api/sync")
    m = by_place(c)
    assert m["가국밥"]["slot"] == m["나김밥"]["slot"] == "점심"
    assert "같은 끼니 여러 건" in m["가국밥"]["reasons"] and "같은 끼니 여러 건" in m["나김밥"]["reasons"]
    assert "같은 끼니 여러 건" not in m["다치킨"]["reasons"]
    cal = {d["date"]: d for d in c.get("/api/calendar", params={"month": "2026-03"}).json()["days"]}["2026-03-05"]
    assert cal["slots"]["점심"]["count"] == 2 and cal["slots"]["점심"]["state"] == "review"
    # 하나를 '내가 먹은 게 아님'으로 빼면 나머지는 더 이상 여러 건이 아님
    c.put(f"/api/meals/{m['나김밥']['id']}", json={"excluded": True})
    assert "같은 끼니 여러 건" not in by_place(c)["가국밥"]["reasons"]
    assert c.get("/api/review").json()["by_reason"].get("같은 끼니 여러 건") is None


def test_resync_no_duplicates_and_cancelled_removed(tmp_path):
    rows = [row("2026-03-06", "12:00", "가김밥", 4000), row("2026-03-06", "19:00", "쿠팡이츠", 25000, "배달"),
            row("2026-04-02", "12:00", "나국밥", 9000),
            row("2026-04-02", "12:00", "나국밥", 9000)]   # 완전히 같은 결제 두 번 (같이 먹은 친구 몫 등) -> 둘 다
    c, fake = make(tmp_path, rows)
    r1 = c.post("/api/sync").json()
    assert (r1["added"], r1["fetched"]) == (4, 4)
    assert "tx:2026-03" in fake.calls and "tx:2026-04" in fake.calls   # 달마다 나눠 받음
    # 사람이 고친 값: 쿠팡이츠 메뉴·인원, 가김밥 끼니 옮김
    m = by_place(c)
    cid = m["쿠팡이츠"]["id"]
    c.put(f"/api/meals/{cid}", json={"items": [{"name": "치킨", "qty": 1}], "people": 2})
    c.put(f"/api/meals/{m['가김밥']['id']}", json={"slot": "아침"})
    r2 = c.post("/api/sync").json()
    assert (r2["added"], r2["updated"], r2["unchanged"], r2["removed"]) == (0, 0, 4, 0)
    assert len(c.get("/api/meals").json()) == 4
    m = by_place(c)
    assert [i["name"] for i in m["쿠팡이츠"]["items"]] == ["치킨"] and m["쿠팡이츠"]["people"] == 2
    assert m["가김밥"]["slot"] == "아침"
    # card_spending이 사용처 표시 이름만 바꿈(원래 이름 raw는 그대로 = 같은 결제) -> 가게는 갱신, 고친 메뉴는 유지,
    # 고치지 않은 결제는 새 이름으로 메뉴를 다시 추정
    for r in fake.rows:
        r["name"] = {"쿠팡이츠": "쿠팡이츠 가나상회", "가김밥": "가김밥나라", "나국밥": "나분식"}[r["name"]]
    r2b = c.post("/api/sync").json()
    assert (r2b["added"], r2b["updated"], r2b["removed"]) == (0, 4, 0)
    m = {x["place"]: x for x in c.get("/api/meals").json()}
    assert [i["name"] for i in m["쿠팡이츠 가나상회"]["items"]] == ["치킨"] and m["쿠팡이츠 가나상회"]["people"] == 2
    assert [i["name"] for i in m["나분식"]["items"]] == ["떡볶이"]
    for r in fake.rows:
        r["name"] = r["raw"]
    c.post("/api/sync")
    # card_spending에서 취소된 결제는 응답에서 빠진다 -> 다음 동기화 때 지움
    fake.rows = [r for r in fake.rows if r["name"] != "쿠팡이츠"]
    fake.rows.pop()   # 같은 결제 두 번 중 하나도 취소
    r3 = c.post("/api/sync").json()
    assert (r3["removed"], r3["added"]) == (2, 0)
    places = [x["place"] for x in c.get("/api/meals").json()]
    assert sorted(places) == ["가김밥", "나국밥"]
    # card_spending 기간 밖(예: 그쪽 DB를 새로 만듦)의 기록은 지우지 않는다
    fake.rows, fake.period = [], ("2026-05-01", "2026-05-31")
    assert c.post("/api/sync").json()["removed"] == 0
    # card_spending이 비어 있으면(기간 없음) 아무것도 지우지 않는다
    fake.period = None
    assert c.post("/api/sync").json()["removed"] == 0 and len(c.get("/api/meals").json()) == 2


def test_category_change_in_card_spending_reflected(tmp_path):
    c, fake = make(tmp_path, [row("2026-03-07", "12:00", "가나상회", 4000, "외식")])
    c.post("/api/sync")
    assert by_place(c)["가나상회"]["slot"] == "점심"
    fake.rows[0]["category"] = "식비 › 카페·간식"   # card_spending에서 범주를 고침
    assert c.post("/api/sync").json()["updated"] == 1
    m = by_place(c)["가나상회"]
    assert m["slot"] == "간식" and m["category"] == "카페·간식"


def test_people_two_halves_kcal_and_people_check(tmp_path):
    rows = [row(f"2026-03-{d:02d}", "19:00", f"가게{d}", 20000, "배달") for d in range(10, 16)]
    rows.append(row("2026-03-16", "19:30", "가나치킨", 46000, "배달"))   # 평소(2만)의 2배 이상
    c, _ = make(tmp_path, rows)
    c.post("/api/sync")
    m = by_place(c)["가나치킨"]
    assert "인원 확인" in m["reasons"]
    one = m["kcal"]["mid"]
    assert one == menu_kcal.MENUS["치킨"].mid
    r = c.put(f"/api/meals/{m['id']}", json={"people": 2}).json()
    assert r["kcal"]["mid"] == round(one / 2) and r["kcal"]["low"] == round(menu_kcal.MENUS["치킨"].low / 2)
    assert "인원 확인" not in r["reasons"]
    assert "인원 확인" not in by_place(c)["가게10"]["reasons"]


def test_card_spending_down(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]          # 닫힌 포트 = 꺼진 card_spending
    c = TestClient(create_app(tmp_path / "m.db", source=HttpCardSource(f"http://127.0.0.1:{port}", timeout=2)))
    assert c.get("/api/weekly").json()["weeks"] == []      # 빈 DB: 0으로 채운 주를 만들지 않는다
    assert c.get("/api/review").json()["count"] == 0
    r = c.post("/api/sync")
    assert r.status_code == 503 and r.json()["detail"]["kind"] == "down"
    # 나머지는 계속 동작 (직접 입력·캘린더)
    assert c.post("/api/meals", json={"date": "2026-03-08", "slot": "저녁", "items": [{"name": "집밥 한상"}]}).status_code == 201
    assert c.get("/api/calendar", params={"month": "2026-03"}).status_code == 200
    assert c.get("/api/status").json()["last_sync"] is None


def test_bad_responses(tmp_path):
    c, fake = make(tmp_path, [row("2026-03-09", "12:00", f"가게{i}", 5000) for i in range(3)], limit=2)
    r = c.post("/api/sync")
    assert r.status_code == 502 and r.json()["detail"]["kind"] == "bad_response"
    assert c.get("/api/meals").json() == []   # 일부만 넣지 않는다
    fake.limit = 1000
    bad = row("2026-03-09", "13:00", "필드빠짐", 1000)
    del bad["amount"]
    fake.rows.append(bad)
    assert c.post("/api/sync").status_code == 502


def test_manual_entry_skip_and_delete(tmp_path):
    c, _ = make(tmp_path, [row("2026-03-10", "12:00", "가김밥", 4000)])
    c.post("/api/sync")
    r = c.post("/api/meals", json={"date": "2026-03-10", "slot": "저녁", "people": 1, "note": "집밥",
                                   "items": [{"name": "집밥 한상"}, {"name": "계란말이", "kcal": 200, "qty": 1}]})
    assert r.status_code == 201
    mid = r.json()["id"]
    m = next(x for x in c.get("/api/meals", params={"start": "2026-03-10", "end": "2026-03-10"}).json() if x["id"] == mid)
    assert m["kcal"]["mid"] == menu_kcal.MENUS["집밥 한상"].mid + 200 and not m["kcal"]["partial"]
    assert [i["kcal"]["source"] for i in m["items"]] == ["table", "manual"]
    # 기준표에 없고 kcal도 안 넣은 메뉴 -> 모름 (0으로 치지 않음)
    r = c.post("/api/meals", json={"date": "2026-03-11", "slot": "점심", "items": [{"name": "이상한메뉴"}]}).json()
    m2 = next(x for x in c.get("/api/meals").json() if x["id"] == r["id"])
    assert m2["kcal"] is None and m2["items"][0]["kcal"] is None
    # 건너뜀
    assert c.post("/api/meals", json={"date": "2026-03-10", "slot": "아침", "skipped": True}).status_code == 201
    day = {d["date"]: d for d in c.get("/api/calendar", params={"month": "2026-03"}).json()["days"]}["2026-03-10"]
    assert day["slots"]["아침"]["state"] == "skip" and day["slots"]["저녁"]["state"] == "ok"
    assert day["kcal"]["mid"] == menu_kcal.MENUS["김밥"].mid + menu_kcal.MENUS["집밥 한상"].mid + 200
    # 잘못된 입력
    assert c.post("/api/meals", json={"date": "2026-03-10", "slot": "저녁", "items": []}).status_code == 400
    assert c.post("/api/meals", json={"date": "2026-03-10", "slot": "저녁", "people": 0,
                                      "items": [{"name": "김밥"}]}).status_code == 422
    assert c.post("/api/meals", json={"date": "2026-03-10", "slot": "브런치", "items": [{"name": "김밥"}]}).status_code == 422
    # 지우기: 직접 입력은 됨, 카드 결제는 안 됨 (다음 동기화 때 되살아나므로)
    assert c.delete(f"/api/meals/{mid}").status_code == 200
    card = by_place(c)["가김밥"]
    assert c.delete(f"/api/meals/{card['id']}").status_code == 400
    assert c.put(f"/api/meals/{card['id']}", json={"place": "다른곳"}).status_code == 400
    assert c.delete("/api/meals/99999").status_code == 404
    assert c.put("/api/meals/99999", json={"people": 2}).status_code == 404


def test_settings_change_reclassifies(tmp_path):
    c, _ = make(tmp_path, [row("2026-03-12", "05:30", "가해장국", 9000), row("2026-03-12", "10:40", "나김밥", 4000)])
    c.post("/api/sync")
    m = by_place(c)
    assert (m["가해장국"]["date"], m["가해장국"]["slot"]) == ("2026-03-12", "아침")
    c.put(f"/api/meals/{m['나김밥']['id']}", json={"slot": "점심", "confirmed": True})  # 사람이 정한 끼니는 고정
    s = c.get("/api/settings").json()
    w = s["windows"] | {"아침": ["06:00", "10:59"], "점심": ["11:00", "14:59"], "야식": ["21:00", "05:59"]}
    r = c.put("/api/settings", json={"windows": w, "snack_meal_min_krw": 10000})
    assert r.status_code == 200 and r.json()["moved"] == 1
    m = by_place(c)
    assert (m["가해장국"]["date"], m["가해장국"]["slot"]) == ("2026-03-11", "야식")
    assert m["나김밥"]["slot"] == "점심"
    # 다시 동기화해도 새 설정 유지
    c.post("/api/sync")
    assert by_place(c)["가해장국"]["slot"] == "야식"
    bad = c.put("/api/settings", json={"windows": w | {"저녁": ["14:00", "20:59"]}, "snack_meal_min_krw": 10000})
    assert bad.status_code == 400 and "겹침" in bad.json()["detail"]
    assert c.put("/api/settings", json={"windows": w | {"저녁": ["25:00", "20:59"]},
                                        "snack_meal_min_krw": 10000}).status_code == 400


def test_weekly(tmp_path):
    c, _ = make(tmp_path, [
        row("2026-03-02", "12:00", "가김밥", 4000),                   # 월
        row("2026-03-03", "01:00", "쿠팡이츠", 20000, "배달"),        # 화 새벽 -> 월 야식, 메뉴 모름
        row("2026-03-04", "22:00", "나치킨", 22000, "배달"),          # 수 야식
        row("2026-03-10", "12:00", "다국밥", 9000),                   # 다음 주
    ])
    c.post("/api/sync")
    w = c.get("/api/weekly", params={"weeks": 2}).json()
    assert w["end"] == "2026-03-10" and [x["week_start"] for x in w["weeks"]] == ["2026-03-02", "2026-03-09"]
    a = w["weeks"][0]
    # 3/2는 김밥은 알지만 쿠팡이츠(메뉴 모름)가 섞여 평균에서 뺀다 -> 다 아는 날은 3/4(치킨)뿐
    assert (a["yasik_days"], a["delivery"], a["eating_out"], a["unknown"], a["kcal_days"], a["partial_days"]) == (2, 2, 1, 1, 1, 1)
    assert a["avg_kcal"]["mid"] == menu_kcal.MENUS["치킨"].mid
    assert w["weeks"][1]["avg_kcal"]["mid"] == menu_kcal.MENUS["돼지국밥"].mid
    assert len(c.get("/api/weekly", params={"weeks": 12}).json()["weeks"]) == 2   # 첫 기록 전 빈 주는 뺌
    assert c.get("/api/weekly", params={"weeks": 1, "end": "2025-01-01"}).json()["weeks"] == []
    tail = c.get("/api/weekly", params={"weeks": 2, "end": "2026-03-22"}).json()["weeks"]
    assert [w["meals"] for w in tail] == [1, 0] and tail[1]["avg_kcal"] is None   # 기록 뒤의 빈 주는 0으로


@pytest.mark.parametrize("name,menu", [("가나순대", "순대국"), ("가나다김밥", "김밥"), ("하노이쌀국수", "쌀국수"),
                                       ("춘천닭갈비", "닭갈비"), ("쿠팡이츠", None), ("우아한형제들", None),
                                       ("GS25 역삼점", None), ("가나식당", None)])
def test_menu_guess(name, menu):
    assert menu_kcal.guess_menu(name) == menu


def test_menu_table_consistent():
    assert menu_kcal.check_table() == []
    assert len(menu_kcal.MENUS) >= 40


def test_review_since_buries_old_and_keeps_new_until_done(tmp_path):
    rows = [row(f"2026-03-{d:02d}", "19:00", "쿠팡이츠", 20000 + d, "배달") for d in range(1, 21)]  # 메뉴 모름 20끼
    c, fake = make(tmp_path, rows)
    c.post("/api/sync")
    r = c.get("/api/review").json()
    assert r["review_since"] == "2026-03-13"                       # 처음 = 마지막 날(3/20) - 7일
    assert r["count"] == 8 and r["buried"] == 12                   # 3/13~3/20만 확인 대상, 그 전은 모름으로
    assert [m["date"] for m in r["rows"]][:2] == ["2026-03-13", "2026-03-14"]   # 오래된 것부터
    cal = {d["date"]: d for d in c.get("/api/calendar", params={"month": "2026-03"}).json()["days"]}
    assert cal["2026-03-01"]["slots"]["저녁"]["state"] == "unknown"
    assert cal["2026-03-13"]["slots"]["저녁"]["state"] == "review"
    assert cal["2026-03-13"]["slots"]["저녁"]["entries"][0]["label"] is None
    # 일주일 뒤 새 결제가 와도 기준일은 그대로 -> 안 채운 3/13~ 끼니는 남는다 (주 단위로 열어도 같음)
    fake.rows += [row(f"2026-03-{d:02d}", "12:00", "가나식당", 9000) for d in range(21, 28)]
    c.post("/api/sync")
    r = c.get("/api/review").json()
    assert r["review_since"] == "2026-03-13" and r["count"] == 15 and r["suggest_since"] is None   # 기본 20끼 이하
    # 쌓임 기준을 낮추면 "○일 이전은 모름으로 두기" 제안 (최근 7일은 남김)
    s = c.get("/api/settings").json()
    c.put("/api/settings", json={"windows": s["windows"], "snack_meal_min_krw": s["snack_meal_min_krw"], "backlog_warn": 10})
    r = c.get("/api/review").json()
    assert r["suggest_since"] == "2026-03-21"
    moved = c.put("/api/review/since", json={"date": r["suggest_since"]}).json()
    assert moved["previous"] == "2026-03-13" and moved["count"] == 7
    assert c.put("/api/review/since", json={"date": moved["previous"]}).json()["count"] == 15   # 되돌리기
    assert c.put("/api/review/since", json={"date": None}).json()["count"] == 27               # 기준일 없음 = 전부
    assert c.get("/api/review").json()["review_since"] is None     # 빈 값이 다시 자동으로 채워지지 않음
    assert c.put("/api/review/since", json={"date": "2026-13-01"}).status_code == 422
    # 모름으로 둔 끼니도 날짜 상세에서는 채울 수 있다
    c.put("/api/review/since", json={"date": "2026-03-13"})
    old = next(m for m in c.get("/api/meals", params={"start": "2026-03-01", "end": "2026-03-01"}).json())
    assert old["reasons"] == ["메뉴 모름"] and not old["needs_review"]
    c.put(f"/api/meals/{old['id']}", json={"items": [{"name": "치킨"}]})
    cal = {d["date"]: d for d in c.get("/api/calendar", params={"month": "2026-03"}).json()["days"]}
    assert cal["2026-03-01"]["slots"]["저녁"]["state"] == "ok"
    assert cal["2026-03-01"]["slots"]["저녁"]["entries"][0]["label"] == "치킨"


def test_kcal_source_db_vs_table(tmp_path):
    menus = {m["name"]: m for m in TestClient(create_app(tmp_path / "m.db", source=FakeCard([]).source)).get("/api/menus").json()}
    assert menus["김밥"]["source"] == "db" and menus["김밥"]["db_rows"] >= 3
    assert menus["치킨"]["source"] == "table" and menus["치킨"]["db_rows"] == 0
    assert all(m.source == "db" for n, m in menu_kcal.MENUS.items() if n in menu_kcal.DB_VALUES)
    c, _ = make(tmp_path, [row("2026-03-02", "12:00", "가나김밥", 4000), row("2026-03-02", "19:00", "다라치킨", 20000, "배달")])
    c.post("/api/sync")
    m = by_place(c)
    assert m["가나김밥"]["items"][0]["kcal"]["source"] == "db" and m["가나김밥"]["kcal"]["mid"] == menu_kcal.DB_VALUES["김밥"][1]
    assert m["다라치킨"]["items"][0]["kcal"]["source"] == "table"
