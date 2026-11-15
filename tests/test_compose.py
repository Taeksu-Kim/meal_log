"""구성 계산 (이름을 풀어 kcal 추정) API 테스트. 메뉴 이름은 지어낸 것 (배달앱에서 흔한 모양: 꼬리표·한정·괄호 속 고기 양)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from meal_log.app import create_app
from tests.fake_card import FakeCard, row
from tests.test_captures import DetailOcr, img, upload


def make(tmp_path, ocr=None):
    c = TestClient(create_app(tmp_path / "data" / "m.db", source=FakeCard([row("2026-11-01", "19:00", "우아한형제들", 27000, "배달")]).source,
                              ocr=ocr))
    c.post("/api/sync")
    return c


def comp(c, name):
    return c.get("/api/menus/compose", params={"name": name})


def test_build_frame_with_stated_meat_grams(tmp_path):
    c = make(tmp_path)
    e = comp(c, "[사장님픽] 50개한정 제육덮밥(고기양300g)").json()
    assert e["frame"].startswith("덮밥") and e["unit"] == "1인분" and not e["rough"]
    labels = [p["label"] for p in e["parts"]]
    assert labels == ["밥", "제육", "소스·채소"]
    meat = e["parts"][1]
    assert meat["detail"].startswith("300g × 116~195/100g") and (meat["low"], meat["high"]) == (350, 580)   # 적힌 양 300g
    assert e["low"] == sum(p["low"] for p in e["parts"]) and e["high"] == sum(p["high"] for p in e["parts"])
    plain = comp(c, "제육덮밥").json()                                     # 양이 없으면 그 재료의 기본 토핑 양 (200g)
    assert plain["parts"][1]["detail"].startswith("200g ×") and plain["mid"] < e["mid"]
    assert "식약처" in e["parts"][0]["detail"] and "제육볶음" in meat["detail"]   # 줄마다 근거


def test_multipliers_and_toppings_on_base_menus(tmp_path):
    c = make(tmp_path)
    one, two = comp(c, "치즈 떡볶이").json(), comp(c, "치즈 떡볶이(2인분)").json()
    assert [p["label"] for p in one["parts"]] == ["떡볶이", "치즈 추가"] and one["frame"].startswith("떡볶이 (기준표")
    assert two["unit"] == "2인분" and two["mid"] == one["mid"] * 2
    big = comp(c, "제육덮밥 곱빼기").json()
    assert big["unit"] == "1그릇" and abs(big["mid"] - comp(c, "제육덮밥").json()["mid"] * 1.5) <= 10
    # 토핑을 얹는 요리가 아니면(샤브샤브·스테이크) 계산할 게 없다 -> 구성 계산 없음 ("비슷한 메뉴"가 맡음)
    assert comp(c, "소고기샤브샤브").status_code == 404 and comp(c, "함박스테이크").status_code == 404
    assert comp(c, "직화 제육덮밥").json()["rough"]                           # 모르는 앞말 -> 대략


def test_rough_and_unknown_and_known(tmp_path):
    c = make(tmp_path)
    r = comp(c, "로제 크림 파스타").json()
    assert r["rough"] and any("로제크림" in n for n in r["notes"])           # 모르는 말 -> 대략
    assert comp(c, "김밥").status_code == 404                              # 이미 기준표에 있음
    assert comp(c, "아무말대잔치").status_code == 404                        # 끝말로 요리 종류를 못 찾음
    assert comp(c, "중식우동").json()["frame"].startswith("우동")           # '동'으로 끝나도 덮밥이 아님
    assert comp(c, "규동").json()["frame"].startswith("덮밥")
    rice = comp(c, "현미밥").json()                                         # 그냥 ○○밥은 토핑 없이 밥만
    assert [p["label"] for p in rice["parts"]] == ["밥", "양념·간장·기름"]
    assert comp(c, " ").status_code == 400                                 # 공백만 -> 잘못된 요청 (못 푼 것과 구분)


def test_save_as_my_menu_and_meals_use_it(tmp_path):
    c = make(tmp_path)
    e = comp(c, "동파고기덮밥(고기양200g)").json()
    r = c.put("/api/menus/동파고기덮밥(고기양200g)", json={"unit": e["unit"], "low": e["low"], "mid": e["mid"], "high": e["high"],
                                                       "source": "calc", "basis": e["basis"]})
    assert r.status_code == 200 and r.json()["menu"]["source"] == "calc" and r.json()["menu"]["basis"].startswith("구성 계산: 밥")
    mine = next(m for m in c.get("/api/menus").json() if m["name"] == "동파고기덮밥(고기양200g)")
    assert mine["custom"] and mine["source"] == "calc"
    meal = c.get("/api/meals").json()[0]
    c.put(f"/api/meals/{meal['id']}", json={"items": [{"name": "동파고기덮밥(고기양200g)", "qty": 1}]})
    meal = c.get("/api/meals").json()[0]
    assert meal["kcal"]["mid"] == e["mid"] and meal["items"][0]["kcal"]["source"] == "calc"
    assert comp(c, "동파고기덮밥(고기양200g)").status_code == 404           # 이제 내 메뉴 -> 다시 풀 필요 없음


def test_capture_items_and_assistant_get_compose(tmp_path):
    c = make(tmp_path, DetailOcr())
    d = upload(c, img(311)).json()["captures"][0]
    tteok = next(i for i in d["items"] if i["read_name"].endswith("치즈 떡볶이(2인분)"))
    assert not tteok["known"] and tteok["compose"]["unit"] == "2인분"      # 캡처 칩에 "구성 추정 약 …"
    sundae = next(i for i in d["items"] if i["name"] == "순대")
    assert sundae["known"] and sundae["compose"] is None                  # 기준표에 있으면 계산 안 함
    info = c.post("/api/assistant/tools/menu_info", json={"name": "차돌짬뽕"}).json()
    assert info["known"] is False and info["compose"]["parts"][1]["label"] == "소고기 추가"
    text = json.dumps(info, ensure_ascii=False)
    assert "우아한형제들" not in text and "27000" not in text
