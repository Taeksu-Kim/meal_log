"""FastAPI 서버 (로컬 전용: __main__이 127.0.0.1에만 띄운다). 화면은 static/index.html 한 장.

이 파일은 창구만: 입력 모양 검사(pydantic) -> 서비스(service.py) 호출 -> 서비스의 실패 표식을 HTTP로. 동작은 서비스에만 있다.
A2A 창구(a2a_tasks.py)도 같은 서비스를 부른다 (REF/AGENT_PRODUCT.md).
"""

from __future__ import annotations

import datetime as dt
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from meal_log import chat, slots, suggest
from meal_log.llm import LocalLLM
from meal_log.service import CaptureStatus as CaptureStatusLiteral
from meal_log.service import MealService, ServiceError
from meal_log.sources import CardSource

STATIC = Path(__file__).resolve().parent / "static"
MAX_PEOPLE = 20


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    qty: float = Field(1, gt=0, le=20)
    kcal: Optional[float] = Field(None, ge=0, le=5000)   # 1단위 kcal 직접 입력 (없으면 기준표)


class NewMeal(BaseModel):
    date: dt.date
    slot: slots.Slot
    items: list[Item] = []
    people: int = Field(1, ge=1, le=MAX_PEOPLE)
    place: Optional[str] = Field(None, max_length=60)
    note: Optional[str] = Field(None, max_length=200)
    time: Optional[str] = None
    skipped: bool = False


class MealChange(BaseModel):
    date: Optional[dt.date] = None
    slot: Optional[slots.Slot] = None
    items: Optional[list[Item]] = None
    people: Optional[int] = Field(None, ge=1, le=MAX_PEOPLE)
    place: Optional[str] = Field(None, max_length=60)
    time: Optional[str] = None
    note: Optional[str] = Field(None, max_length=200)
    store: Optional[str] = Field(None, max_length=60)   # 영수증으로 알게 된 가게 이름 (null = 지우기)
    confirmed: Optional[bool] = None
    excluded: Optional[bool] = None


class SlotSettingsBody(BaseModel):
    windows: dict[str, tuple[str, str]]
    snack_meal_min_krw: int
    backlog_warn: Optional[int] = Field(None, ge=1, le=1000)   # 없으면 그대로


class ReviewSince(BaseModel):
    date: Optional[dt.date] = None   # None = 기준일 없음 (전부 확인 대상)


class UserMenu(BaseModel):
    unit: str = Field(min_length=1, max_length=20)
    low: int = Field(ge=0, le=suggest.MAX_KCAL)
    mid: Optional[int] = Field(None, ge=0, le=suggest.MAX_KCAL)   # 없으면 low·high 가운데
    high: int = Field(ge=0, le=suggest.MAX_KCAL)
    source: Literal["user", "llm", "label", "calc"] = "user"   # menu_kcal.CUSTOM_SOURCES
    basis: Optional[str] = Field(None, max_length=200)


