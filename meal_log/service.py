"""서비스 층 -- meal_log 상태(SQLite 한 파일)를 다루는 동작의 유일한 자리 (REF/AGENT_PRODUCT.md 규칙 2·3).

제품 API(app.py)와 A2A 창구(a2a_tasks.py)가 **같은 함수**를 부른다. 그래서 A2A로 들어온 일도 화면에 보이고 사람이 이어서 고친다.
검증·에러 분류는 여기서 한 번만: 실패는 HTTP가 아닌 이름 있는 예외(``ServiceError`` 하위)로 던지고, 창구가 각자 형식으로 바꾼다.
외부 의존(card_spending·로컬 LLM·OCR 서버)은 주입받거나 처음 쓸 때 연결 -- 꺼져 있어도 서비스는 뜨고 그 기능만 ``Unavailable``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from meal_log import analysis, chat, memory, menu_kcal, receipts, slots, suggest
from meal_log.llm import LLMBadOutput, LLMDown, LocalLLM, make_local_llm
from meal_log.sources import CardSource, CardSourceDown, CardSourceError, HttpCardSource
from meal_log.store import NotAllowed, NotFound, Store

MAX_IMAGE_BYTES = 15 * 1024 * 1024     # 휴대폰 캡처 한 장은 보통 0.5~3MB
MAX_IMAGE_PIXELS = 30_000_000          # 아주 긴 스크롤 캡처까지 (1080 × 약 27,000)
CaptureStatus = Literal["read", "dismissed", "confirmed"]
Origin = Literal["screen", "a2a"]


# ---------------------------------------------------------------- 실패 표식 (창구가 HTTP·JSON-RPC로 바꾼다)
class ServiceError(Exception):
    http = 500
    kind: Optional[str] = None

    def __init__(self, message: str, kind: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        if kind:
            self.kind = kind


class Invalid(ServiceError):
    http = 400


class Missing(ServiceError):
    http = 404


class Conflict(ServiceError):
    http = 409


class Unavailable(ServiceError):
    """외부 의존이 꺼져 있음 (kind: down=card_spending · llm_down · ocr_down) -- 켜는 법을 message에."""
    http = 503


class Upstream(ServiceError):
    """외부 의존이 이상한 답을 줌 (kind: bad_response · bad_llm_output)."""
    http = 502


@dataclass
class UploadResult:
    captures: list[dict]
    errors: list[dict]


def _to_png(content: bytes) -> bytes:
    """올린 파일 -> PNG (OCR 서버가 PNG를 받음). 이미지가 아니거나 너무 크면 ValueError."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        im = Image.open(io.BytesIO(content))
        if im.width * im.height > MAX_IMAGE_PIXELS:
            raise ValueError(f"이미지가 너무 큼 ({im.width}×{im.height})")
        im.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("이미지 파일이 아님 (png·jpg 등)") from exc
    buf = io.BytesIO()
    ImageOps.exif_transpose(im).convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _check_time(t: Optional[str]) -> None:
    if t and slots.minutes(t) is None:
        raise Invalid("시각은 HH:MM (예: 12:30)")


