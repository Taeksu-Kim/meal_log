"""A2A 창구 E2E (JSON-RPC로 보냄 -> 화면 API로 확인). 바깥(card_spending·LLM·OCR)만 가짜, 값은 전부 지어낸 것.
REF/AGENT_PRODUCT.md 완료 확인: A2A로 보낸 일이 화면에 보임 / 같은 요청 2회 / 취소 / 재시작 후 조회 / 모델 꺼짐."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from meal_log.app import create_app
from tests.fake_card import FakeCard, row
from tests.test_captures import LABEL, FakeOcr, img
from tests.test_chat import DownLLM, PromptLLM

TODAY = "2026-11-15"
ROWS = [row("2026-11-14", "19:30", "우아한형제들", 21000, "배달")]   # 어제 저녁, 메뉴 모름


def make(tmp_path, *, ocr=None, llm=None, rows=ROWS, name="m.db"):
    fake = FakeCard(list(rows))
    c = TestClient(create_app(tmp_path / name, source=fake.source, llm=llm or PromptLLM({}), ocr=ocr))
    if rows:
        c.post("/api/sync")
    return c, fake


def rpc(c, method, params, rid=1):
    r = c.post("/a2a", json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).json()
    return r.get("result"), r.get("error")


def send(c, *parts, task_id=None, message_id=None, context_id=None):
    msg = {"role": "ROLE_USER", "parts": list(parts)}
    if message_id:
        msg["messageId"] = message_id
    if task_id:
        msg["taskId"] = task_id
    if context_id:
        msg["contextId"] = context_id
    res, err = rpc(c, "SendMessage", {"message": msg})
    return (res or {}).get("task"), err


def data(task, artifact):
    return next(a for a in task["artifacts"] if a["artifactId"] == artifact)["parts"][0]["data"]


def test_agent_card(tmp_path):
    c, _ = make(tmp_path)
    card = c.get("/.well-known/agent-card.json").json()
    assert [s["id"] for s in card["skills"]] == ["log-meal", "read-receipt", "query-meals", "sync-card"]
    assert card["supportedInterfaces"][0]["url"].endswith("/a2a") and card["capabilities"]["streaming"] is False


def test_log_meal_fills_card_payment_and_shows_on_screen(tmp_path):
    c, _ = make(tmp_path)
    t, err = send(c, {"text": "어제 저녁 치킨 반 마리"}, {"data": {"today": TODAY}}, message_id="m1")
    assert err is None and t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert t["metadata"]["reviewUrl"].endswith("/#2026-11-14") and "카드 결제 1건에 채움" in t["status"]["message"]["parts"][0]["text"]
    meals = data(t, "meals")["meals"]
    assert [(m["place"], m["items"]) for m in meals] == [("우아한형제들", [{"name": "치킨", "qty": 0.5}])]
    # 화면 API에서 그대로 보이고, 사람이 이어서 고칠 수 있다
    screen = c.get("/api/meals", params={"start": "2026-11-14", "end": "2026-11-14"}).json()
    assert len(screen) == 1 and screen[0]["items"][0]["name"] == "치킨"
    c.put(f"/api/meals/{screen[0]['id']}", json={"people": 2})
    t2, _ = rpc(c, "GetTask", {"id": t["id"]})
    assert data(t2, "meals")["meals"][0]["people"] == 2                      # 화면에서 고친 게 A2A 조회에도
    # 새 끼니(채울 결제 없음)는 메모 "A2A"로 남는다
    t3, _ = send(c, {"text": "오늘 점심에 김밥 한 줄"}, {"data": {"today": TODAY}})
    m = c.get("/api/meals", params={"start": TODAY, "end": TODAY}).json()[0]
    assert t3["status"]["state"] == "TASK_STATE_COMPLETED" and m["note"] == "A2A" and m["source"] == "manual"


def test_same_message_twice_is_one_task(tmp_path):
    c, _ = make(tmp_path)
    a, _ = send(c, {"text": "오늘 점심에 김밥"}, {"data": {"today": TODAY}}, message_id="dup")
    b, _ = send(c, {"text": "오늘 점심에 김밥"}, {"data": {"today": TODAY}}, message_id="dup")
    assert a["id"] == b["id"] and len(c.get("/api/meals", params={"start": TODAY, "end": TODAY}).json()) == 1


def test_input_required_slot_then_qty(tmp_path):
    c, _ = make(tmp_path)
    t, _ = send(c, {"text": "고구마 반 개"}, {"data": {"today": TODAY}})
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and "slot" in data(t, "question")["need"]
    assert c.get("/api/meals", params={"start": TODAY, "end": TODAY}).json() == []          # 답 전에는 저장 안 함
    done, err = send(c, {"data": {"slot": "간식"}}, task_id=t["id"])
    assert err is None and done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert c.get("/api/meals", params={"start": TODAY, "end": TODAY}).json()[0]["slot"] == "간식"
    # 말한 단위가 기준표와 다름 -> 수량을 되묻는다
    p, _ = send(c, {"text": "점심 피자 세 조각"}, {"data": {"today": TODAY}})
    need = data(p, "question")["need"]
    assert "qty" in need and need["qty"]["items"][0]["said_unit"] == "조각"
    p2, _ = send(c, {"data": {"items": [{"name": "피자", "qty": 0.4}]}}, task_id=p["id"])
    assert p2["status"]["state"] == "TASK_STATE_COMPLETED" and data(p2, "meals")["meals"][0]["items"] == [{"name": "피자", "qty": 0.4}]
    _, err = send(c, {"data": {"slot": "저녁"}}, task_id=p["id"])             # 끝난 작업에 또 답하면 오류
    assert err["code"] == -32602


def test_cancel_and_not_found(tmp_path):
    c, _ = make(tmp_path)
    t, _ = send(c, {"text": "고구마 반 개"}, {"data": {"today": TODAY}})
    res, _ = rpc(c, "CancelTask", {"id": t["id"]})
    assert res["status"]["state"] == "TASK_STATE_CANCELED"
    _, err = rpc(c, "CancelTask", {"id": t["id"]})
    assert err["code"] == -32002
    _, err = rpc(c, "GetTask", {"id": "nope"})
    assert err["code"] == -32001


def test_read_receipt_confirmed_on_screen_completes_task(tmp_path):
    from tests.test_captures import ROWS as CAP_ROWS
    c, _ = make(tmp_path, ocr=FakeOcr(), rows=CAP_ROWS)
    raw = base64.b64encode(img(301)).decode()
    t, err = send(c, {"raw": raw, "filename": "r.png", "mediaType": "image/png"})
    assert err is None and t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    cap = data(t, "captures")["captures"][0]
    assert t["metadata"]["reviewUrl"].endswith(f"#capture={cap['id']}") and cap["total"] == 20000
    screen = c.get("/api/captures").json()
    assert [(x["id"], x["origin"]) for x in screen] == [(cap["id"], "a2a")]                  # 화면의 확인할 캡처에 보임
    # 사람이 화면에서 확정 -> 작업 완료
    c.post(f"/api/captures/{cap['id']}/confirm", json={"target": str(screen[0]["candidates"][0]["id"]),
                                                       "items": [{"name": "치킨"}], "store": "가나다치킨 역삼점"})
    t2, _ = rpc(c, "GetTask", {"id": t["id"]})
    assert t2["status"]["state"] == "TASK_STATE_COMPLETED"


def test_read_label_confirm_via_a2a_and_cancel(tmp_path):
    class LabelOcr(FakeOcr):
        def read(self, png, mode):
            return LABEL if png and self.reads == 0 else super().read(png, mode)
    c, _ = make(tmp_path, ocr=LabelOcr(), rows=[])
    t, _ = send(c, {"raw": base64.b64encode(img(305)).decode(), "filename": "label.jpg"})
    done, err = send(c, {"data": {"confirm": True}}, task_id=t["id"])
    assert err is None and done["status"]["state"] == "TASK_STATE_COMPLETED"
    mine = {m["name"]: m for m in c.get("/api/menus").json() if m["custom"]}
    assert mine["가나다불고기"]["source"] == "label" and mine["가나다불고기"]["mid"] == 410
    # 취소하면 화면의 확인할 캡처에서 치워진다
    t2, _ = send(c, {"raw": base64.b64encode(img(302)).decode(), "filename": "p.png"})
    rpc(c, "CancelTask", {"id": t2["id"]})
    assert c.get("/api/captures").json() == []


def test_restart_keeps_tasks(tmp_path):
    c, _ = make(tmp_path)
    t, _ = send(c, {"text": "고구마 반 개"}, {"data": {"today": TODAY}}, context_id="ctx1")
    c2 = TestClient(create_app(tmp_path / "m.db", source=FakeCard([]).source, llm=PromptLLM({})))   # 같은 DB로 다시 띄움
    again, _ = rpc(c2, "GetTask", {"id": t["id"]})
    assert again["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    done, _ = send(c2, {"data": {"slot": "야식"}}, task_id=t["id"])                     # 재시작 후 이어서
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    lst, _ = rpc(c2, "ListTasks", {"contextId": "ctx1"})
    assert [x["id"] for x in lst["tasks"]] == [t["id"]]


def test_dependencies_down_are_failed_tasks(tmp_path):
    c, fake = make(tmp_path, llm=DownLLM())
    t, _ = send(c, {"raw": base64.b64encode(img(301)).decode()})                         # OCR 없음
    assert t["status"]["state"] == "TASK_STATE_FAILED" and "ocr.sh start" in t["status"]["message"]["parts"][0]["text"]
    t, _ = send(c, {"text": "점심엔 김밥, 저녁엔 라면"}, {"data": {"today": TODAY}})      # LLM 필요한데 꺼짐
    assert t["status"]["state"] == "TASK_STATE_FAILED" and "vLLM" in t["status"]["message"]["parts"][0]["text"]
    down = TestClient(create_app(tmp_path / "d.db", source=__import__("meal_log.sources", fromlist=["x"]).HttpCardSource(
        "http://127.0.0.1:9", timeout=1)))
    t, _ = send(down, {"data": {"skill": "sync-card"}})
    assert t["status"]["state"] == "TASK_STATE_FAILED" and "card_spending serve" in t["status"]["message"]["parts"][0]["text"]


def test_query_and_sync(tmp_path):
    c, fake = make(tmp_path)
    send(c, {"text": "어제 저녁 치킨 반 마리"}, {"data": {"today": TODAY}})
    t, _ = send(c, {"data": {"skill": "query-meals", "start": "2026-11-13", "end": "2026-11-14"}})
    days = data(t, "days")["days"]
    assert [d["date"] for d in days] == ["2026-11-13", "2026-11-14"]
    assert days[1]["meals"][0]["items"] == [{"name": "치킨", "qty": 0.5}] and days[1]["kcal"]["mid"] > 0 and days[0]["meals"] == []
    fake.rows.append(row("2026-11-15", "12:00", "가나김밥", 4000))
    s, _ = send(c, {"data": {"skill": "sync-card"}})
    assert s["status"]["state"] == "TASK_STATE_COMPLETED" and data(s, "sync")["added"] == 1


def test_bad_requests(tmp_path):
    c, _ = make(tmp_path)
    _, err = send(c, {"data": {"skill": "dance"}})
    assert err["code"] == -32602
    _, err = send(c, {"text": "점심에 김밥"}, {"data": {"date": "2026-13-01"}})
    assert err["code"] == -32602
    rej = [x for x in rpc(c, "ListTasks", {})[0]["tasks"] if x["status"]["state"] == "TASK_STATE_REJECTED"]
    assert len(rej) == 1                                                                # 형식 오류도 작업으로 남김 ('진행 중'으로 남지 않게)
    _, err = send(c, {"url": "https://example.com/r.png"})
    assert err["code"] == -32005
    t, _ = send(c, {"text": "오늘은 굶었다"}, {"data": {"today": TODAY}})
    assert t["status"]["state"] in ("TASK_STATE_REJECTED", "TASK_STATE_INPUT_REQUIRED")
