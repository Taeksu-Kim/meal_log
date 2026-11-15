"""내 메뉴(기준표에 없는 메뉴의 kcal)와 LLM kcal 추정 API 테스트. 바깥(로컬 LLM)만 가짜."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from meal_log.app import create_app
from tests.fake_card import FakeCard
from tests.test_chat import DownLLM, PromptLLM

TODAY = "2026-11-15"


def make(tmp_path, llm=None):
    return TestClient(create_app(tmp_path / "m.db", source=FakeCard([]).source, llm=llm))


def sweet_potato_meal(c) -> int:
    return c.post("/api/meals", json={"date": TODAY, "slot": "아침", "items": [{"name": "옥수수", "qty": 2}]}).json()["id"]


def meal(c, mid):
    return next(m for m in c.get("/api/meals").json() if m["id"] == mid)


def test_user_menu_makes_unknown_known_everywhere(tmp_path):
    c = make(tmp_path, PromptLLM({}))
    mid = sweet_potato_meal(c)
    assert meal(c, mid)["kcal"] is None                                   # 처음엔 모름
    r = c.put("/api/menus/옥수수", json={"unit": "1개", "low": 110, "high": 170}).json()
    assert r["previous"] is None and r["menu"]["mid"] == 140              # 보통값 없으면 가운데
    m = meal(c, mid)
    assert m["kcal"]["mid"] == 280 and m["items"][0]["kcal"]["source"] == "user"   # 기존 기록에도 바로 (×2)
    menus = {x["name"]: x for x in c.get("/api/menus").json()}
    assert menus["옥수수"]["custom"] and not menus["김밥"]["custom"]
    # 채팅 해석에서도 아는 메뉴가 됨
    item = c.post("/api/chat/parse", json={"text": "아침에 옥수수 1개", "today": TODAY}).json()["entries"][0]["items"][0]
    assert item["known"] and item["low"] == 110 and not item["unit_mismatch"]
    # 바꾸면 이전 값을 돌려줌 (되돌리기용), 지우면 다시 모름
    assert c.put("/api/menus/옥수수", json={"unit": "1개", "low": 100, "mid": 120, "high": 160}).json()["previous"]["mid"] == 140
    assert c.delete("/api/menus/옥수수").status_code == 200
    assert meal(c, mid)["kcal"] is None
    assert c.delete("/api/menus/옥수수").status_code == 404


def test_user_menu_multiword_name_accepted_by_chat_rules(tmp_path):
    c = make(tmp_path, PromptLLM({}))
    c.put("/api/menus/옥수수 맛탕", json={"unit": "1인분", "low": 300, "high": 450})
    r = c.post("/api/chat/parse", json={"text": "간식으로 옥수수 맛탕", "today": TODAY}).json()
    assert r["via"] == "rules" and r["entries"][0]["items"][0]["name"] == "옥수수 맛탕"   # 여러 단어여도 내 메뉴면 규칙으로


def test_user_menu_validation(tmp_path):
    c = make(tmp_path)
    assert c.put("/api/menus/김밥", json={"unit": "1줄", "low": 1, "high": 2}).status_code == 409      # 기준표 이름
    assert c.put("/api/menus/옥수수", json={"unit": "1개", "low": 200, "high": 100}).status_code == 400
    assert c.put("/api/menus/옥수수", json={"unit": "1개", "low": 100, "mid": 300, "high": 200}).status_code == 400
    assert c.put("/api/menus/옥수수", json={"unit": "1개", "low": 0, "high": 0}).status_code == 400
    assert c.put("/api/menus/옥수수", json={"unit": "", "low": 1, "high": 2}).status_code == 422
    assert c.put("/api/menus/옥수수", json={"unit": "1개", "low": -1, "high": 2}).status_code == 422
    assert c.put("/api/menus/옥수수", json={"unit": "1개", "low": 1, "high": 99999}).status_code == 422
    assert c.put("/api/menus/" + "가" * 31, json={"unit": "1개", "low": 1, "high": 2}).status_code == 400
    assert c.delete("/api/menus/김밥").status_code == 400


def test_estimate_with_fake_llm(tmp_path):
    llm = PromptLLM({"옥수수": {"known": True, "unit": "개", "low": 80, "mid": 130, "high": 200, "basis": "중간 크기 약 150g"},
                     "쀍쉙퉭": {"known": False, "unit": "", "low": 0, "mid": 0, "high": 0, "basis": ""}})
    c = make(tmp_path, llm)
    r = c.post("/api/menus/estimate", json={"name": "옥수수", "unit": "개"}).json()
    assert {k: r[k] for k in ("known", "unit", "low", "mid", "high", "basis")} == \
        {"known": True, "unit": "1개", "low": 80, "mid": 130, "high": 200, "basis": "중간 크기 약 150g"}
    system, user = llm.calls[-1]
    sent = json.loads(user)
    assert (sent["음식"], sent["단위"]) == ("옥수수", "개") and set(sent) == {"음식", "단위", "참고 기준"}   # 이름·단위·참고 기준만
    assert c.post("/api/menus/estimate", json={"name": "쀍쉙퉭"}).json() == {"known": False}
    assert c.get("/api/menus").json()[-1]["name"] != "옥수수"             # 추정만으로는 저장하지 않음


def test_estimate_bad_output_and_down(tmp_path):
    bad = make(tmp_path, PromptLLM({"옥수수": {"known": True, "unit": "1개", "low": 300, "mid": 100, "high": 200, "basis": ""}}))
    r = bad.post("/api/menus/estimate", json={"name": "옥수수"})
    assert r.status_code == 502 and r.json()["detail"]["kind"] == "bad_llm_output"     # low > mid는 재시도 후 거부
    huge = make(tmp_path / "h", PromptLLM({"옥수수": {"known": True, "unit": "1개", "low": 10, "mid": 20, "high": 99999, "basis": ""}}))
    assert huge.post("/api/menus/estimate", json={"name": "옥수수"}).status_code == 502
    down = make(tmp_path / "d", DownLLM())
    r = down.post("/api/menus/estimate", json={"name": "옥수수"})
    assert r.status_code == 503 and r.json()["detail"]["kind"] == "llm_down"
    assert down.post("/api/menus/estimate", json={"name": ""}).status_code == 422


def test_estimate_gets_similar_reference_values(tmp_path):
    llm = PromptLLM({"치즈떡볶이": {"known": True, "unit": "1인분", "low": 500, "mid": 600, "high": 700, "basis": "떡볶이 480 기준"},
                     "감자": {"known": True, "unit": "1개", "low": 80, "mid": 100, "high": 130, "basis": ""}})
    c = make(tmp_path, llm)
    c.put("/api/menus/떡볶이 국물", json={"unit": "1인분", "low": 50, "high": 90})          # 내 메뉴도 참고 대상
    r = c.post("/api/menus/estimate", json={"name": "치즈떡볶이"}).json()
    refs = json.loads(llm.calls[-1][1])["참고 기준"]
    assert refs[0].startswith("떡볶이 1인분: 350–600") and any(x.startswith("떡볶이 국물") for x in refs)
    assert r["refs"][0] == "떡볶이" and len(refs) <= 7
    assert any(x.startswith("공기밥") for x in refs)                                  # 양 감각용 기준은 항상
    # 이름만 비슷한 다른 음식(감자탕)이 걸려도 기준(공기밥·김밥 식약처 값)을 같이 준다
    c.post("/api/menus/estimate", json={"name": "감자"})
    refs2 = json.loads(llm.calls[-1][1])["참고 기준"]
    assert refs2[0].startswith("감자탕") and any(x.startswith("공기밥") for x in refs2)
    assert any("김밥 1줄" in x and "식약처 DB" in x for x in refs2)
    assert "감자 ≠ 감자탕" in llm.calls[-1][0]


def test_similar_menus_excludes_itself():
    from meal_log import menu_kcal, suggest
    names = [m.name for m in suggest.similar_menus("김밥", menu_kcal.MENUS.values())]
    assert "김밥" not in names and "삼각김밥" in names
