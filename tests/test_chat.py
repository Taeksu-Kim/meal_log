"""채팅 입력 해석 API 테스트. 바깥(card_spending·로컬 LLM)만 가짜. 문장은 전부 지어낸 것."""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from meal_log import chat
from meal_log.app import create_app
from tests.fake_card import FakeCard, row

TODAY = "2026-11-15"   # 일요일


class PromptLLM:
    """문장 내용으로 답하는 가짜 LLM (호출 순서에 기대지 않는다). 받은 프롬프트를 기록한다."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers, self.calls = answers, []

    def complete(self, system: str, user: str, *, schema=None) -> str:
        self.calls.append((system, user))
        for key, ans in self.answers.items():
            if key in user:
                return ans if isinstance(ans, str) else json.dumps(ans, ensure_ascii=False)
        return '{"entries": []}'


class DownLLM:
    def complete(self, system, user, *, schema=None):
        from agent.core.llm import LLMUnavailableError
        raise LLMUnavailableError("connection refused")


def make(tmp_path, llm=None, rows=()):
    fake = FakeCard(list(rows))
    c = TestClient(create_app(tmp_path / "m.db", source=fake.source, llm=llm))
    if rows:
        c.post("/api/sync")
    return c


def parse(c, text, **kw):
    return c.post("/api/chat/parse", json={"text": text, "today": TODAY, **kw})


def test_rules_today_slot_qty(tmp_path):
    llm = PromptLLM({})
    c = make(tmp_path, llm)
    r = parse(c, "오늘 아침에 옥수수 1개 먹었어").json()
    assert r["via"] == "rules" and not llm.calls       # 규칙으로 되면 LLM을 부르지 않는다
    (e,) = r["entries"]
    assert (e["date"], e["slot"]) == (TODAY, "아침")
    assert [(i["name"], i["qty"], i["known"]) for i in e["items"]] == [("옥수수", 1.0, False)]


def test_rules_dates_and_default_date(tmp_path):
    c = make(tmp_path, PromptLLM({}))
    assert parse(c, "어제 저녁 김밥 두 줄이랑 라면").json()["entries"][0]["date"] == "2026-11-14"
    assert parse(c, "그제 점심 비빔밥").json()["entries"][0]["date"] == "2026-11-13"
    assert parse(c, "11/3 야식 치킨 반 마리").json()["entries"][0]["date"] == "2026-11-03"
    assert parse(c, "12/30 점심 김밥").json()["entries"][0]["date"] == "2025-12-30"   # 미래 날짜는 작년으로
    # 날짜를 말하지 않으면 화면의 날짜 칸, 그것도 없으면 오늘
    assert parse(c, "점심에 짜장면", date="2026-11-10").json()["entries"][0]["date"] == "2026-11-10"
    assert parse(c, "점심에 짜장면").json()["entries"][0]["date"] == TODAY
    e = parse(c, "어제 저녁 김밥 두 줄이랑 라면").json()["entries"][0]
    assert [(i["name"], i["qty"]) for i in e["items"]] == [("김밥", 2.0), ("라면", 1.0)]
    assert all(i["known"] and i["low"] for i in e["items"])


def test_rules_no_slot_and_tricky_words(tmp_path):
    c = make(tmp_path, PromptLLM({}))
    e = parse(c, "옥수수 반 개").json()["entries"][0]
    assert e["slot"] is None and e["items"][0]["qty"] == 0.5        # 끼니는 지어내지 않는다
    assert parse(c, "만두 먹었어").json()["entries"][0]["items"][0]["name"] == "만두"   # "만" × 2가 아님
    assert [i["name"] for i in parse(c, "아침밥으로 토스트랑 우유").json()["entries"][0]["items"]] == ["토스트", "우유"]
    assert parse(c, "라떼 한 잔 마셨어").json()["entries"][0]["items"][0]["name"] == "카페라떼"   # 별칭


def test_unit_mismatch_no_kcal(tmp_path):
    c = make(tmp_path, PromptLLM({}))
    pizza = parse(c, "점심 피자 세 조각").json()["entries"][0]["items"][0]
    assert pizza["unit_mismatch"] and pizza["low"] is None and pizza["said_unit"] == "조각"   # 라지 3판으로 곱하지 않음
    gimbap = parse(c, "점심 김밥 두 줄").json()["entries"][0]["items"][0]
    assert not gimbap["unit_mismatch"] and gimbap["low"] > 0


def test_complex_goes_to_llm(tmp_path):
    llm = PromptLLM({"점심엔 김밥, 저녁엔 라면": {"entries": [
        {"date": TODAY, "slot": "점심", "items": [{"name": "김밥", "qty": 1, "unit": ""}]},
        {"date": TODAY, "slot": "저녁", "items": [{"name": "라면", "qty": 1, "unit": ""}]}]}})
    c = make(tmp_path, llm)
    r = parse(c, "점심엔 김밥, 저녁엔 라면 먹었어").json()
    assert r["via"] == "llm" and [e["slot"] for e in r["entries"]] == ["점심", "저녁"]
    system, user = llm.calls[0]
    assert "2026-11-09(월)" in system and "김밥, " in user          # 최근 날짜 표 + 메뉴 목록
    assert "원" not in user and "쿠팡" not in user                    # 금액·가게 정보는 보내지 않는다
    # 규칙으로 되는 문장도 "LLM으로 다시 해석"이면 LLM
    llm.answers["옥수수"] = {"entries": [{"date": TODAY, "slot": "아침", "items": [{"name": "옥수수", "qty": 1, "unit": "개"}]}]}
    assert parse(c, "아침에 옥수수 1개", force_llm=True).json()["via"] == "llm"
    # 먹은 내용이 없으면 빈 목록
    assert parse(c, "점심엔 아무것도, 저녁엔 굶음").json()["entries"] == []


def test_llm_bad_output_and_retry(tmp_path):
    future = {"entries": [{"date": "2026-11-16", "slot": "저녁", "items": [{"name": "치킨", "qty": 1, "unit": ""}]}]}
    c = make(tmp_path, PromptLLM({"월요일": future}))
    r = parse(c, "월요일 저녁에 치킨")
    assert r.status_code == 502 and r.json()["detail"]["kind"] == "bad_llm_output"   # 미래 날짜는 재시도 후 거부
    c2 = make(tmp_path / "b", PromptLLM({"월요일": "이건 JSON이 아님"}))
    assert c2.post("/api/chat/parse", json={"text": "월요일 저녁에 치킨", "today": TODAY}).status_code == 502
    many = {"entries": [{"date": TODAY, "slot": "점심", "items": [{"name": "김밥", "qty": 99, "unit": ""}]}]}
    c3 = make(tmp_path / "c", PromptLLM({"월요일": many}))
    assert c3.post("/api/chat/parse", json={"text": "월요일 점심 김밥 아흔아홉 줄", "today": TODAY}).status_code == 502


def test_llm_down_only_matters_for_complex(tmp_path):
    c = make(tmp_path, DownLLM())
    assert parse(c, "오늘 아침에 옥수수 1개 먹었어").status_code == 200       # 규칙으로 되는 건 LLM 없이
    r = parse(c, "점심엔 김밥, 저녁엔 라면 먹었어")
    assert r.status_code == 503 and r.json()["detail"]["kind"] == "llm_down"
    no_url = TestClient(create_app(tmp_path / "n.db", source=FakeCard([]).source))   # llm도 llm_url도 없음
    assert no_url.post("/api/chat/parse", json={"text": "점심엔 김밥, 저녁엔 라면", "today": TODAY}).status_code == 503


def test_fill_candidates_are_unknown_card_meals(tmp_path):
    rows = [row("2026-11-14", "19:30", "우아한형제들", 20000, "배달"),   # 메뉴 모름 -> 후보
            row("2026-11-14", "12:10", "가나김밥", 4000),               # 메뉴 추정됨 -> 후보 아님
            row("2026-11-14", "15:00", "다라마트", 30000, "마트·장보기")]  # 장보기 -> 후보 아님
    c = make(tmp_path, PromptLLM({}), rows)
    e = parse(c, "어제 저녁 치킨 반 마리").json()["entries"][0]
    assert [(x["slot"], x["place"], x["time"]) for x in e["fill_candidates"]] == [("저녁", "우아한형제들", "19:30")]
    # 화면은 이 후보에 기존 API로 메뉴를 넣는다 -> 새 끼니가 생기지 않고 그 결제가 채워짐
    mid = e["fill_candidates"][0]["id"]
    c.put(f"/api/meals/{mid}", json={"items": [{"name": "치킨", "qty": 0.5}]})
    day = c.get("/api/meals", params={"start": "2026-11-14", "end": "2026-11-14"}).json()
    assert len(day) == 3 and next(m for m in day if m["id"] == mid)["kcal"]["mid"] > 0
    assert parse(c, "어제 저녁 치킨 반 마리").json()["entries"][0]["fill_candidates"] == []   # 채웠으면 후보에서 빠짐


@pytest.mark.parametrize("bad", ["", " " * 3, "가" * 301])
def test_bad_text(tmp_path, bad):
    c = make(tmp_path, PromptLLM({}))
    assert parse(c, bad).status_code in (400, 422)


def test_parse_rules_direct_defers():
    t = date(2026, 11, 15)
    for s in ("점심엔 김밥, 저녁엔 라면", "아점으로 샌드위치", "월요일 저녁 치킨", "중식으로 짬뽕 먹었어", "어제 점심 쿠팡이츠는 마라탕이었어",
              "김치찌개 빼고 다 먹었어", "월요일, 치킨이랑 맥주", "아점, 샌드위치"):   # 쉼표로 끊기면 "월요일"이 메뉴가 될 뻔
        assert chat.parse_rules(s, t, t) is None, s


def test_user_example_with_packaged_food(tmp_path):
    """사용자 예: 포장 식품(내 메뉴) + 재료. 띄어쓰기·순서가 달라도, 지시어·'같이'·'묶음'이 있어도 규칙으로."""
    c = make(tmp_path, PromptLLM({}))
    c.put("/api/menus/닭다리양념스테이크", json={"unit": "1팩(200g)", "low": 410, "mid": 410, "high": 410, "source": "label"})
    r = parse(c, "저 양념 닭다리 스테이크는, 이거랑 양파 하나, 팽이 버섯 한 묶음 같이 먹었어").json()
    assert r["via"] == "rules"
    items = r["entries"][0]["items"]
    assert [(i["name"], i["qty"]) for i in items] == [("닭다리양념스테이크", 1), ("양파", 1), ("팽이버섯", 1)]
    assert all(i["known"] and not i["unit_mismatch"] for i in items)          # 묶음 = 1봉
    assert items[0]["low"] == 410


def test_llm_down_falls_back_to_loose_rules(tmp_path):
    c = make(tmp_path, DownLLM())
    r = parse(c, "저녁에 고사리 나물이랑 계란 두 개")
    assert r.status_code == 200 and r.json()["loose"] and r.json()["via"] == "rules"
    assert [i["name"] for i in r.json()["entries"][0]["items"]] == ["고사리 나물", "계란"]
    assert parse(c, "점심엔 김밥, 저녁엔 라면").status_code == 503          # 느슨하게도 못 하면 그대로 꺼짐 안내


def test_match_menu_and_units():
    menus = set(chat.menu_kcal.MENUS) | {"닭다리양념스테이크"}
    assert chat.match_menu("양념 닭다리 스테이크", menus) == "닭다리양념스테이크"
    assert chat.match_menu("팽이 버섯", menus) == "팽이버섯"
    assert chat.match_menu("김치볶음밥", menus) == "김치볶음밥"                # 비슷한 게 없으면 그대로
    assert chat.match_menu("김 밥", menus) == "김밥"                         # 짧은 이름은 띄어쓰기 무시로만 맞춤
    t = date(2026, 11, 15)
    for s in ("점심에 양파는 하나", "점심에 양파 하나는"):                       # 수량 앞·뒤의 '는'
        assert [i.name for i in chat.parse_rules(s, t, t).entries[0].items] == ["양파"], s
    assert chat.unit_ok("묶음", "1봉(약 150g)") and chat.unit_ok("알", "1개(약 50g)") and not chat.unit_ok("조각", "라지 1판")