class MealService:
    def __init__(self, db_path: str | Path, *, source: Optional[CardSource] = None, llm: Optional[LocalLLM] = None,
                 llm_url: Optional[str] = None, ocr=None, ocr_url: Optional[str] = None,
                 self_url: str = "http://127.0.0.1:8766", backends: Optional[dict] = None) -> None:
        """``backends``: 대화 세션 구현 주입 {"claude": ..., "local": ...} (테스트). 없으면 실제 Claude Code·로컬 vLLM.
        ``self_url``: 이 서버 주소 -- Claude 세션의 MCP 다리가 도구를 부를 곳."""
        self.store = Store(db_path)
        self.source: CardSource = source or HttpCardSource()
        self._llm, self._llm_url = llm, llm_url
        self._ocr, self._ocr_url = ocr, ocr_url
        from meal_log.chat_backends import ClaudeBackend, LocalBackend
        self.backends = backends or {"local": LocalBackend(self, self._get_llm, ping=self._ping_llm),
                                     "claude": ClaudeBackend(self_url)}
        import threading
        self._chat_locks: dict[str, threading.Lock] = {}

    # ---------------------------------------------------------------- 외부 의존 (처음 쓸 때 연결)
    def _get_llm(self) -> LocalLLM:
        if self._llm is None:
            if not self._llm_url:
                raise LLMDown("로컬 LLM 주소가 설정되지 않음 (serve --llm-url)")
            self._llm = make_local_llm(self._llm_url)
        return self._llm

    def _get_ocr(self):
        """doc_parser의 OCR 서버(doc_reader/doc_parser/tools/ocr.sh start). OCR 서버가 나중에 켜져도 되게 처음 쓸 때 연결."""
        from doc_parser import HttpOcr, OcrUnavailable

        if self._ocr is None:
            if not self._ocr_url:
                raise Unavailable("OCR 서버 주소가 설정되지 않음 (serve --ocr-url) -- meal_log 폴더에서 bash ../doc_reader/doc_parser/tools/ocr.sh start",
                                  "ocr_down")
            reader = HttpOcr(self._ocr_url, timeout=120)
            try:
                reader.check()
            except OcrUnavailable as exc:
                raise Unavailable(f"{exc} -- meal_log 폴더에서 bash ../doc_reader/doc_parser/tools/ocr.sh start", "ocr_down") from exc
            self._ocr = reader
        return self._ocr

    def _ping_llm(self) -> bool:
        """로컬 vLLM이 지금 답하는지 (대화 세션 선택지를 잠글지 정할 때). 클라이언트를 이미 만들었어도 서버가 내려갔을 수 있다."""
        if self._llm is not None and not self._llm_url:
            return True                              # 주입한 가짜
        if not self._llm_url:
            return False
        import urllib.request
        try:
            urllib.request.urlopen(f"{self._llm_url.rstrip('/')}/models", timeout=2).read()
            return True
        except OSError:
            return False

    @staticmethod
    def _llm_error(exc: Exception) -> ServiceError:
        if isinstance(exc, LLMDown):
            return Unavailable(f"{exc} -- 로컬 vLLM(:8002)을 켜 주세요 (REF/LLM.md)", "llm_down")
        return Upstream(str(exc), "bad_llm_output")

    # ---------------------------------------------------------------- 상태·카드 결제
    def status(self) -> dict:
        span = self.store.meals()
        return {"card_url": self.source.url, "last_sync": self.store.last_sync(), "meals": len(span),
                "first_date": span[0]["date"] if span else None, "last_date": span[-1]["date"] if span else None}

    def sync(self) -> dict:
        try:
            fetched = self.source.fetch()
        except CardSourceDown as exc:
            raise Unavailable(f"{exc} -- card_spending 폴더에서 python -m card_spending serve", "down") from exc
        except CardSourceError as exc:
            raise Upstream(str(exc), "bad_response") from exc
        return self.store.sync(fetched)

    # ---------------------------------------------------------------- 설정
    def settings(self) -> dict:
        s = self.store.slot_settings()
        return {"windows": s.windows, "snack_meal_min_krw": s.snack_meal_min_krw, "defaults": slots.DEFAULT_WINDOWS,
                "default_snack_meal_min_krw": slots.DEFAULT_SNACK_MEAL_MIN_KRW,
                "review_since": analysis.review_since(self.store), "backlog_warn": analysis.backlog_warn(self.store),
                "default_backlog_warn": analysis.DEFAULT_BACKLOG_WARN}

    def put_settings(self, windows: dict, snack_meal_min_krw: int, backlog_warn: Optional[int]) -> dict:
        s = slots.SlotSettings(windows=dict(windows), snack_meal_min_krw=snack_meal_min_krw)
        errs = slots.validate(s)
        if errs:
            raise Invalid("; ".join(errs))
        if backlog_warn is not None:
            self.store.set_setting("backlog_warn", str(backlog_warn))
        return {"moved": self.store.set_slot_settings(s)} | self.settings()

    # ---------------------------------------------------------------- 메뉴 (기준표 + 내 메뉴)
    def menus(self) -> list[dict]:
        return [{"name": m.name, "unit": m.unit, "low": m.low, "mid": m.mid, "high": m.high, "source": m.source,
                 "db_rows": m.db_rows, "basis": m.basis, "custom": m.source in menu_kcal.CUSTOM_SOURCES}
                for m in analysis.all_menus(self.store).values()]

    def compose_view(self, name: str, menus: Optional[dict] = None) -> Optional[dict]:
        """이름을 풀어 계산한 kcal 추정 (compose.py) -- 화면·대화 도우미가 같은 모양으로 쓴다. 풀 수 없으면 None."""
        from meal_log import compose
        menus = menus if menus is not None else analysis.all_menus(self.store)
        e = compose.estimate(name, menus)
        if e is None:
            return None
        # 단위: 값이 가리키는 양 ("2인분"이라 적힌 메뉴는 2인분 값, 곱빼기·미니는 그 한 그릇)
        unit = "1인분" if e.mult == 1 else f"{e.mult:g}인분" if e.mult == int(e.mult) else "1그릇"
        return {"name": e.name, "frame": e.frame, "low": e.low, "mid": e.mid, "high": e.high, "unit": unit,
                "parts": [{"label": p.label, "detail": p.detail, "low": p.low, "mid": p.mid, "high": p.high} for p in e.parts],
                "notes": e.notes, "rough": bool(e.unknown), "basis": e.basis()}

    def compose(self, name: str) -> dict:
        if not name.strip():
            raise Invalid("메뉴 이름이 비었음")
        v = self.compose_view(name.strip())
        if v is None:
            raise Missing(f"'{name}'은(는) 이름으로 풀 수 없음 (끝말로 요리 종류를 못 찾았거나 이미 기준표에 있는 메뉴)")
        return v

    def put_menu(self, name: str, *, unit: str, low: int, mid: Optional[int], high: int, source: str,
                 basis: Optional[str]) -> dict:
        """내 메뉴 저장 (기준표에 없는 이름만). previous = 바꾸기 전 값 (되돌리기용)."""
        name = name.strip()
        if not name or len(name) > 30:
            raise Invalid("메뉴 이름은 1~30자")
        if name in menu_kcal.MENUS:
            raise Conflict(f"'{name}'은(는) 기준표에 이미 있는 메뉴 -- 끼니의 kcal를 직접 넣으려면 끼니에서")
        mid = mid if mid is not None else round((low + high) / 2)
        if not (low <= mid <= high) or high == 0:
            raise Invalid("kcal는 낮음 ≤ 보통 ≤ 높음, 높음은 0보다 커야 함")
        prev = self.store.put_user_menu(name, unit=unit.strip(), low=low, mid=mid, high=high, source=source, basis=basis)
        return {"menu": self.store.user_menu(name), "previous": prev}

    def delete_menu(self, name: str) -> dict:
        if name in menu_kcal.MENUS:
            raise Invalid("기준표 메뉴는 지울 수 없음")
        try:
            return {"deleted": self.store.delete_user_menu(name)}
        except NotFound as exc:
            raise Missing(str(exc)) from exc

    def estimate_menu(self, name: str, unit: Optional[str]) -> dict:
        """로컬 LLM에 메뉴 이름·단위(+ 비슷한 기준표 메뉴 값)만 보내 kcal 범위 제안 (저장 안 함)."""
        try:
            return suggest.estimate_kcal(self._get_llm(), name.strip(), (unit or "").strip() or None,
                                         menus=analysis.all_menus(self.store).values())
        except (LLMDown, LLMBadOutput) as exc:
            raise self._llm_error(exc) from exc

    def similar(self, name: str, menus: Optional[dict] = None) -> list[dict]:
        """kcal 모르는 품목에 붙일 비슷한 기준표 메뉴 (가게 이름 규칙 -> 이름 조각 겹침). LLM 없이 쓸 수 있는 참고값."""
        menus = menus if menus is not None else analysis.all_menus(self.store)
        out = []
        g = menu_kcal.guess_menu(name)
        for m in ([menus[g]] if g in menus else []) + [m for m in suggest.similar_menus(name, menus.values(), k=3)
                                                        if m.name not in suggest.ANCHORS]:
            if m.name not in {x["name"] for x in out}:
                out.append({"name": m.name, "unit": m.unit, "low": m.low, "mid": m.mid, "high": m.high, "source": m.source})
        return out[:3]

    # ---------------------------------------------------------------- 끼니
    def meals(self, start: Optional[dt.date] = None, end: Optional[dt.date] = None) -> list[dict]:
        return self._with_suggestions(analysis.meals_between(self.store, start, end))

    def _with_suggestions(self, ms: list[dict]) -> list[dict]:
        """메뉴 모르는 카드 끼니에 경험 기억 제안(``suggest``)을 붙인다. 기억은 전체 확정 기록에서 매번 계산 (memory.py)."""
        targets = [m for m in ms if memory.wants_suggestion(m)]
        if not targets:
            return ms
        mem = memory.Memory(analysis.meals_between(self.store, None, None))
        rejects = self.store.memory_rejects()
        for m in targets:
            m["suggest"] = [{"signature": s.signature, "items": [{"name": n, "qty": q} for n, q in s.items], "store": s.store,
                             "times": s.times, "of": s.of, "last": s.last, "basis": s.basis}
                            for s in mem.suggest(m, rejects.get(m["id"], ()))]
        return ms

    def reject_suggestion(self, meal_id: int, signature: str, *, undo: bool = False) -> dict:
        """[틀림]: 이 끼니에 이 메뉴 묶음을 다시 제안하지 않는다. undo=True면 되돌리기."""
        self.meal(meal_id)   # 없는 끼니면 Missing
        if not signature.strip():
            raise Invalid("signature가 필요")
        self.store.set_memory_reject(meal_id, signature, not undo)
        return self.meal(meal_id)

    def meal(self, meal_id: int) -> dict:
        m = next((x for x in analysis.meals_between(self.store, None, None) if x["id"] == meal_id), None)
        if m is None:
            raise Missing(f"없는 끼니: {meal_id}")
        return self._with_suggestions([m])[0]

    def add_meal(self, *, day: dt.date, slot: str, items: list[dict], people: int = 1, place: Optional[str] = None,
                 note: Optional[str] = None, time: Optional[str] = None, skipped: bool = False) -> int:
        _check_time(time)
        if not skipped and not items:
            raise Invalid("메뉴를 하나 이상 넣거나 '건너뜀'으로")
        if skipped and slot not in slots.EATING:
            raise Invalid("건너뜀은 끼니에만")
        return self.store.add_manual(day=day, slot=slot, items=items, people=people, place=place or None,
                                     note=note or None, time=time or None, skipped=skipped)

    def change_meal(self, meal_id: int, changes: dict) -> dict:
        for k in ("date", "slot", "people"):
            if k in changes and changes[k] is None:
                raise Invalid(f"{k}는 비울 수 없음")
        _check_time(changes.get("time"))
        if isinstance(changes.get("date"), dt.date):
            changes["date"] = changes["date"].isoformat()
        try:
            self.store.update_meal(meal_id, changes)
        except NotFound as exc:
            raise Missing(str(exc)) from exc
        except NotAllowed as exc:
            raise Invalid(str(exc)) from exc
        return self.meal(meal_id)

    def delete_meal(self, meal_id: int) -> dict:
        try:
            self.store.delete_meal(meal_id)
        except NotFound as exc:
            raise Missing(str(exc)) from exc
        except NotAllowed as exc:
            raise Invalid(str(exc)) from exc
        return {"deleted": meal_id}

    # ---------------------------------------------------------------- 확인·캘린더·주별
    def review(self) -> dict:
        r = analysis.review(self.store)
        self._with_suggestions(r["rows"])
        return r

    def set_review_since(self, day: Optional[dt.date]) -> dict:
        """정리 기준일 옮기기 ("○일 이전은 모름으로 두기"). 데이터는 바꾸지 않는다 -- previous로 되돌릴 수 있다."""
        previous = analysis.review_since(self.store)
        self.store.set_setting("review_since", day.isoformat() if day else "")
        return {"previous": previous} | analysis.review(self.store)

    def calendar(self, month: str) -> dict:
        return analysis.calendar(self.store, month)

    def weekly(self, end: Optional[dt.date], weeks: int) -> dict:
        return analysis.weekly(self.store, end, weeks)

    # ---------------------------------------------------------------- 채팅 입력 해석
    def item_view(self, it: chat.Item, menus: dict) -> dict:
        m = menus.get(it.name)
        # 말한 단위가 기준표 단위에 없으면("피자 3조각" vs "라지 1판") kcal를 곱하지 않고 수량 확인을 요청
        mismatch = bool(m and it.unit and not chat.unit_ok(it.unit, m.unit))
        return {"name": it.name, "qty": it.qty, "said_unit": it.unit, "known": m is not None, "unit": m.unit if m else None,
                "unit_mismatch": mismatch, "low": m.low * it.qty if m and not mismatch else None,
                "high": m.high * it.qty if m and not mismatch else None, "source": m.source if m else None}

    def chat_parse(self, text: str, *, day: Optional[dt.date] = None, today: Optional[dt.date] = None,
                   force_llm: bool = False) -> dict:
        """문장 해석만 (저장 안 함). 끼니마다 채울 수 있는 카드 결제 후보(같은 날, 메뉴 모르는 결제)를 같이 준다."""
        today = today or dt.date.today()
        if not text.strip():   # 공백만 -> LLM까지 보내지 않는다
            raise Invalid("먹은 것을 적어 주세요")
        menus = analysis.all_menus(self.store)
        try:
            parsed = chat.parse(text, today, day or today, self._get_llm, force_llm=force_llm, menus=menus)
        except (LLMDown, LLMBadOutput) as exc:
            raise self._llm_error(exc) from exc
        entries = []
        for e in parsed.entries:
            meals_day = analysis.meals_between(self.store, e.date, e.date)
            cands = [{"id": m["id"], "slot": m["slot"], "place": m["place"], "time": m["time"], "amount": m["amount"]}
                     for m in meals_day if m["source"] == "card" and analysis.counted(m) and not m["items"]]
            entries.append({"date": e.date.isoformat(), "slot": e.slot, "items": [self.item_view(i, menus) for i in e.items],
                            "fill_candidates": cands})
        return {"via": parsed.via, "seconds": parsed.seconds, "loose": parsed.loose, "entries": entries}

    def apply_entry(self, entry: dict, *, note: Optional[str] = None) -> dict:
        """해석된 끼니 하나를 저장: 같은 날·끼니에 메뉴 모르는 카드 결제가 있으면 거기 채우고, 없으면 새 끼니.
        화면은 사람이 고른 대상으로 기존 API(PUT·POST)를 부르고, A2A는 이 함수를 부른다."""
        if not entry.get("slot"):
            raise Invalid("끼니(아침·점심·저녁·간식·야식)가 필요")
        items = [{"name": i["name"], "qty": i["qty"]} for i in entry["items"]]
        if not items:
            raise Invalid("메뉴가 없음")
        cand = next((c for c in entry.get("fill_candidates", []) if c["slot"] == entry["slot"]), None)
        if cand:
            self.store.update_meal(cand["id"], {"items": items})
            return {"meal_id": cand["id"], "filled": True}
        mid = self.store.add_manual(day=dt.date.fromisoformat(entry["date"]), slot=entry["slot"], items=items, note=note)
        return {"meal_id": mid, "filled": False}

    # ---------------------------------------------------------------- 대화 도우미 (세션 2종)
    def assistant_backends(self) -> list[dict]:
        out = []
        for name, label in (("local", "로컬 LLM"), ("claude", "Claude Code")):
            ok, why = self.backends[name].available()
            out.append({"id": name, "label": label, "available": ok, "reason": why,
                        "sends_out": name == "claude",
                        "notice": "대화 내용과 식단(끼니·메뉴·kcal)이 Anthropic으로 나갑니다. 결제 금액·가게·카드 정보는 보내지 않습니다."
                        if name == "claude" else "이 PC 안에서만 처리합니다."})
        return out

    def conversations(self) -> list[dict]:
        return [self._conv_view(c, with_messages=False) for c in self.store.conversations()]

    def conversation(self, cid: str) -> dict:
        c = self.store.conversation(cid)
        if not c:
            raise Missing(f"없는 대화: {cid}")
        return self._conv_view(c, with_messages=True)

    def _conv_view(self, c: dict, *, with_messages: bool) -> dict:
        import json as _json
        v = {"id": c["id"], "backend": c["backend"], "title": c["title"] or "새 대화", "turns": c["turns"],
             "created_at": c["created_at"], "updated_at": c["updated_at"]}
        if with_messages:
            v["messages"] = [{"role": m["role"], "text": m["text"], "events": _json.loads(m["events"]) if m["events"] else [],
                              "cost": m["cost"], "created_at": m["created_at"]} for m in self.store.chat_messages(c["id"])]
        return v

    def create_conversation(self, backend: str) -> dict:
        import uuid
        if backend not in self.backends:
            raise Invalid("backend는 local 또는 claude")
        ok, why = self.backends[backend].available()
        if not ok:
            raise Unavailable(why, f"{backend}_down")
        cid = uuid.uuid4().hex[:16]
        self.store.add_conversation(cid, backend, str(uuid.uuid4()) if backend == "claude" else None)
        return self.conversation(cid)

    def delete_conversation(self, cid: str) -> dict:
        try:
            self.store.delete_conversation(cid)
        except NotFound as exc:
            raise Missing(str(exc)) from exc
        return {"deleted": cid}

    def send_message(self, cid: str, text: str, *, today: Optional[dt.date] = None):
        """사용자 말 하나 -> 그 대화의 세션이 낸 이벤트를 차례로 내보낸다 (화면 SSE). 사용자 말은 바로, 답은 끝날 때 저장.
        같은 대화에 동시에 두 번 보내지 못하게 막는다 (Claude 세션 --resume이 꼬이지 않게)."""
        import json as _json
        import threading

        from meal_log.chat_backends import Turn
        c = self.store.conversation(cid)
        if not c:
            raise Missing(f"없는 대화: {cid}")
        text = (text or "").strip()
        if not text:
            raise Invalid("보낼 말이 비었음")
        lock = self._chat_locks.setdefault(cid, threading.Lock())
        if not lock.acquire(blocking=False):
            raise Conflict("이 대화는 아직 답하는 중입니다")
        history = [{"role": m["role"], "text": m["text"]} for m in self.store.chat_messages(cid)]
        self.store.add_chat_message(cid, "user", text, title=text[:40])

        def events():
            parts, tool_events, cost, failed = [], [], None, False
            try:
                for ev in self.backends[c["backend"]].run(Turn(c, history, text, today or dt.date.today())):
                    if ev["type"] == "session":
                        self.store.mark_claude_started(cid)
                        continue
                    if ev["type"] == "text":
                        parts.append(ev["text"])
                    elif ev["type"] == "tool_result":
                        tool_events.append({"name": ev["name"], "ok": ev["ok"], "summary": ev["summary"], "dates": ev["dates"]})
                    elif ev["type"] == "done":
                        cost = ev.get("cost")
                    elif ev["type"] == "error":
                        failed = True
                        tool_events.append({"name": "error", "ok": False, "summary": ev["message"], "dates": []})
                    yield ev
            except GeneratorExit:   # 화면이 멈추기를 눌러 연결을 끊음 (app이 닫아 줌)
                failed = True
                tool_events.append({"name": "error", "ok": False, "summary": "멈춤", "dates": []})
                raise
            finally:
                answer = "".join(parts).strip() or ("(답하지 못함)" if failed else "")
                self.store.add_chat_message(cid, "assistant", answer, events=_json.dumps(tool_events, ensure_ascii=False),
                                            cost=cost, turn_done=not failed)
                lock.release()
        return events()

    # ---------------------------------------------------------------- 영수증·캡처·영양정보 라벨
    def capture_view(self, c: dict, menus: Optional[dict] = None, meals_all: Optional[list[dict]] = None) -> dict:
        menus = menus if menus is not None else analysis.all_menus(self.store)
        meals_all = meals_all if meals_all is not None else self.store.meals()
        r = receipts.parse_receipt(c["ocr_text"] or "")   # 읽은 글자에서 매번 다시 뽑는다 (규칙을 고치면 바로 반영)
        cands = receipts.match_candidates(r, meals_all) if c["status"] == "read" else []
        by_id = {m["id"]: m for m in meals_all}
        guess_slot = None
        lab = r.label
        if r.day:
            guess_slot = slots.place(r.day, r.time, "배달" if r.kind == "delivery" else "외식", r.total or 0,
                                     self.store.slot_settings())[0].slot
        return {
            "id": c["id"], "filename": c["filename"], "created_at": c["created_at"], "status": c["status"], "kind": r.kind,
            "origin": c.get("origin") or "screen",
            "store": r.store, "date": r.day.isoformat() if r.day else None, "time": r.time, "total": r.total,
            "menu_total": r.menu_total, "tip": r.tip, "discount": r.discount, "total_guess": r.total_guess,
            "guess_slot": guess_slot, "text": receipts.clean_ocr(c["ocr_text"] or ""), "fixed_date": r.fixed_date,
            "label": ({"name": lab.name, "weight": lab.weight, "unit": lab.unit, "kcal": round(lab.kcal) if lab.kcal else None,
                       "exists": bool(lab.name and lab.name in menus)} if lab else None),
            "items": [self.item_view(chat.Item(chat.match_menu(i.name, menus), i.qty), menus)
                      | {"price": i.price, "read_name": i.read_name or i.name, "option": i.option,
                         "compose": None if chat.match_menu(i.name, menus) in menus else self.compose_view(i.name, menus),
                         "similar": [] if chat.match_menu(i.name, menus) in menus else self.similar(i.name, menus)}
                      for i in r.items],
            "candidates": [{"id": m["id"], "date": m["date"], "slot": m["slot"], "pay_date": m["pay_date"], "time": m["time"],
                            "place": m["place"], "amount": m["amount"], "items": [it["name"] for it in m["items"]],
                            "exact": m.get("exact", True)}
                           for m in cands],
            "meal": ({"id": c["meal_id"], "date": by_id[c["meal_id"]]["date"], "slot": by_id[c["meal_id"]]["slot"]}
                     if c["meal_id"] in by_id else None),
        }

    def captures(self, status: CaptureStatus = "read") -> list[dict]:
        menus, meals_all = analysis.all_menus(self.store), self.store.meals()
        return [self.capture_view(c, menus, meals_all) for c in self.store.captures(status)]

    def capture(self, cid: str) -> dict:
        c = self.store.capture(cid)
        if not c:
            raise Missing(f"없는 캡처: {cid}")
        return self.capture_view(c)

    def capture_image(self, cid: str) -> Path:
        try:
            return self.store.capture_image(cid)
        except NotFound as exc:
            raise Missing(str(exc)) from exc

    def upload_captures(self, files: list[tuple[str, bytes]], *, origin: Origin = "screen") -> UploadResult:
        """여러 장 올리기 -> 같은 파일(내용 해시)은 다시 읽지 않음 -> OCR -> 확인 대기. 저장·짝짓기는 확정 때."""
        results, errors = [], []
        reader = None
        for name, content in files:
            name = (name or "image")[:120]
            if len(content) > MAX_IMAGE_BYTES:
                errors.append({"filename": name, "message": f"파일이 너무 큼 ({len(content) // 1024 // 1024}MB, 15MB까지)"})
                continue
            cid = hashlib.sha256(content).hexdigest()[:24]
            old = self.store.capture(cid)
            if old:
                results.append((old, True))
                continue
            try:
                png = _to_png(content)
            except ValueError as exc:
                errors.append({"filename": name, "message": str(exc)})
                continue
            reader = reader or self._get_ocr()     # OCR이 필요한 파일이 있을 때만 연결 (중복만 올리면 OCR 없이도 됨)
            try:
                items = reader.read(png, "page")
            except OSError as exc:                 # urllib 연결 끊김·시간 초과
                raise Unavailable(f"OCR 서버 응답 없음: {exc} -- meal_log 폴더에서 bash ../doc_reader/doc_parser/tools/ocr.sh start", "ocr_down") from exc
            self.store.add_capture(cid, filename=name, png=png, ocr_text=receipts.ocr_to_text(items), parsed="", origin=origin)
            results.append((self.store.capture(cid), False))
        menus, meals_all = analysis.all_menus(self.store), self.store.meals()
        views = []
        for c, dup in results:
            views.append(self.capture_view(c, menus, meals_all) | {"duplicate": dup})
        return UploadResult(views, errors)

    def confirm_capture(self, cid: str, *, target: str, items: list[dict], day: Optional[dt.date] = None,
                        slot: Optional[str] = None, store_name: Optional[str] = None) -> dict:
        """확정: 짝지은 카드 결제 끼니에 품목을 채우거나(가게 이름도) 새 끼니로 넣는다. previous = 되돌리기용 이전 값."""
        c = self.store.capture(cid)
        if not c:
            raise Missing(f"없는 캡처: {cid}")
        if not items:
            raise Invalid("메뉴가 없음")
        store_name = (store_name or "").strip() or None
        if target == "new":
            if not day or not slot:
                raise Invalid("새 끼니로 넣으려면 날짜와 끼니가 필요")
            mid = self.store.add_manual(day=day, slot=slot, items=items, place=store_name, note="영수증")
            previous = None
        else:
            try:
                mid = int(target)
                old = self.store.meal(mid)
            except (ValueError, NotFound) as exc:
                raise Missing(f"없는 끼니: {target}") from exc
            previous = {"items": [{"name": i["name"], "qty": i["qty"], "kcal": i["kcal_manual"]} for i in old["items"]],
                        "store": old["store"], "confirmed": bool(old["confirmed"])}
            self.store.update_meal(mid, {"items": items, "store": store_name})
        self.store.set_capture(cid, status="confirmed", meal_id=mid)
        return {"meal_id": mid, "previous": previous}

    def set_capture_status(self, cid: str, status: CaptureStatus) -> dict:
        try:
            self.store.set_capture(cid, status=status, meal_id=None)
        except NotFound as exc:
            raise Missing(str(exc)) from exc
        return {"id": cid, "status": status}

    def save_label(self, cid: str, name: Optional[str] = None) -> dict:
        """영양정보 라벨 캡처 -> 내 메뉴 (출처 포장지 영양정보) + 캡처는 반영함. 화면의 [내 메뉴로 저장]과 같은 일."""
        v = self.capture(cid)
        lab = v["label"]
        if v["kind"] != "label" or not lab or lab["kcal"] is None:
            raise Invalid("영양정보 라벨이 아니거나 kcal를 못 읽음")
        name = (name or lab["name"] or "").strip()
        unit = f"1팩({lab['weight']:g}{lab['unit']})" if lab["weight"] else "1팩"
        out = self.put_menu(name, unit=unit, low=lab["kcal"], mid=lab["kcal"], high=lab["kcal"], source="label",
                            basis=f"포장지 영양정보: 총 내용량 {lab['weight'] or '?'}{lab['unit']} {lab['kcal']}kcal")
        self.set_capture_status(cid, "confirmed")
        return out
