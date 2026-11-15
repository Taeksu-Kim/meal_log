"""Claude Code 대화 세션용 MCP 서버 (stdio) -- meal_log 도우미 도구(chat_tools.TOOLS)를 Claude에게 보여 주고,
실행은 떠 있는 meal_log 서버(POST /api/assistant/tools/{name})에 넘긴다 (상태의 주인은 서비스 하나).

    claude -p ... --mcp-config '{"mcpServers": {"meal_log": {"command": "<python>", "args": ["-m", "meal_log.mcp_bridge", "--url", "http://127.0.0.1:8766"]}}}'

MCP stdio = 줄마다 JSON-RPC 2.0. 쓰는 메서드만: initialize · notifications/initialized · tools/list · tools/call · ping.
(mcp 패키지를 설치하지 않으려고 손으로 구현 -- 도구 호출 외의 MCP 기능은 없다)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from meal_log.chat_tools import TOOLS

PROTOCOL = "2025-06-18"


def _call(url: str, name: str, args: dict) -> tuple[str, bool]:
    req = urllib.request.Request(f"{url}/api/assistant/tools/{name}", data=json.dumps(args, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read().decode(), False
    except urllib.error.HTTPError as exc:
        return exc.read().decode(errors="replace"), True
    except (urllib.error.URLError, OSError) as exc:
        return json.dumps({"error": f"meal_log 서버에 연결할 수 없음 ({url}): {exc}"}, ensure_ascii=False), True


def handle(msg: dict, url: str) -> dict | None:
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:                       # 알림(notifications/*)에는 답하지 않는다
        return None
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or PROTOCOL,
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "meal_log", "version": "0.1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema} for t in TOOLS.values()]}
    elif method == "tools/call":
        p = msg.get("params") or {}
        text, is_error = _call(url, str(p.get("name")), p.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8766")
    url = ap.parse_args(argv).url.rstrip("/")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            out = handle(json.loads(line), url)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
