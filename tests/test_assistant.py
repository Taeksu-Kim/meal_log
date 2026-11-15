"""대화 도우미 (세션 2종) API 테스트. 바깥(Claude Code 프로세스·로컬 LLM)만 가짜, 값은 전부 지어낸 것.
가짜 Claude 실행기는 2026-11-15 실측한 stream-json 모양 그대로 줄을 낸다 (REF/AGENT_PRODUCT.md)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from meal_log.app import create_app
from meal_log.chat_backends import ClaudeBackend, LocalBackend
from meal_log.chat_tools import run_tool
from tests.fake_card import FakeCard, row

TODAY = "2026-11-15"
ROWS = [row("2026-11-14", "19:30", "우아한형제들", 21000, "배달")]


class StepLLM:
    """로컬 LLM 가짜: 이번 턴에 도구 결과가 아직 없으면 도구를, 있으면 답을 낸다 (프롬프트 내용으로 판단)."""

    def __init__(self, tool: dict, answer: str) -> None:
        self.tool, self.answer, self.prompts = tool, answer, []

    def complete(self, system, user, *, schema=None):
        self.prompts.append((system, user))
        last_user = user.rsplit("사용자: ", 1)[-1]
        if "[도구" in last_user:
            return json.dumps({"action": "answer", "text": self.answer}, ensure_ascii=False)
        return json.dumps({"action": "tool", **self.tool}, ensure_ascii=False)


class FakeClaude:
    """claude -p 가짜 실행기: 받은 명령을 기록하고, 도구는 MCP 대신 실제 도구 함수로 실행해 stream-json 줄을 낸다."""

    def __init__(self) -> None:
        self.cmds, self.svc, self.script = [], None, []

    def __call__(self, cmd, cwd):
        self.cmds.append(cmd)
        sid = cmd[cmd.index("--session-id") + 1] if "--session-id" in cmd else cmd[cmd.index("--resume") + 1]
        if self.script and self.script[0] and self.script[0][0][0] == "nologin":   # 세션이 만들어지기 전에 실패
            self.script.pop(0)
            yield json.dumps({"type": "_exit", "code": 1, "stderr": "Invalid API key · Please run /login"})
            return
        yield json.dumps({"type": "system", "subtype": "init", "session_id": sid, "tools": ["mcp__meal_log__log_meal"]})
        for step in self.script.pop(0):
            if step[0] == "tool":
                _, name, args = step
                yield json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "t1", "name": f"mcp__meal_log__{name}", "input": args}]}})
                result = run_tool(self.svc, name, args)
                yield json.dumps({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}]}})
            elif step[0] == "text":
                for piece in (step[1][:5], step[1][5:]):
                    yield json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                                        "delta": {"type": "text_delta", "text": piece}}})
                yield json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": step[1]}]}})
            elif step[0] == "fail":
                yield json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "로그인 필요"})
                return
        yield json.dumps({"type": "result", "subtype": "success", "result": "끝", "total_cost_usd": 0.07, "session_id": sid})


def make(tmp_path, *, llm=None, claude=None, claude_bin="/usr/bin/claude", name="m.db"):
    fake = FakeCard(list(ROWS))
    holder = {}
    backends = None
    if llm is not None or claude is not None:
        backends = {}
    app = create_app(tmp_path / name, source=fake.source, llm=llm, backends=backends)
    svc = app.state.service
    if backends is not None:
        backends["local"] = LocalBackend(svc, svc._get_llm, ping=lambda: llm is not None)
        backends["claude"] = ClaudeBackend("http://127.0.0.1:1", runner=claude or FakeClaude(), claude_bin=claude_bin)
        svc.backends = backends
    if isinstance(claude, FakeClaude):
        claude.svc = svc
    c = TestClient(app)
    c.post("/api/sync")
    holder["svc"] = svc
    return c, svc


def sse(c, cid, text):
    r = c.post(f"/api/conversations/{cid}/messages", json={"text": text, "today": TODAY})
    assert r.status_code == 200, r.text
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


def test_backends_listing_and_lock(tmp_path):
    c, _ = make(tmp_path, llm=None, claude=FakeClaude(), claude_bin="")
    b = {x["id"]: x for x in c.get("/api/assistant/backends").json()}
    assert not b["local"]["available"] and "vLLM" in b["local"]["reason"]          # 로컬 꺼짐 -> 잠금 + 켜는 법
    assert not b["claude"]["available"] and "claude" in b["claude"]["reason"]
    assert b["claude"]["sends_out"] and "결제 금액·가게·카드 정보는 보내지 않습니다" in b["claude"]["notice"]
    r = c.post("/api/conversations", json={"backend": "claude"})
    assert r.status_code == 503 and r.json()["detail"]["kind"] == "claude_down"


def test_local_conversation_records_meal(tmp_path):
    llm = StepLLM({"tool": "log_meal", "args": {"text": "오늘 점심에 김밥 한 줄"}}, "11/15(일) 점심에 김밥 한 줄을 기록했습니다.")
    c, _ = make(tmp_path, llm=llm, claude=FakeClaude())
    conv = c.post("/api/conversations", json={"backend": "local"}).json()
    ev = sse(c, conv["id"], "오늘 점심에 김밥 한 줄 먹었어")
    assert [e["type"] for e in ev] == ["tool", "tool_result", "text", "done"]
    assert ev[1]["summary"] == "기록함: 11/15 점심 김밥" and ev[1]["dates"] == [TODAY]
    assert c.get("/api/meals", params={"start": TODAY, "end": TODAY}).json()[0]["note"] == "대화"   # 화면에 보임
    system, user = llm.prompts[0]
    assert "식단 도우미 지침" in system and "[오늘은 2026-11-15 일요일]" in system and "log_meal" in system
    sse(c, conv["id"], "고마워")
    assert "사용자: 오늘 점심에 김밥 한 줄 먹었어" in llm.prompts[-1][1]                          # 이전 대화를 같이 보낸다
    saved = c.get(f"/api/conversations/{conv['id']}").json()
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant", "user", "assistant"]
    assert saved["messages"][1]["events"][0]["summary"].startswith("기록함") and saved["title"].startswith("오늘 점심에")
    assert saved["backend"] == "local"


def test_local_llm_down_is_error_event(tmp_path):
    class Down:
        def complete(self, *a, **k):
            from agent.core.llm import LLMUnavailableError
            raise LLMUnavailableError("refused")
    c, svc = make(tmp_path, llm=Down(), claude=FakeClaude())
    conv = c.post("/api/conversations", json={"backend": "local"}).json()
    ev = sse(c, conv["id"], "오늘 점심에 김밥")
    assert ev[-1]["type"] == "error" and ev[-1]["kind"] == "llm_down" and "vLLM(:8002)" in ev[-1]["message"]   # 켜는 법까지
    msgs = c.get(f"/api/conversations/{conv['id']}").json()["messages"]
    assert msgs[0]["text"] == "오늘 점심에 김밥" and msgs[1]["events"][0]["name"] == "error"     # 사용자 말은 남는다


def test_claude_session_flags_resume_and_events(tmp_path):
    fc = FakeClaude()
    c, _ = make(tmp_path, llm=StepLLM({}, ""), claude=fc)
    conv = c.post("/api/conversations", json={"backend": "claude"}).json()
    fc.script = [[("tool", "query_meals", {"start": "2026-11-14", "end": "2026-11-14"}),
                  ("tool", "update_meal", {"meal_id": 1, "items": [{"name": "치킨", "qty": 0.5}]}),
                  ("text", "11/14(토) 저녁에 치킨 반 마리를 넣었습니다.")],
                 [("text", "네, 2명으로 고쳤습니다.")]]
    ev = sse(c, conv["id"], "어제 저녁 배달은 치킨 반 마리였어")
    cmd = fc.cmds[0]
    sid = cmd[cmd.index("--session-id") + 1]
    for flag, val in (("--tools", ""), ("--setting-sources", ""), ("--allowedTools", "mcp__meal_log"), ("--output-format", "stream-json")):
        assert cmd[cmd.index(flag) + 1] == val, flag
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--append-system-prompt-file") + 1].endswith("assistant/GUIDE.md")
    assert cmd[2].startswith("[오늘은 2026-11-15 일요일] ")
    assert [e["type"] for e in ev] == ["tool", "tool_result", "tool", "tool_result", "text", "text", "done"]
    assert ev[3]["summary"] == "고침: 11/14 저녁 치킨 ×0.5" and ev[-1]["cost"] == 0.07
    assert "".join(e["text"] for e in ev if e["type"] == "text") == "11/14(토) 저녁에 치킨 반 마리를 넣었습니다."
    sse(c, conv["id"], "2명이었어")
    assert "--resume" in fc.cmds[1] and fc.cmds[1][fc.cmds[1].index("--resume") + 1] == sid      # 같은 세션 이어서
    saved = c.get(f"/api/conversations/{conv['id']}").json()
    assert saved["turns"] == 2 and saved["messages"][1]["cost"] == 0.07
    # 재시작 후에도 같은 세션으로 이어 감
    fc2 = FakeClaude()
    fc2.script = [[("text", "이어서 답합니다.")]]
    c2, svc2 = make(tmp_path, llm=StepLLM({}, ""), claude=fc2)
    assert [x["id"] for x in c2.get("/api/conversations").json()] == [conv["id"]]
    sse(c2, conv["id"], "아까 뭐 먹었지?")
    assert fc2.cmds[0][fc2.cmds[0].index("--resume") + 1] == sid


def test_claude_failure_and_busy(tmp_path):
    fc = FakeClaude()
    c, svc = make(tmp_path, llm=StepLLM({}, ""), claude=fc)
    conv = c.post("/api/conversations", json={"backend": "claude"}).json()
    fc.script = [[("fail",)]]
    ev = sse(c, conv["id"], "안녕")
    assert ev[-1] == {"type": "error", "kind": "claude_error", "message": "로그인 필요"}
    assert c.get(f"/api/conversations/{conv['id']}").json()["turns"] == 0          # 실패한 턴은 세지 않음
    fc.script = [[("text", "첫 답입니다")], [("text", "둘째")]]
    pending = svc.send_message(conv["id"], "하나")                                  # 아직 답하는 중
    r = c.post(f"/api/conversations/{conv['id']}/messages", json={"text": "둘"})
    assert r.status_code == 409
    list(pending)                                                                  # 끝나면 다시 보낼 수 있음
    assert sse(c, conv["id"], "둘")[-1]["type"] == "done"
    assert c.post(f"/api/conversations/{conv['id']}/messages", json={"text": " "}).status_code == 400
    assert c.post("/api/conversations/nope/messages", json={"text": "x"}).status_code == 404
    assert c.delete(f"/api/conversations/{conv['id']}").status_code == 200 and c.get("/api/conversations").json() == []


def test_tools_never_expose_payment_info(tmp_path):
    c, svc = make(tmp_path, llm=StepLLM({}, ""), claude=FakeClaude())
    for name, args in (("query_meals", {"start": "2026-11-14", "end": "2026-11-14"}), ("pending_meals", {}), ("weekly", {}),
                       ("log_meal", {"text": "어제 저녁 치킨 반 마리", "date": "2026-11-15"})):
        out = json.dumps(c.post(f"/api/assistant/tools/{name}", json=args).json(), ensure_ascii=False)
        for word in ("amount", "place", "store", "category", "pay_date", "21000", "21,000", "우아한형제들", "배달"):
            assert word not in out, (name, word)
    assert c.post("/api/assistant/tools/rm_rf", json={}).status_code == 400


def test_mcp_bridge_protocol(monkeypatch):
    from meal_log import mcp_bridge
    assert mcp_bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, "u") is None
    init = mcp_bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, "u")
    assert init["result"]["capabilities"] == {"tools": {}} and init["result"]["protocolVersion"] == "2025-06-18"
    tools = mcp_bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "u")["result"]["tools"]
    assert {t["name"] for t in tools} == {"query_meals", "log_meal", "update_meal", "menu_info", "save_menu", "weekly", "pending_meals"}
    monkeypatch.setattr(mcp_bridge, "_call", lambda url, name, args: (json.dumps({"called": name, "args": args}), False))
    res = mcp_bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "weekly", "arguments": {"weeks": 2}}}, "u")
    assert json.loads(res["result"]["content"][0]["text"]) == {"called": "weekly", "args": {"weeks": 2}}
    assert mcp_bridge.handle({"jsonrpc": "2.0", "id": 4, "method": "resources/list"}, "u")["error"]["code"] == -32601


def test_claude_first_turn_flag_follows_real_session(tmp_path):
    """--session-id/--resume은 턴 수가 아니라 세션이 실제로 만들어졌는지(init 이벤트)로 고른다."""
    fc = FakeClaude()
    c, svc = make(tmp_path, llm=StepLLM({}, ""), claude=fc)
    conv = c.post("/api/conversations", json={"backend": "claude"}).json()
    fc.script = [[("nologin",)], [("fail",)], [("text", "됐습니다")]]
    ev = sse(c, conv["id"], "안녕")                       # 세션 만들기 전에 실패 -> 다음도 --session-id
    assert ev[-1]["kind"] == "claude_error" and "login" in ev[-1]["message"]
    assert all(e["type"] != "session" for e in ev)        # 내부 표식은 화면에 안 보냄
    sse(c, conv["id"], "안녕")                            # 세션은 만들어졌지만 실패 -> 다음은 --resume
    sse(c, conv["id"], "안녕")
    flags = ["--session-id" if "--session-id" in cmd else "--resume" for cmd in fc.cmds]
    assert flags == ["--session-id", "--session-id", "--resume"]


def test_stop_mid_answer_saves_and_unlocks(tmp_path):
    """멈추기 (화면이 연결을 끊음 = 생성기 close): '멈춤'으로 저장, 대화 잠금 풀림."""
    fc = FakeClaude()
    c, svc = make(tmp_path, llm=StepLLM({}, ""), claude=fc)
    conv = c.post("/api/conversations", json={"backend": "claude"}).json()
    fc.script = [[("text", "아주 긴 답의 앞부분"), ("text", "뒷부분")], [("text", "다시")]]
    g = svc.send_message(conv["id"], "길게 답해줘")
    assert next(g)["type"] == "text"
    g.close()
    msgs = c.get(f"/api/conversations/{conv['id']}").json()["messages"]
    assert msgs[-1]["text"] == "아주 긴" and msgs[-1]["events"][-1]["summary"] == "멈춤"
    assert sse(c, conv["id"], "다시")[-1]["type"] == "done"       # 잠금 풀림, 세션은 이어서
    assert "--resume" in fc.cmds[-1]
