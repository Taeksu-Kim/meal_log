"""대화 세션 2종 -- ``ChatBackend`` 하나 뒤의 두 구현 (REF/AGENT_PRODUCT.md "채팅 세션 2종").

① ``ClaudeBackend``: Claude Code를 headless로 (``claude -p``). 개발용 CLAUDE.md·사용자 설정은 싣지 않고(``--setting-sources ""``)
   도우미 지침(assistant/GUIDE.md)만, 기본 도구는 끄고(``--tools ""``) meal_log MCP 도구만 (mcp_bridge.py -> 서버의 도구 API).
   첫 턴 ``--session-id``, 다음 턴 ``--resume`` (세션 id는 서비스 SQLite). 플래그 동작은 2026-11-15 실측 (REF/AGENT_PRODUCT.md).
② ``LocalBackend``: 로컬 Qwen(vLLM). 지금 vLLM은 도구 호출 기능 없이 떠 있어서, 매 단계 "도구 쓰기 / 답하기"를 JSON 스키마로 고르게 한다.
   같은 지침·같은 도구(chat_tools.TOOLS).

둘 다 같은 이벤트를 낸다 (화면 SSE가 그대로 받음):
  {"type": "text", "text": 조각} · {"type": "tool", "name", "input"} · {"type": "tool_result", "name", "ok", "summary", "dates"}
  · {"type": "done", "text", "cost"?, "session"?} · {"type": "error", "kind", "message"}
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal, Optional, Protocol

from meal_log.chat_tools import TOOLS, run_tool
from meal_log.llm import LLMBadOutput, LLMDown, ask_json
from meal_log.service import MealService, ServiceError

Backend = Literal["claude", "local"]
GUIDE = Path(__file__).resolve().parent.parent / "assistant" / "GUIDE.md"
ASSISTANT_HOME = GUIDE.parent        # Claude 세션의 cwd (세션 기록이 cwd별로 저장되므로 늘 같은 곳에서 띄운다)
DOW = "월화수목금토일"
MAX_LOCAL_STEPS = 5
HISTORY_TURNS = 8                    # 로컬 LLM에 다시 보여 줄 이전 메시지 수 (컨텍스트가 작다)


@dataclass
class Turn:
    conv: dict                       # conversations 행
    history: list[dict]              # 이전 메시지 (role, text)
    text: str                        # 이번 사용자 말
    today: dt.date


class ChatBackend(Protocol):
    name: Backend

    def available(self) -> tuple[bool, str]: ...      # (쓸 수 있나, 못 쓰면 이유와 켜는 법)

    def run(self, turn: Turn) -> Iterator[dict]: ...


def today_line(d: dt.date) -> str:
    return f"[오늘은 {d.isoformat()} {DOW[d.weekday()]}요일]"


def summarize(name: str, args: dict, result: object) -> tuple[str, list[str]]:
    """도구 결과 -> 화면에 보일 한 줄과 바뀐 날짜들 (캘린더 갱신·강조용). 두 세션이 같은 문구를 쓴다."""
    r = result if isinstance(result, dict) else {}
    if "error" in r or "detail" in r:
        msg = r.get("error") or r.get("detail")
        return f"{name}: {msg.get('message') if isinstance(msg, dict) else msg}", []

    def meal_line(m: dict) -> str:
        d = dt.date.fromisoformat(m["date"])
        items = "·".join(f"{i['name']}{'' if i['qty'] == 1 else f' ×{i['qty']:g}'}" for i in m["items"]) or "메뉴 없음"
        return f"{d.month}/{d.day} {m['slot']} {items}" + (f" ({m['people']}명)" if m.get("people", 1) != 1 else "")

    if name == "log_meal":
        if r.get("saved"):
            return "기록함: " + ", ".join(meal_line(m) for m in r["saved"]), [m["date"] for m in r["saved"]]
        if r.get("need"):
            return "기록 전 확인 필요: " + ", ".join(r["need"]), []
        return "먹은 내용을 찾지 못함", []
    if name == "update_meal" and "date" in r:
        return "고침: " + meal_line(r), [r["date"]]
    if name == "save_menu" and "saved" in r:
        return f"내 메뉴 저장: {r['saved']} {r['unit']} {r['low']}–{r['high']} kcal", []
    if name == "query_meals":
        return f"식단 조회: {r.get('start')} ~ {r.get('end')}", []
    if name == "menu_info":
        return f"메뉴 kcal 확인: {r.get('name')}" + ("" if r.get("known") else " (기준 없음)"), []
    if name == "weekly":
        return f"주별 추이 조회 ({len(r.get('weeks', []))}주)", []
    if name == "pending_meals":
        return f"확인할 끼니 조회 ({r.get('count', 0)}끼)", []
    return name, []


# ---------------------------------------------------------------- ① Claude Code
def run_subprocess(cmd: list[str], cwd: Path) -> Iterator[str]:
    """claude -p를 띄워 stdout 줄을 내보낸다. 소비 쪽이 멈추면(화면이 연결을 끊음) 프로세스를 끝낸다."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8")
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line
        proc.wait(timeout=30)
        if proc.returncode:
            err = (proc.stderr.read() if proc.stderr else "")[-400:]
            yield json.dumps({"type": "_exit", "code": proc.returncode, "stderr": err})
    finally:
        if proc.poll() is None:
            proc.terminate()