class EstimateReq(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    unit: Optional[str] = Field(None, max_length=20)


class ConfirmCapture(BaseModel):
    target: str                                  # 채울 끼니 id, 또는 "new" = 새 끼니로
    date: Optional[dt.date] = None               # target="new"일 때 필수
    slot: Optional[slots.Slot] = None
    items: list[Item] = Field(min_length=1)
    store: Optional[str] = Field(None, max_length=60)


class CaptureStatus(BaseModel):
    status: CaptureStatusLiteral   # read = 확인 대기로 되돌리기, dismissed = 치우기, confirmed = 반영함(라벨)


class ChatText(BaseModel):
    text: str = Field(min_length=1, max_length=chat.MAX_TEXT)
    date: Optional[dt.date] = None    # 날짜를 말하지 않았을 때 쓸 날 (화면의 날짜 칸, 기본 오늘)
    today: Optional[dt.date] = None   # 브라우저 기준 오늘 ("어제" 계산용). 없으면 서버 날짜
    force_llm: bool = False           # 규칙 결과가 틀렸을 때 "LLM으로 다시 해석"


class RejectSuggestion(BaseModel):
    signature: str = Field(min_length=1, max_length=400)   # 제안의 메뉴 묶음 표식 (memory.signature)


class NewConversation(BaseModel):
    backend: Literal["local", "claude"] = "local"


class ChatSend(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    today: Optional[dt.date] = None


def create_app(db_path: str | Path, *, source: Optional[CardSource] = None, llm: Optional[LocalLLM] = None,
               llm_url: Optional[str] = None, ocr=None, ocr_url: Optional[str] = None,
               service: Optional[MealService] = None, self_url: str = "http://127.0.0.1:8766",
               backends: Optional[dict] = None) -> FastAPI:
    """``source``·``llm``·``ocr``: 외부 의존 가짜 주입 (테스트). 없으면 card_spending 기본 주소, ``llm_url``·``ocr_url``로
    처음 필요할 때 연결 -- 나중에 켜져도 되게."""
    svc = service or MealService(db_path, source=source, llm=llm, llm_url=llm_url, ocr=ocr, ocr_url=ocr_url,
                                 self_url=self_url, backends=backends)
    app = FastAPI(title="meal_log", docs_url=None, redoc_url=None)
    app.state.service = svc

    @app.exception_handler(ServiceError)
    def service_error(_: Request, exc: ServiceError) -> JSONResponse:
        detail = {"kind": exc.kind, "message": exc.message} if exc.kind else exc.message
        return JSONResponse({"detail": detail}, status_code=exc.http)

    from meal_log.a2a_tasks import mount   # A2A 창구 (Agent Card + JSON-RPC) -- 같은 서비스
    mount(app, svc)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status() -> dict:
        return svc.status()

    @app.post("/api/sync")
    def sync() -> dict:
        return svc.sync()

    @app.get("/api/settings")
    def get_settings() -> dict:
        return svc.settings()

    @app.put("/api/settings")
    def put_settings(body: SlotSettingsBody) -> dict:
        return svc.put_settings(body.windows, body.snack_meal_min_krw, body.backlog_warn)

    @app.get("/api/menus")
    def menus() -> list[dict]:
        return svc.menus()

    @app.put("/api/menus/{name}")
    def put_menu(name: str, body: UserMenu) -> dict:
        return svc.put_menu(name, unit=body.unit, low=body.low, mid=body.mid, high=body.high, source=body.source,
                            basis=body.basis)

    @app.delete("/api/menus/{name}")
    def delete_menu(name: str) -> dict:
        return svc.delete_menu(name)

    @app.post("/api/menus/estimate")
    def estimate_menu(body: EstimateReq) -> dict:
        return svc.estimate_menu(body.name, body.unit)

    @app.get("/api/menus/compose")
    def menus_compose(name: str = Query(..., min_length=1, max_length=60)) -> dict:
        """기준표에 없는 메뉴 이름을 풀어 계산한 추정 + 줄별 근거 (compose.py). 풀 수 없으면 404."""
        return svc.compose(name.strip())

    @app.get("/api/menus/similar")
    def menus_similar(name: str = Query(..., min_length=1, max_length=30)) -> list[dict]:
        return svc.similar(name.strip())

    @app.get("/api/meals")
    def meals(start: Optional[date] = None, end: Optional[date] = None) -> list[dict]:
        return svc.meals(start, end)

    @app.post("/api/meals", status_code=201)
    def add_meal(body: NewMeal) -> dict:
        return {"id": svc.add_meal(day=body.date, slot=body.slot, items=[i.model_dump() for i in body.items],
                                   people=body.people, place=body.place, note=body.note, time=body.time,
                                   skipped=body.skipped)}

    @app.put("/api/meals/{meal_id}")
    def change_meal(meal_id: int, body: MealChange) -> dict:
        changes = {k: getattr(body, k) for k in body.model_fields_set}
        if "items" in changes:
            changes["items"] = [i.model_dump() for i in changes["items"] or []]
        return svc.change_meal(meal_id, changes)

    @app.delete("/api/meals/{meal_id}")
    def delete_meal(meal_id: int) -> dict:
        return svc.delete_meal(meal_id)

    @app.post("/api/meals/{meal_id}/suggestions/reject")
    def reject_suggestion(meal_id: int, body: RejectSuggestion) -> dict:
        return svc.reject_suggestion(meal_id, body.signature)

    @app.delete("/api/meals/{meal_id}/suggestions/reject")
    def unreject_suggestion(meal_id: int, signature: str = Query(..., min_length=1, max_length=400)) -> dict:
        return svc.reject_suggestion(meal_id, signature, undo=True)

    @app.get("/api/review")
    def review() -> dict:
        return svc.review()

    @app.put("/api/review/since")
    def set_review_since(body: ReviewSince) -> dict:
        return svc.set_review_since(body.date)

    @app.get("/api/calendar")
    def calendar(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict:
        return svc.calendar(month)

    @app.get("/api/weekly")
    def weekly(end: Optional[date] = None, weeks: int = Query(12, ge=1, le=60)) -> dict:
        return svc.weekly(end, weeks)

    @app.post("/api/chat/parse")
    def chat_parse(body: ChatText) -> dict:
        return svc.chat_parse(body.text, day=body.date, today=body.today, force_llm=body.force_llm)

    @app.get("/api/assistant/backends")
    def assistant_backends() -> list[dict]:
        return svc.assistant_backends()

    @app.get("/api/conversations")
    def conversations() -> list[dict]:
        return svc.conversations()

    @app.post("/api/conversations", status_code=201)
    def new_conversation(body: NewConversation) -> dict:
        return svc.create_conversation(body.backend)

    @app.get("/api/conversations/{cid}")
    def conversation(cid: str) -> dict:
        return svc.conversation(cid)

    @app.delete("/api/conversations/{cid}")
    def delete_conversation(cid: str) -> dict:
        return svc.delete_conversation(cid)

    @app.post("/api/conversations/{cid}/messages")
    async def send_message(cid: str, body: ChatSend) -> StreamingResponse:
        """답을 SSE로 흘려보낸다 (줄마다 data: {이벤트}). 화면이 연결을 끊으면(멈추기) 세션 실행도 멈춘다.

        세션 실행은 전용 스레드가 돌리고 끝나면 그 스레드가 직접 닫는다 -- 연결이 끊겨도 GC를 기다리지 않고 바로 닫혀야
        답 저장·대화 잠금 해제·claude 프로세스 종료가 일어난다 (2026-11-15 브라우저 점검: 멈춘 뒤 대화가 계속 잠김)."""
        import asyncio
        import json as _json
        import threading
        events = svc.send_message(cid, body.text, today=body.today)   # 대화 없음·빈 말·답하는 중은 여기서 바로 HTTP 에러
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def put(x) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, x)
            except RuntimeError:   # 서버가 내려가는 중
                pass

        def pump() -> None:
            try:
                for ev in events:
                    if stop.is_set():
                        break
                    put(ev)
            finally:
                events.close()   # 같은 스레드에서 닫는다 (멈춤이면 서비스가 '멈춤'으로 저장)
                put(None)
        threading.Thread(target=pump, name=f"chat-{cid}", daemon=True).start()

        async def sse():
            try:
                while (ev := await queue.get()) is not None:
                    yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
            finally:
                stop.set()
        return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/assistant/tools/{name}")
    def assistant_tool(name: str, args: dict) -> object:
        """대화 도우미 도구 실행 (Claude 세션의 MCP 다리가 부름, 로컬 LLM 세션은 서비스에서 바로). 결과는 식단만 (chat_tools)."""
        from meal_log.chat_tools import run_tool
        return run_tool(svc, name, args)

    @app.post("/api/captures")
    async def upload_captures(files: list[UploadFile] = File(...)) -> dict:
        blobs = [((f.filename or "image"), await f.read()) for f in files]
        r = svc.upload_captures(blobs)
        return {"captures": r.captures, "errors": r.errors}

    @app.get("/api/captures")
    def list_captures(status: CaptureStatusLiteral = "read") -> list[dict]:
        return svc.captures(status)

    @app.get("/api/captures/{cid}/image")
    def capture_image(cid: str) -> FileResponse:
        return FileResponse(svc.capture_image(cid), media_type="image/png")

    @app.post("/api/captures/{cid}/confirm")
    def confirm_capture(cid: str, body: ConfirmCapture) -> dict:
        return svc.confirm_capture(cid, target=body.target, items=[i.model_dump() for i in body.items], day=body.date,
                                   slot=body.slot, store_name=body.store)

    @app.put("/api/captures/{cid}/status")
    def capture_status(cid: str, body: CaptureStatus) -> dict:
        return svc.set_capture_status(cid, body.status)

    return app
