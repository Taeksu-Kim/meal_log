"""A2A 창구 -- 다른 에이전트가 meal_log에 일을 맡긴다 (Agent Card + JSON-RPC, REF/AGENT_PRODUCT.md).

창구 배관(디스커버리·JSON-RPC·에러 코드)은 doc_parser의 도메인 없는 ``a2a.py``를 그대로 쓴다 (공용 패키지로 옮겨지면 import만 바꿈).
여기는 meal_log 일만: 메시지 -> 서비스(service.py) 함수. 화면과 같은 서비스라 A2A로 넣은 끼니·캡처는 화면에 보이고 사람이 이어서 고친다.

기능 (skill) -- data part ``{"skill": ...}``로 고르고, 없으면 이미지면 read-receipt, 글자면 log-meal
- log-meal       "어제 저녁 치킨 반 마리" -> 해석 -> 저장 (같은 날·끼니의 메뉴 모르는 카드 결제에 채우거나 새 끼니).
                 끼니가 빠졌거나 말한 단위가 기준표와 다르면 INPUT_REQUIRED + 무엇을 보내야 하는지 data로 (이어서 {"slot"}·{"items"})
- read-receipt   영수증·배달앱 캡처·영양정보 라벨 이미지 -> 화면의 '확인할 캡처'. 사람이 화면에서 확정하면 COMPLETED,
                 치우면 CANCELED. A2A로 {"confirm": true}를 이어 보내면 읽은 그대로 확정 (라벨은 내 메뉴로 저장)
- query-meals    {"start", "end"} -> 날짜별 끼니·메뉴·kcal (1인 추정, 모르는 끼니 수)
- sync-card      card_spending에서 식비 결제 가져오기
같은 messageId를 다시 보내면 새 일을 만들지 않고 처음 작업을 돌려준다. 개인 데이터라 127.0.0.1에만 연다 (__main__).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import urllib.parse
import uuid
from pathlib import Path
from typing import Optional

from doc_parser import a2a

from meal_log import slots
from meal_log.service import Invalid, MealService, Missing, ServiceError, Unavailable

SKILLS = ("log-meal", "read-receipt", "query-meals", "sync-card")
STATE = {"input_required": "TASK_STATE_INPUT_REQUIRED", "completed": "TASK_STATE_COMPLETED", "failed": "TASK_STATE_FAILED",
         "canceled": "TASK_STATE_CANCELED", "rejected": "TASK_STATE_REJECTED", "working": "TASK_STATE_WORKING"}

CARD = {
    "name": "meal_log",
    "description": "식단 기록 에이전트. 먹은 것을 한국어 문장으로 받아 끼니(아침·점심·저녁·간식·야식)로 기록하고, 영수증·배달앱 캡처·"
                   "포장 식품 영양정보 라벨 이미지를 읽어 카드 결제와 짝지으며, 날짜별 끼니·kcal(1인 추정)를 알려 줍니다. "
                   "이 PC 안(127.0.0.1)에서만 열립니다. Meal diary agent (Korean): log meals from text, read receipts/labels, query kcal.",
    "version": "0.1.0",
    "defaultInputModes": ["text/plain", "application/json", "image/png", "image/jpeg"],
    "defaultOutputModes": ["application/json", "text/plain"],
    "capabilities": {},
    "skills": [
        {"id": "log-meal", "name": "먹은 것 기록 (Log a meal)",
         "description": "한국어 문장 하나로 끼니를 기록합니다. 날짜를 말하지 않으면 data {\"date\": \"YYYY-MM-DD\"} 또는 오늘. "
                        "끼니가 없거나 단위가 기준표와 다르면 INPUT_REQUIRED로 되묻습니다 (같은 taskId로 data {\"slot\": \"저녁\"} "
                        "또는 {\"items\": [{\"name\", \"qty\"}]}). 결과: 저장한 끼니(data)와 확인 화면 링크(metadata.reviewUrl).",
         "tags": ["meal", "diet", "korean", "kcal"], "examples": ["어제 저녁 치킨 반 마리랑 맥주 한 잔", "오늘 아침에 고구마 1개 먹었어"],
         "inputModes": ["text/plain", "application/json"], "outputModes": ["application/json"]},
        {"id": "read-receipt", "name": "영수증·캡처·영양정보 라벨 읽기 (Read a receipt or nutrition label)",
         "description": "이미지(file part raw base64 또는 file:// url, 또는 data {\"path\"})를 OCR로 읽어 화면의 '확인할 캡처'에 올립니다. "
                        "사람이 화면에서 확정하면 COMPLETED. 같은 taskId로 data {\"confirm\": true}를 보내면 읽은 그대로 확정합니다.",
         "tags": ["receipt", "ocr", "nutrition-label"], "examples": ["이 배달 영수증 기록해 줘"],
         "inputModes": ["image/png", "image/jpeg", "application/json"], "outputModes": ["application/json"]},
        {"id": "query-meals", "name": "식단 조회 (Query meals)",
         "description": "data {\"skill\": \"query-meals\", \"start\": \"YYYY-MM-DD\", \"end\": \"YYYY-MM-DD\"} (기본: 최근 7일) -> "
                        "날짜별 끼니·메뉴·kcal 범위와 kcal 모르는 끼니 수.",
         "tags": ["meal", "kcal", "report"], "examples": ["지난주에 뭐 먹었어?"],
         "inputModes": ["application/json"], "outputModes": ["application/json"]},
        {"id": "sync-card", "name": "카드 결제 가져오기 (Sync card payments)",
         "description": "data {\"skill\": \"sync-card\"} -> card_spending에서 식비 결제를 가져와 끼니로 나눕니다.",
         "tags": ["sync"], "examples": [], "inputModes": ["application/json"], "outputModes": ["application/json"]},
    ],
}


def _parts(message: dict) -> tuple[str, dict, list[tuple[str, bytes]]]:
    """메시지 -> (글자, data 합친 것, 파일들)."""
    texts, data, files = [], {}, []
    for p in message.get("parts") or []:
        if not isinstance(p, dict):
            continue
        if isinstance(p.get("text"), str):
            texts.append(p["text"])
        elif "raw" in p:
            try:
                files.append((p.get("filename") or "image", base64.b64decode(p["raw"], validate=True)))
            except ValueError as exc:
                raise a2a.invalid_params("raw is not valid base64") from exc
        elif "url" in p:
            u = urllib.parse.urlparse(str(p["url"]))
            if u.scheme != "file":
                raise a2a.content_type_not_supported("url parts: only file:// on this machine")
            path = Path(urllib.parse.unquote(u.path))
            files.append((p.get("filename") or path.name, _read_local(path)))
        elif isinstance(p.get("data"), dict):
            data.update(p["data"])
    if data.get("path"):
        path = Path(str(data["path"]))
        files.append((path.name, _read_local(path)))
    return " ".join(t.strip() for t in texts if t.strip()), data, files


def _read_local(path: Path) -> bytes:
    if not path.is_absolute() or not path.is_file():
        raise a2a.invalid_params(f"파일이 없습니다: {path} (이 PC의 절대 경로)")
    return path.read_bytes()


def _date(v, what: str) -> Optional[dt.date]:
    if v in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError as exc:
        raise a2a.invalid_params(f"{what}는 YYYY-MM-DD") from exc


class MealTasks:
    """A2A TaskHandler: 한 작업 = 한 메시지로 맡긴 일. 상태는 서비스의 SQLite(a2a_tasks)에."""

    def __init__(self, svc: MealService) -> None:
        self.svc = svc
        self.store = svc.store

    # ---------------------------------------------------------------- TaskHandler
    def send(self, message: dict, configuration: dict, base: str) -> dict:
        if message.get("taskId"):
            return self._follow_up(str(message["taskId"]), message, base)
        mid = message.get("messageId")
        if mid:
            old = self.store.task_by_message(str(mid))
            if old:                                  # 같은 요청 2회 -> 처음 작업 (새 끼니·캡처를 또 만들지 않음)
                return self._view(old, base)
        text, data, files = _parts(message)
        skill = data.get("skill") or (message.get("metadata") or {}).get("skill") or ("read-receipt" if files else "log-meal")
        if skill not in SKILLS:
            raise a2a.invalid_params(f"skill은 {', '.join(SKILLS)} 중 하나")
        tid = uuid.uuid4().hex[:16]
        ctx = str(message.get("contextId") or tid)
        request = json.dumps({"text": text, "data": {k: v for k, v in data.items() if k != "path"},
                              "files": [n for n, _ in files]}, ensure_ascii=False)
        self.store.add_task(tid, context_id=ctx, message_id=str(mid) if mid else None, skill=skill, state="working",
                            request=request)
        try:
            run = {"log-meal": lambda: self._log_meal(tid, text, data), "read-receipt": lambda: self._read(tid, files),
                   "query-meals": lambda: self._query(tid, data), "sync-card": lambda: self._sync(tid)}[skill]
            run()
        except Unavailable as exc:                   # 모델·card_spending 꺼짐 -> 사람이 알 수 있게 실패 작업으로 남긴다
            self.store.update_task(tid, state="failed", error=exc.message)
        except a2a.A2AError as exc:                  # 형식 오류 -> 작업은 거부로 남기고 JSON-RPC 에러로도 알린다
            self.store.update_task(tid, state="rejected", error=exc.message)
            raise
        except ServiceError as exc:
            self.store.update_task(tid, state="rejected", error=exc.message)
        return self._view(self.store.task(tid), base)

    def get(self, task_id: str, base: str) -> dict:
        return self._view(self._task(task_id), base)

    def list(self, params: dict, base: str) -> list[dict]:
        want = params.get("status")
        views = [self._view(t, base) for t in self.store.tasks(params.get("contextId"))]
        return [v for v in views if not want or v["status"]["state"] == want]

    def cancel(self, task_id: str, base: str) -> dict:
        t = self._task(task_id)
        state = self._state(t)
        if state != "input_required":
            raise a2a.not_cancelable(task_id, STATE[state])
        if t["skill"] == "read-receipt":             # 화면의 확인할 캡처에서 치운다 (되돌릴 수 있음)
            for cid in json.loads(t["pending"] or "{}").get("captures", []):
                if (self.store.capture(cid) or {}).get("status") == "read":
                    self.svc.set_capture_status(cid, "dismissed")
        self.store.update_task(task_id, state="canceled")
        return self._view(self.store.task(task_id), base)

    # ---------------------------------------------------------------- 기능
    def _log_meal(self, tid: str, text: str, data: dict) -> None:
        if not text:
            raise Invalid("먹은 것을 글자(text part)로 적어 주세요")
        parsed = self.svc.chat_parse(text, day=_date(data.get("date"), "date"), today=_date(data.get("today"), "today"))
        if not parsed["entries"]:
            self.store.update_task(tid, state="rejected", error="먹은 내용을 찾지 못했습니다")
            return
        for e in parsed["entries"]:
            if not e["slot"] and data.get("slot") in slots.EATING:
                e["slot"] = data["slot"]
        self._finish_or_ask(tid, parsed["entries"])

    def _finish_or_ask(self, tid: str, entries: list[dict]) -> None:
        need = self._needs(entries)
        if need:
            self.store.update_task(tid, state="input_required", pending=json.dumps({"entries": entries, "need": need},
                                                                                    ensure_ascii=False))
            return
        saved = [self.svc.apply_entry(e, note="A2A") for e in entries]
        self.store.update_task(tid, state="completed", pending=None,
                               result=json.dumps({"meals": [s["meal_id"] for s in saved],
                                                  "filled": [s["meal_id"] for s in saved if s["filled"]]}))

    @staticmethod
    def _needs(entries: list[dict]) -> dict:
        need: dict = {}
        if any(not e["slot"] for e in entries):
            need["slot"] = {"options": list(slots.EATING), "send": {"slot": "저녁"}}
        bad = [{"name": i["name"], "said_unit": i["said_unit"], "table_unit": i["unit"], "said_qty": i["qty"]}
               for e in entries for i in e["items"] if i["unit_mismatch"]]
        if bad:
            need["qty"] = {"items": bad, "send": {"items": [{"name": b["name"], "qty": 0.5} for b in bad]},
                           "note": "말한 단위가 기준표 단위와 다릅니다. 기준표 단위로 센 수량을 보내 주세요"}
        return need

    def _read(self, tid: str, files: list[tuple[str, bytes]]) -> None:
        if not files:
            raise Invalid("이미지가 없습니다: file part {raw, filename} 또는 data {\"path\"}")
        r = self.svc.upload_captures(files, origin="a2a")
        if not r.captures:
            raise Invalid("; ".join(f"{e['filename']}: {e['message']}" for e in r.errors) or "읽을 이미지가 없음")
        self.store.update_task(tid, state="input_required", pending=json.dumps({"captures": [c["id"] for c in r.captures],
                                                                                "errors": r.errors}, ensure_ascii=False))

    def _query(self, tid: str, data: dict) -> None:
        end = _date(data.get("end"), "end") or dt.date.today()
        start = _date(data.get("start"), "start") or end - dt.timedelta(days=6)
        if start > end or (end - start).days > 92:
            raise Invalid("기간은 start ≤ end, 93일까지")
        self.store.update_task(tid, state="completed", result=json.dumps({"start": start.isoformat(), "end": end.isoformat()}))

    def _sync(self, tid: str) -> None:
        self.store.update_task(tid, state="completed", result=json.dumps(self.svc.sync(), ensure_ascii=False))

    # ---------------------------------------------------------------- 이어서 보낸 메시지 (INPUT_REQUIRED에 답)
    def _follow_up(self, task_id: str, message: dict, base: str) -> dict:
        t = self._task(task_id)
        if self._state(t) != "input_required":
            raise a2a.invalid_params(f"이 작업은 더 받을 것이 없습니다 (state {STATE[self._state(t)]})")
        text, data, _ = _parts(message)
        pending = json.loads(t["pending"] or "{}")
        try:
            if t["skill"] == "log-meal":
                slot = data.get("slot") or (text if text in slots.EATING else None)
                for e in pending["entries"]:
                    if not e["slot"] and slot in slots.EATING:
                        e["slot"] = slot
                    for it in e["items"]:
                        fix = next((x for x in data.get("items") or [] if x.get("name") == it["name"]), None)
                        if fix and isinstance(fix.get("qty"), (int, float)) and 0 < fix["qty"] <= 20:
                            it["qty"], it["unit_mismatch"] = float(fix["qty"]), False
                self._finish_or_ask(task_id, pending["entries"])
            elif t["skill"] == "read-receipt" and data.get("confirm"):
                for cid in pending.get("captures", []):
                    self._confirm_capture(cid, data)
            else:
                raise a2a.invalid_params("보낼 수 있는 것: log-meal은 {slot}·{items}, read-receipt는 {confirm: true}")
        except ServiceError as exc:
            raise a2a.invalid_params(exc.message) from exc
        return self._view(self.store.task(task_id), base)

    def _confirm_capture(self, cid: str, data: dict) -> None:
        v = self.svc.capture(cid)
        if v["status"] != "read":
            return
        if v["kind"] == "label":
            self.svc.save_label(cid)
            return
        items = [{"name": i["name"], "qty": i["qty"]} for i in v["items"]]
        if v["candidates"]:
            self.svc.confirm_capture(cid, target=str(v["candidates"][0]["id"]), items=items, store_name=v["store"])
        else:
            slot = data.get("slot") or v["guess_slot"]
            day = _date(data.get("date"), "date") or (dt.date.fromisoformat(v["date"]) if v["date"] else None)
            if not day or not slot:
                raise Invalid(f"캡처 {cid}: 짝지을 카드 결제가 없고 날짜·끼니를 못 읽었습니다 -- data {{date, slot}}를 같이 보내 주세요")
            self.svc.confirm_capture(cid, target="new", items=items, day=day, slot=slot, store_name=v["store"])

    # ---------------------------------------------------------------- 보기
    def _task(self, task_id: str) -> dict:
        t = self.store.task(task_id)
        if not t:
            raise a2a.task_not_found(task_id)
        return t

    def _state(self, t: dict) -> str:
        """read-receipt는 캡처 상태에서 매번 다시 정한다 -- 사람이 화면에서 확정·치우면 작업 상태도 바뀐다."""
        if t["skill"] != "read-receipt" or t["state"] != "input_required":
            return t["state"]
        st = [(self.store.capture(c) or {}).get("status") for c in json.loads(t["pending"] or "{}").get("captures", [])]
        if any(s == "read" for s in st):
            return "input_required"
        return "completed" if any(s == "confirmed" for s in st) else "canceled"

    def _view(self, t: dict, base: str) -> dict:
        state = self._state(t)
        req, pending = json.loads(t["request"] or "{}"), json.loads(t["pending"] or "{}")
        result = json.loads(t["result"] or "{}")
        meta = {"skill": t["skill"], "reviewUrl": f"{base}/"}
        arts, text = [], None
        if state in ("failed", "rejected"):
            text = t["error"]
        elif state == "canceled":
            text = "취소됨"
        elif t["skill"] == "log-meal":
            if state == "input_required":
                need = pending["need"]
                text = ("끼니를 알려 주세요 (아침·점심·저녁·간식·야식). " if "slot" in need else "") + \
                       (need["qty"]["note"] if "qty" in need else "")
                arts.append({"artifactId": "question", "name": "question",
                             "parts": [{"data": {"need": need, "entries": pending["entries"]}, "mediaType": "application/json"}]})
                meta["reviewUrl"] = f"{base}/#{pending['entries'][0]['date']}"
            else:
                meals = [self._meal_or_none(i) for i in result.get("meals", [])]
                meals = [m for m in meals if m]
                if meals:
                    meta["reviewUrl"] = f"{base}/#{meals[0]['date']}"
                text = "기록했습니다: " + ", ".join(f"{m['date']} {m['slot']} {'·'.join(i['name'] for i in m['items'])}" for m in meals) + \
                       (f" (카드 결제 {len(result.get('filled', []))}건에 채움)" if result.get("filled") else "") + \
                       f". 확인·수정: {meta['reviewUrl']}"
                arts.append({"artifactId": "meals", "name": "meals",
                             "parts": [{"data": {"meals": [self._meal_brief(m) for m in meals]}, "mediaType": "application/json"}]})
        elif t["skill"] == "read-receipt":
            caps = []
            for cid in pending.get("captures", []):
                try:
                    caps.append(self.svc.capture(cid))
                except Missing:
                    continue
            if caps:
                meta["reviewUrl"] = f"{base}/#capture={caps[0]['id']}"
                meta["captureIds"] = [c["id"] for c in caps]
            text = (f"캡처 {len(caps)}장을 읽어 화면의 '확인할 캡처'에 올렸습니다. 확인·확정: {meta['reviewUrl']} "
                    "(또는 같은 taskId로 data {\"confirm\": true})") if state == "input_required" else \
                   f"캡처 {len(caps)}장 처리됨 ({'확정' if state == 'completed' else '치움'})"
            arts.append({"artifactId": "captures", "name": "captures",
                         "parts": [{"data": {"captures": [self._capture_brief(c) for c in caps],
                                             "errors": pending.get("errors", [])}, "mediaType": "application/json"}]})
        elif t["skill"] == "query-meals":
            days = self._days(dt.date.fromisoformat(result["start"]), dt.date.fromisoformat(result["end"]))
            meta["reviewUrl"] = f"{base}/#{result['end']}"
            known = [d for d in days if d["kcal"] and not d["unknown"]]
            text = f"{result['start']} ~ {result['end']}: 끼니 {sum(len(d['meals']) for d in days)}개" + \
                   (f", kcal를 다 아는 날 {len(known)}일 하루 평균 약 {round(sum(d['kcal']['mid'] for d in known) / len(known))} kcal"
                    if known else ", kcal를 다 아는 날 없음")
            arts.append({"artifactId": "days", "name": "days", "parts": [{"data": {"start": result["start"], "end": result["end"],
                                                                                   "days": days}, "mediaType": "application/json"}]})
        elif t["skill"] == "sync-card":
            text = f"식비 결제 {result.get('fetched', 0)}건 확인 · 새로 {result.get('added', 0)} · 취소돼 뺌 {result.get('removed', 0)}"
            arts.append({"artifactId": "sync", "name": "sync", "parts": [{"data": result, "mediaType": "application/json"}]})
        if req.get("text"):
            meta["request"] = req["text"]
        return a2a.task(t["id"], t["context_id"], STATE[state], text=text, artifacts=arts or None,
                        timestamp=t["updated_at"], metadata=meta)

    def _meal_or_none(self, mid: int) -> Optional[dict]:
        try:
            return self.svc.meal(mid)
        except Missing:
            return None                              # 사람이 화면에서 지웠음

    @staticmethod
    def _meal_brief(m: dict) -> dict:
        return {"id": m["id"], "date": m["date"], "slot": m["slot"], "place": m.get("store") or m["place"],
                "items": [{"name": i["name"], "qty": i["qty"]} for i in m["items"]], "people": m["people"],
                "kcal_per_person": m["kcal"], "excluded": bool(m["excluded"])}

    @staticmethod
    def _capture_brief(c: dict) -> dict:
        return {"id": c["id"], "status": c["status"], "kind": c["kind"], "store": c["store"], "date": c["date"],
                "time": c["time"], "total": c["total"], "items": [{"name": i["name"], "qty": i["qty"]} for i in c["items"]],
                "label": c["label"], "matched_payments": len(c["candidates"]), "meal": c["meal"]}

    def _days(self, start: dt.date, end: dt.date) -> list[dict]:
        ms = self.svc.meals(start, end)
        out = []
        d = start
        while d <= end:
            day = [m for m in ms if m["date"] == d.isoformat()]
            eaten = [m for m in day if m["slot"] in slots.EATING and not m["skipped"] and not m["excluded"]]
            known = [m["kcal"] for m in eaten if m["kcal"]]
            out.append({"date": d.isoformat(),
                        "kcal": {k: sum(x[k] for x in known) for k in ("low", "mid", "high")} if known else None,
                        "unknown": sum(1 for m in eaten if not m["kcal"] or m["kcal"]["partial"]),
                        "meals": [self._meal_brief(m) for m in eaten]})
            d += dt.timedelta(days=1)
        return out


def mount(app, svc: MealService) -> None:
    a2a.mount_a2a(app, CARD, MealTasks(svc))