class ClaudeBackend:
    name: Backend = "claude"

    def __init__(self, mcp_url: str, *, runner: Optional[Callable[[list[str], Path], Iterator[str]]] = None,
                 claude_bin: Optional[str] = None, guide: Path = GUIDE, home: Path = ASSISTANT_HOME) -> None:
        self.mcp_url = mcp_url.rstrip("/")
        self.runner = runner or run_subprocess
        self.bin = claude_bin if claude_bin is not None else shutil.which("claude")
        self.guide, self.home = guide, home

    def available(self) -> tuple[bool, str]:
        if not self.bin:
            return False, "claude 명령이 없습니다 (Claude Code 설치·로그인 필요: npm i -g @anthropic-ai/claude-code, claude 로그인)"
        return True, ""

    def command(self, turn: Turn) -> list[str]:
        mcp = {"mcpServers": {"meal_log": {"command": sys.executable, "args": ["-m", "meal_log.mcp_bridge", "--url", self.mcp_url],
                                           "env": {"PYTHONPATH": str(Path(__file__).resolve().parent.parent)}}}}
        first = not turn.conv.get("claude_started")   # 턴 수가 아니라 세션이 실제로 만들어졌는지 (멈추거나 실패한 첫 턴 뒤에도 맞게)
        return [self.bin or "claude", "-p", f"{today_line(turn.today)} {turn.text}",
                "--session-id" if first else "--resume", turn.conv["claude_session"],
                "--tools", "", "--strict-mcp-config", "--mcp-config", json.dumps(mcp, ensure_ascii=False),
                "--allowedTools", "mcp__meal_log", "--setting-sources", "",
                "--append-system-prompt-file", str(self.guide),
                "--output-format", "stream-json", "--verbose", "--include-partial-messages"]

    def run(self, turn: Turn) -> Iterator[dict]:
        ok, why = self.available()
        if not ok:
            yield {"type": "error", "kind": "claude_down", "message": why}
            return
        names: dict[str, tuple[str, dict]] = {}
        streamed = False
        for line in self.runner(self.command(turn), self.home):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                yield {"type": "session", "id": ev.get("session_id")}   # 서비스가 기록 (화면에는 안 보냄)
            elif t == "stream_event":
                e = ev.get("event") or {}
                if e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                    streamed = True
                    yield {"type": "text", "text": e["delta"]["text"]}
            elif t == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        name = str(c.get("name", "")).removeprefix("mcp__meal_log__")
                        names[c.get("id")] = (name, c.get("input") or {})
                        yield {"type": "tool", "name": name, "input": c.get("input") or {}}
                    elif c.get("type") == "text" and not streamed:
                        yield {"type": "text", "text": c.get("text", "")}
                streamed = False
            elif t == "user":
                for c in (ev.get("message") or {}).get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        name, args = names.get(c.get("tool_use_id"), ("?", {}))
                        raw = c.get("content")
                        text = raw[0].get("text", "") if isinstance(raw, list) and raw else str(raw or "")
                        try:
                            result = json.loads(text)
                        except ValueError:
                            result = {"error": text[:200]}
                        summary, dates = summarize(name, args, result)
                        yield {"type": "tool_result", "name": name, "ok": not c.get("is_error"), "summary": summary, "dates": dates}
            elif t == "result":
                if ev.get("is_error") or ev.get("subtype") != "success":
                    yield {"type": "error", "kind": "claude_error", "message": str(ev.get("result") or ev.get("subtype"))[:300]}
                    return
                yield {"type": "done", "text": ev.get("result", ""), "cost": ev.get("total_cost_usd"), "session": ev.get("session_id")}
                return
            elif t == "_exit":
                yield {"type": "error", "kind": "claude_error",
                       "message": f"claude가 오류로 끝남 (코드 {ev['code']}): {ev.get('stderr', '').strip()[-200:] or '로그인 상태 확인: claude'}"}
                return
        yield {"type": "error", "kind": "claude_error", "message": "claude가 결과 없이 끝남"}


# ---------------------------------------------------------------- ② 로컬 LLM
def _step_schema() -> dict:
    return {"anyOf": [
        {"type": "object", "required": ["action", "tool", "args"], "properties": {
            "action": {"type": "string", "const": "tool"}, "tool": {"type": "string", "enum": list(TOOLS)},
            "args": {"type": "object"}}},
        {"type": "object", "required": ["action", "text"], "properties": {
            "action": {"type": "string", "const": "answer"}, "text": {"type": "string"}}}]}


LOCAL_START = ("vLLM(:8002)을 켜 주세요 -- REF/LLM.md. "
               "OCR 서버와 GPU를 같이 쓰기 어려우니 먼저 끄세요: bash ../doc_reader/doc_parser/tools/ocr.sh stop")


class LocalBackend:
    name: Backend = "local"

    def __init__(self, svc: MealService, llm_getter: Callable[[], object], ping: Optional[Callable[[], bool]] = None,
                 guide: Path = GUIDE) -> None:
        self.svc, self.llm_getter, self.ping, self.guide = svc, llm_getter, ping, guide

    def available(self) -> tuple[bool, str]:
        try:
            if self.ping is not None and not self.ping():
                raise LLMDown("응답 없음")
            self.llm_getter()
        except LLMDown as exc:
            return False, f"로컬 LLM이 꺼져 있습니다 ({exc}). {LOCAL_START}"
        return True, ""

    def _system(self, today: dt.date) -> str:
        tools = "\n".join(f"- {t.name}: {t.description}. 인자: {json.dumps(t.schema.get('properties', {}), ensure_ascii=False)}"
                          for t in TOOLS.values())
        return (self.guide.read_text(encoding="utf-8") + f"\n\n{today_line(today)}\n\n## 도구\n{tools}\n\n## 답하는 형식\n"
                "매번 JSON 하나: 도구를 쓰려면 {\"action\": \"tool\", \"tool\": 이름, \"args\": {...}}, "
                "사용자에게 답하려면 {\"action\": \"answer\", \"text\": 답}. 도구 결과를 본 뒤 답한다. 결과에 없는 사실은 지어내지 않는다.")

    def run(self, turn: Turn) -> Iterator[dict]:
        from agent.core.llm import OutputError

        try:
            llm = self.llm_getter()
        except LLMDown as exc:
            yield {"type": "error", "kind": "llm_down", "message": self.available()[1] or str(exc)}
            return
        lines = [f"{'사용자' if m['role'] == 'user' else '도우미'}: {m['text']}" for m in turn.history[-HISTORY_TURNS:]]
        lines.append(f"사용자: {turn.text}")
        system = self._system(turn.today)

        def check(d: object) -> dict:
            if not isinstance(d, dict) or d.get("action") not in ("tool", "answer"):
                raise OutputError("action은 tool 또는 answer")
            if d["action"] == "tool" and (d.get("tool") not in TOOLS or not isinstance(d.get("args"), dict)):
                raise OutputError(f"tool은 {', '.join(TOOLS)} 중 하나, args는 객체")
            if d["action"] == "answer" and not str(d.get("text") or "").strip():
                raise OutputError("text가 비었음")
            return d

        for _ in range(MAX_LOCAL_STEPS):
            try:
                step = ask_json(llm, system, "\n".join(lines), _step_schema(), check, what="assistant step")
            except LLMDown as exc:
                yield {"type": "error", "kind": "llm_down", "message": f"로컬 LLM 연결이 끊김 ({exc}). {LOCAL_START}"}
                return
            except LLMBadOutput as exc:
                yield {"type": "error", "kind": "bad_llm_output", "message": f"로컬 LLM 답을 해석하지 못함: {exc}"}
                return
            if step["action"] == "answer":
                yield {"type": "text", "text": step["text"]}
                yield {"type": "done", "text": step["text"]}
                return
            name, args = step["tool"], step["args"]
            yield {"type": "tool", "name": name, "input": args}
            try:
                result = run_tool(self.svc, name, args)
                ok = True
            except ServiceError as exc:
                result, ok = {"error": exc.message}, False
            summary, dates = summarize(name, args, result)
            yield {"type": "tool_result", "name": name, "ok": ok, "summary": summary, "dates": dates}
            text = json.dumps(result, ensure_ascii=False)
            lines.append(f"[도구 {name} 결과] {text[:3000]}" + (" …(잘림)" if len(text) > 3000 else ""))
        yield {"type": "text", "text": "여러 번 시도했지만 끝내지 못했습니다. 조금 더 구체적으로 말씀해 주세요."}
        yield {"type": "done", "text": "여러 번 시도했지만 끝내지 못했습니다."}
